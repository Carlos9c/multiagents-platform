"""
Builds the two inputs to the aggregate LLM call:
  1. Per-agent frequency table (verdict percentages across all selected projects).
  2. Text corpus (findings + issues from non-healthy agents, ordered by severity).
"""

from __future__ import annotations

import logging

from app.models.agent_evaluation import AgentEvaluation
from app.models.supervisor_report import SupervisorReport

logger = logging.getLogger(__name__)

# Hard limit on corpus characters before the LLM call — degraded evidence gets priority
_MAX_CORPUS_CHARS = 60_000

_AGENT_NAMES = [
    "planner",
    "atomic_task_generator",
    "execution_sequencer",
    "environment_planner",
    "catalog_selector",
    "orchestrator",
    "context_selection_agent",
    "environment_manager_agent",
    "code_change_agent",
    "code_change_agent_validator",
    "command_runner_agent",
    "command_runner_agent_validator",
    "test_builder_agent",
    "test_builder_agent_validator",
    "document_writer_agent",
    "document_writer_agent_validator",
    "stage_evaluator",
    "recovery_planner",
    "recovery_assignment",
    "requirements_evaluator",
    "review_episode",
    "aria_orchestrator",
]


def build_frequency_table(
    reports: list[SupervisorReport],
) -> list[dict]:
    """Return a list of per-agent verdict frequency dicts, sorted by degradation rate desc."""
    n = len(reports)
    if n == 0:
        return []

    # {agent_name: {verdict: count}}
    counts: dict[str, dict[str, int]] = {
        name: {"degraded": 0, "needs_attention": 0, "healthy": 0, "not_supervised": 0}
        for name in _AGENT_NAMES
    }

    for report in reports:
        evals_by_name: dict[str, AgentEvaluation] = {
            ev.agent_name: ev for ev in report.agent_evaluations
        }
        for agent_name in _AGENT_NAMES:
            ev = evals_by_name.get(agent_name)
            if ev is None or ev.verdict is None:
                counts[agent_name]["not_supervised"] += 1
            elif ev.verdict == "degraded":
                counts[agent_name]["degraded"] += 1
            elif ev.verdict == "needs_attention":
                counts[agent_name]["needs_attention"] += 1
            else:
                counts[agent_name]["healthy"] += 1

    table = []
    for agent_name in _AGENT_NAMES:
        c = counts[agent_name]
        total = n
        table.append(
            {
                "agent_name": agent_name,
                "total_projects": total,
                "degraded_pct": round(c["degraded"] / total * 100),
                "needs_attention_pct": round(c["needs_attention"] / total * 100),
                "healthy_pct": round(c["healthy"] / total * 100),
                "not_supervised_pct": round(c["not_supervised"] / total * 100),
                "degraded_count": c["degraded"],
                "needs_attention_count": c["needs_attention"],
            }
        )

    table.sort(key=lambda x: (x["degraded_pct"], x["needs_attention_pct"]), reverse=True)
    return table


def _format_report_block(report: SupervisorReport, verdict_filter: str) -> str:
    """Format one report's non-healthy evaluations into a text block."""
    lines: list[str] = []
    lines.append(
        f"--- Project {report.project_id} "
        f"(overall: {report.overall_verdict or 'unknown'}, "
        f"report_id: {report.report_id}) ---"
    )

    # Include the report's own synthesis (first 800 chars to save space)
    if report.synthesis:
        preview = report.synthesis[:800].strip()
        if len(report.synthesis) > 800:
            preview += "…"
        lines.append(f"Report synthesis preview:\n{preview}")

    # Per-agent evidence matching the verdict filter
    for ev in report.agent_evaluations:
        if ev.verdict != verdict_filter:
            continue
        lines.append(f"\n{ev.agent_name} [{ev.verdict}]:")
        if ev.findings:
            lines.append(f"  Findings: {ev.findings[:400]}")
        issues: list = ev.issues or []
        if issues:
            for issue in issues[:5]:
                lines.append(f"  - {issue}")

    return "\n".join(lines)


def build_text_corpus(reports: list[SupervisorReport]) -> str:
    """Build the text corpus passed to the LLM.

    Degraded evidence first (highest priority), then needs_attention.
    Truncated to _MAX_CORPUS_CHARS if needed.
    """
    # Sort reports: degraded overall first, then needs_attention, then healthy
    _rank = {"degraded": 0, "needs_attention": 1, "healthy": 2}
    sorted_reports = sorted(
        reports,
        key=lambda r: _rank.get(r.overall_verdict or "", 3),
    )

    blocks: list[str] = []

    # Pass 1: degraded agents
    for report in sorted_reports:
        if any(ev.verdict == "degraded" for ev in report.agent_evaluations):
            blocks.append(_format_report_block(report, "degraded"))

    # Pass 2: needs_attention agents
    for report in sorted_reports:
        block = _format_report_block(report, "needs_attention")
        # Only add if it has actual needs_attention content
        if "needs_attention]" in block:
            blocks.append(block)

    corpus = "\n\n".join(blocks)

    if len(corpus) > _MAX_CORPUS_CHARS:
        corpus = corpus[:_MAX_CORPUS_CHARS] + "\n\n[corpus truncated due to size]"

    return corpus
