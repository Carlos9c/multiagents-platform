"""Functional tester QA agent.

Runs the project's test suite via Docker (when available) and performs
LLM-based static analysis to identify functional defects.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa import source_reader
from app.services.qa.agents._llm_schemas import ProbeAnalysisOutput, QAFindingItem
from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QAFindingDetail, QARequest
from app.services.qa.docker_runner import run_in_project_container
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("qa/functional_tester", "analyze")

# Commands to discover and run the test suite, tried in order.
_TEST_COMMANDS = [
    "python -m pytest --tb=short -q 2>&1 | head -100",
    "npm test -- --watchAll=false 2>&1 | head -100",
    "cargo test 2>&1 | head -100",
    "go test ./... 2>&1 | head -100",
    "mvn test -q 2>&1 | head -100",
]


def _run_tests(request: QARequest) -> str:
    """Attempt to run the test suite. Returns raw output or a sentinel."""
    for cmd in _TEST_COMMANDS:
        result = run_in_project_container(
            project_id=request.project_id,
            command=cmd,
            cwd=request.source_path,
            timeout_seconds=180,
        )
        if not result.available:
            return "unavailable"
        if result.succeeded or (
            "passed" in result.stdout.lower() or "test" in result.stdout.lower()
        ):
            combined = (result.stdout + result.stderr)[:3000]
            return combined or "unavailable"
    return "unavailable"


def _to_finding_detail(item: QAFindingItem) -> QAFindingDetail:
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
    )


class FunctionalTesterAgent(BaseQAAgent):
    """Identifies functional defects via test execution and source analysis."""

    @property
    def name(self) -> str:
        return "functional_tester"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        source_block = source_reader.format_for_prompt(files)
        test_output = _run_tests(request)

        prompt_loader.validate_builder_inputs(
            "qa/functional_tester",
            "analyze",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
                "test_output": test_output,
            },
        )

        user_prompt = f"""project_goal: {request.project_goal}
product_type: {request.product_type}

Source files:
{source_block}

Test suite output:
{test_output}

Analyze for functional defects."""

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="functional_tester_output",
                json_schema=ProbeAnalysisOutput.model_json_schema(),
            )
            output = ProbeAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("functional_tester_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="functional_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes=f"LLM call failed: {exc}",
                )
            )
            return session

        for item in output.findings:
            session.add_finding(_to_finding_detail(item))

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="functional_analysis",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.findings),
                notes=output.probe_summary,
            )
        )

        logger.info(
            "functional_tester_complete project_id=%s findings=%s outcome=%s",
            request.project_id,
            len(output.findings),
            output.probe_outcome,
        )
        return session
