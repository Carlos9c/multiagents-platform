"""Functional QA Agent — two-pass LLM analysis.

Pass 1 (scenario generation):
  Given source code and the project goal, an LLM generates a prioritised set
  of test scenarios covering happy paths, edge cases, and error conditions.

Pass 2 (result analysis):
  The generated scenarios are (optionally) executed in Docker.  A second LLM
  call receives the source code, the scenarios, and any execution output and
  produces structured QA findings.

This two-pass design separates *what to test* (creative, domain-driven) from
*whether the code handles it correctly* (analytical), giving each LLM call a
focused, single responsibility.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa import source_reader
from app.services.qa.agents._llm_schemas import (
    ProbeAnalysisOutput,
    QAFindingItem,
    ScenarioList,
)
from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QAFindingDetail, QARequest
from app.services.qa.docker_runner import run_in_project_container
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_GENERATE_PROMPT = prompt_loader.get("qa/functional_qa_agent", "generate_scenarios")
_ANALYZE_PROMPT = prompt_loader.get("qa/functional_qa_agent", "analyze_results")

_TEST_COMMANDS = [
    "python -m pytest --tb=short -q 2>&1 | head -60",
    "npm test --if-present 2>&1 | head -60",
    "cargo test 2>&1 | head -60",
    "go test ./... 2>&1 | head -60",
]


def _to_finding_detail(item: QAFindingItem, producer_agent: str | None = None) -> QAFindingDetail:
    return QAFindingDetail(
        finding_id=item.finding_id,
        severity=item.severity,
        category=item.category,
        title=item.title,
        description=item.description,
        reproduction_steps=item.reproduction_steps,
        affected_component=item.affected_component,
        auto_remediable=item.auto_remediable,
        remediation_hint=item.remediation_hint,
        producer_agent=producer_agent,
    )


def _run_tests(request: QARequest) -> str:
    """Try test commands in Docker; return combined output or 'unavailable'."""
    for cmd in _TEST_COMMANDS:
        result = run_in_project_container(
            project_id=request.project_id,
            command=cmd,
            cwd=request.source_path,
            timeout_seconds=60,
        )
        if not result.available:
            return "unavailable"
        if result.succeeded or result.stdout or result.stderr:
            return (result.stdout + result.stderr)[:3000] or "unavailable"
    return "unavailable"


class FunctionalQAAgent(BaseQAAgent):
    """Two-pass functional correctness agent: generate scenarios → analyse results."""

    @property
    def name(self) -> str:
        return "functional_qa_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        if not files:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="functional_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="No source files found for analysis",
                )
            )
            return session

        source_block = source_reader.format_for_prompt(files)

        # ── Pass 1: Generate test scenarios ────────────────────────────────────
        scenarios = self._generate_scenarios(request, source_block)
        if scenarios is None:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="functional_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="Pass 1 (scenario generation) LLM call failed",
                )
            )
            return session

        scenarios_json = json.dumps(
            [s.model_dump() for s in scenarios.scenarios],
            ensure_ascii=False,
            indent=2,
        )

        # ── Optional: run tests in Docker ──────────────────────────────────────
        execution_output = _run_tests(request)

        # ── Pass 2: Analyse results ────────────────────────────────────────────
        analysis = self._analyze_results(request, source_block, scenarios_json, execution_output)
        if analysis is None:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="functional_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="Pass 2 (result analysis) LLM call failed",
                )
            )
            return session

        for item in analysis.findings:
            session.add_finding(_to_finding_detail(item, self.name))

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="functional_analysis",
                target=request.source_path,
                outcome=analysis.probe_outcome,
                findings_count=len(analysis.findings),
                notes=f"Scenarios generated: {len(scenarios.scenarios)}. {analysis.probe_summary}",
            )
        )

        logger.info(
            "functional_qa_agent_complete project_id=%s scenarios=%s findings=%s outcome=%s",
            request.project_id,
            len(scenarios.scenarios),
            len(analysis.findings),
            analysis.probe_outcome,
        )
        return session

    # ── LLM helpers ────────────────────────────────────────────────────────────

    def _generate_scenarios(self, request: QARequest, source_block: str) -> ScenarioList | None:
        prompt_loader.validate_builder_inputs(
            "qa/functional_qa_agent",
            "generate_scenarios",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
            },
        )
        user_prompt = (
            f"project_goal: {request.project_goal}\n"
            f"product_type: {request.product_type}\n\n"
            f"Source files:\n{source_block}\n\n"
            "Generate a prioritised set of test scenarios."
        )
        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_GENERATE_PROMPT,
                user_prompt=user_prompt,
                schema_name="functional_qa_agent_scenarios",
                json_schema=ScenarioList.model_json_schema(),
            )
            return ScenarioList.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("functional_qa_agent_pass1_failed exc=%s", exc)
            return None

    def _analyze_results(
        self,
        request: QARequest,
        source_block: str,
        scenarios_json: str,
        execution_output: str,
    ) -> ProbeAnalysisOutput | None:
        prompt_loader.validate_builder_inputs(
            "qa/functional_qa_agent",
            "analyze_results",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
                "scenarios_json": scenarios_json,
                "execution_output": execution_output,
            },
        )
        user_prompt = (
            f"project_goal: {request.project_goal}\n"
            f"product_type: {request.product_type}\n\n"
            f"Source files:\n{source_block}\n\n"
            f"Test scenarios:\n{scenarios_json}\n\n"
            f"Execution output:\n{execution_output}\n\n"
            "Analyse which scenarios the implementation handles correctly."
        )
        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_ANALYZE_PROMPT,
                user_prompt=user_prompt,
                schema_name="functional_qa_agent_analysis",
                json_schema=ProbeAnalysisOutput.model_json_schema(),
            )
            return ProbeAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("functional_qa_agent_pass2_failed exc=%s", exc)
            return None
