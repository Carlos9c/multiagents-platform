"""Performance profiler QA agent.

Combines Docker-based timing measurements with LLM source analysis to
identify performance bottlenecks.
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

_SYSTEM_PROMPT = prompt_loader.get("qa/performance_profiler", "analyze")

# Simple timing commands, tried in order.
_TIMING_COMMANDS = [
    "python -m timeit -n 1 -r 1 'import time; time.sleep(0)' 2>&1",
    "time echo ok 2>&1",
]


def _collect_timing(request: QARequest) -> str:
    """Attempt basic timing measurements. Returns output or sentinel."""
    for cmd in _TIMING_COMMANDS:
        result = run_in_project_container(
            project_id=request.project_id,
            command=cmd,
            cwd=request.source_path,
            timeout_seconds=30,
        )
        if not result.available:
            return "unavailable"
        if result.succeeded:
            combined = (result.stdout + result.stderr)[:2000]
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


class PerformanceProfilerAgent(BaseQAAgent):
    """Identifies performance bottlenecks via source analysis and timing measurements."""

    @property
    def name(self) -> str:
        return "performance_profiler"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        source_block = source_reader.format_for_prompt(files)
        timing_output = _collect_timing(request)

        prompt_loader.validate_builder_inputs(
            "qa/performance_profiler",
            "analyze",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
                "timing_output": timing_output,
            },
        )

        user_prompt = f"""project_goal: {request.project_goal}
product_type: {request.product_type}

Source files:
{source_block}

Timing measurements:
{timing_output}

Identify performance bottlenecks."""

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="performance_profiler_output",
                json_schema=ProbeAnalysisOutput.model_json_schema(),
            )
            output = ProbeAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("performance_profiler_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="performance_analysis",
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
                probe_type="performance_analysis",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.findings),
                notes=output.probe_summary,
            )
        )

        logger.info(
            "performance_profiler_complete project_id=%s findings=%s outcome=%s",
            request.project_id,
            len(output.findings),
            output.probe_outcome,
        )
        return session
