"""Performance QA Agent — deterministic thresholds + timing measurements.

Runs timing commands in the project's Docker container and compares the
results against product-type-specific thresholds.  Threshold violations are
reported as findings without an LLM call — the verdict per metric is
deterministic.  An optional LLM pass adds context-aware findings (e.g.,
explaining *why* a threshold was breached based on source code analysis).

Thresholds are conservative defaults; operators can override via configuration
once baseline measurements are established.
"""

from __future__ import annotations

import logging
import re

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

_SYSTEM_PROMPT = prompt_loader.get("qa/performance_qa_agent", "analyze_performance")

# ── Thresholds (seconds unless stated) ────────────────────────────────────────

_THRESHOLDS: dict[str, dict[str, float]] = {
    "rest_api": {"startup_seconds": 10.0, "response_ms": 500.0},
    "graphql_api": {"startup_seconds": 10.0, "response_ms": 500.0},
    "web_app": {"startup_seconds": 15.0, "page_load_ms": 3000.0},
    "cli_tool": {"execution_seconds": 30.0},
    "library": {"import_ms": 500.0},
    "mobile_android": {"startup_seconds": 3.0},
    "desktop_app": {"startup_seconds": 5.0},
}

# ── Timing commands ────────────────────────────────────────────────────────────

_STARTUP_COMMANDS: dict[str, str] = {
    "rest_api": "time python -c 'import importlib; importlib.import_module(\"app\")' 2>&1",
    "graphql_api": "time python -c 'import importlib; importlib.import_module(\"app\")' 2>&1",
    "cli_tool": "time python -m pytest --collect-only -q 2>&1 | head -5",
    "library": "time python -c 'import importlib, sys; [importlib.import_module(m) for m in sys.modules]' 2>&1",
    "web_app": "time node -e 'require(\"./app\")' 2>&1",
}

_DEFAULT_TIMING_CMD = "time echo 'timing_probe' 2>&1"


def _collect_timing(request: QARequest) -> tuple[str, float | None]:
    """Run a timing command; return (raw_output, measured_seconds_or_None)."""
    cmd = _STARTUP_COMMANDS.get(request.product_type, _DEFAULT_TIMING_CMD)
    result = run_in_project_container(
        project_id=request.project_id,
        command=cmd,
        cwd=request.source_path,
        timeout_seconds=60,
    )
    if not result.available:
        return "unavailable", None

    raw = (result.stdout + result.stderr)[:2000] or "unavailable"

    # Try to extract elapsed seconds from "real 0m1.234s" or "0.123 real" patterns
    for pattern in [
        r"real\s+(\d+)m([\d.]+)s",  # bash time: real 0m1.234s
        r"([\d.]+)\s+real",  # zsh time: 0.123 real
        r"Elapsed:\s+([\d.]+)s",
    ]:
        m = re.search(pattern, raw)
        if m:
            try:
                if len(m.groups()) == 2:
                    secs = int(m.group(1)) * 60 + float(m.group(2))
                else:
                    secs = float(m.group(1))
                return raw, secs
            except ValueError:
                pass

    return raw, None


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


class PerformanceQAAgent(BaseQAAgent):
    """Deterministic threshold checks + optional LLM source analysis."""

    @property
    def name(self) -> str:
        return "performance_qa_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        thresholds = _THRESHOLDS.get(request.product_type, {})
        timing_output, measured_seconds = _collect_timing(request)

        # ── Deterministic threshold check ──────────────────────────────────────
        threshold_findings: list[QAFindingDetail] = []

        if measured_seconds is not None and "startup_seconds" in thresholds:
            limit = thresholds["startup_seconds"]
            if measured_seconds > limit:
                threshold_findings.append(
                    QAFindingDetail(
                        finding_id="pqa-T001",
                        severity="high" if measured_seconds > limit * 2 else "medium",
                        category="performance",
                        title=f"Startup time exceeds threshold: {measured_seconds:.2f}s > {limit}s",
                        description=(
                            f"Application took {measured_seconds:.2f}s to start, "
                            f"exceeding the {limit}s threshold for {request.product_type}."
                        ),
                        reproduction_steps=[
                            "Measure startup time with: "
                            + _STARTUP_COMMANDS.get(request.product_type, _DEFAULT_TIMING_CMD)
                        ],
                        auto_remediable=False,
                        remediation_hint="Profile startup to identify slow imports or initialization code.",
                        producer_agent=self.name,
                    )
                )

        for finding in threshold_findings:
            session.add_finding(finding)

        # ── Optional LLM source analysis ───────────────────────────────────────
        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        if files:
            import json

            source_block = source_reader.format_for_prompt(files)
            thresholds_json = json.dumps(thresholds, ensure_ascii=False)

            prompt_loader.validate_builder_inputs(
                "qa/performance_qa_agent",
                "analyze_performance",
                {
                    "project_goal": request.project_goal,
                    "product_type": request.product_type,
                    "source_files": source_block,
                    "timing_output": timing_output,
                    "thresholds_json": thresholds_json,
                },
            )

            user_prompt = (
                f"project_goal: {request.project_goal}\n"
                f"product_type: {request.product_type}\n\n"
                f"Source files:\n{source_block}\n\n"
                f"Timing output:\n{timing_output}\n\n"
                f"Thresholds: {thresholds_json}\n\n"
                "Identify performance bottlenecks in the source code."
            )

            try:
                provider = get_llm_provider()
                raw = provider.generate_structured(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    schema_name="performance_qa_agent_output",
                    json_schema=ProbeAnalysisOutput.model_json_schema(),
                )
                llm_output = ProbeAnalysisOutput.model_validate(raw)
                for item in llm_output.findings:
                    session.add_finding(_to_finding_detail(item, self.name))
                probe_outcome = llm_output.probe_outcome if not threshold_findings else "failed"
                probe_summary = llm_output.probe_summary
            except (ValidationError, Exception) as exc:
                logger.warning("performance_qa_agent_llm_failed exc=%s", exc)
                probe_outcome = "failed" if threshold_findings else "passed"
                probe_summary = f"LLM analysis skipped: {exc}"
        else:
            probe_outcome = "failed" if threshold_findings else "skipped"
            probe_summary = "No source files; only timing measurements performed."

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="performance_analysis",
                target=request.source_path,
                outcome=probe_outcome,
                findings_count=session.total_findings,
                notes=(
                    f"Measured: {measured_seconds:.2f}s. "
                    f"Threshold findings: {len(threshold_findings)}. {probe_summary}"
                    if measured_seconds is not None
                    else f"Timing unavailable. {probe_summary}"
                ),
            )
        )

        logger.info(
            "performance_qa_agent_complete project_id=%s measured=%.2fs threshold_findings=%s",
            request.project_id,
            measured_seconds or 0.0,
            len(threshold_findings),
        )
        return session
