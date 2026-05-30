"""
Supervisor evaluator for the execution_sequencer agent.

One LLM evaluation call per plan_version found in the planning_trace.jsonl.
Aggregates all per-version results into a single AgentEvaluationOutput.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.execution_run import ExecutionRun
from app.models.task import (
    TASK_STATUS_FAILED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
    Task,
)
from app.services.llm.factory import get_llm_provider
from app.services.project_storage import ProjectStorageService
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.prompt_resolver import resolve_system_prompt
from app.services.supervisor.trace_writer import PLANNING_TRACE_FILENAME

logger = logging.getLogger(__name__)

SEQUENCER_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("execution_sequencer_evaluator")

_NON_COMPLETED_STATUSES = {
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
    TASK_STATUS_FOLLOWED_UP,
}

_storage = ProjectStorageService()


def _load_sequencer_trace_entries(project_id: int) -> list[dict]:
    """Return all execution_sequencer entries from planning_trace.jsonl, in order."""
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME

    if not trace_path.exists():
        return []

    entries: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") == "execution_sequencer":
                entries.append(entry)
        except json.JSONDecodeError:
            logger.warning(
                "execution_sequencer_evaluator: invalid JSONL line for project %s", project_id
            )
    return entries


def _get_failure_info_for_task(db: Session, task: Task) -> dict | None:
    run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task.id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    if run is None:
        return None
    return {
        "run_id": run.id,
        "run_status": run.status,
        "failure_type": run.failure_type,
        "failure_code": run.failure_code,
        "remaining_scope": run.remaining_scope,
        "blockers_found": run.blockers_found,
        "validation_notes": run.validation_notes,
    }


def _build_task_outcomes(db: Session, project_id: int, task_ids: list[int]) -> list[dict]:
    """Build per-task outcome context for all tasks referenced in a plan."""
    tasks = db.query(Task).filter(Task.project_id == project_id, Task.id.in_(task_ids)).all()
    task_map = {t.id: t for t in tasks}

    result: list[dict] = []
    for task_id in task_ids:
        task = task_map.get(task_id)
        if task is None:
            result.append({"task_id": task_id, "title": "(not found)", "status": "unknown"})
            continue

        entry: dict = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
        }
        if task.status in _NON_COMPLETED_STATUSES:
            failure_info = _get_failure_info_for_task(db, task)
            if failure_info:
                entry["failure_info"] = failure_info
        result.append(entry)
    return result


def _extract_all_task_ids(trace_entry: dict) -> list[int]:
    """Extract all task IDs referenced in a plan's execution_batches."""
    output_snapshot = trace_entry.get("output_snapshot", {})
    execution_batches = output_snapshot.get("execution_batches", [])
    task_ids: list[int] = []
    seen: set[int] = set()
    for batch in execution_batches:
        for tid in batch.get("task_ids", []):
            if tid not in seen:
                task_ids.append(int(tid))
                seen.add(int(tid))
    return task_ids


def _build_per_version_user_prompt(
    *,
    project_id: int,
    trace_entry: dict,
    system_prompt: str,
    task_outcomes: list[dict],
    previous_plan_snapshot: dict | None,
) -> str:
    plan_version = trace_entry.get("plan_version")
    supersedes = trace_entry.get("supersedes_plan_version")
    call_type = trace_entry.get("call_type", "initial")

    prompt_loader.validate_builder_inputs(
        "execution_sequencer_evaluator",
        "main",
        {
            "project_id": project_id,
            "plan_version": plan_version,
            "supersedes_plan_version": supersedes,
            "call_type": call_type,
            "system_prompt": system_prompt,
            "trace_entry": trace_entry,
            "task_outcomes": task_outcomes,
            "previous_plan_snapshot": previous_plan_snapshot,
        },
    )

    previous_section = ""
    if previous_plan_snapshot:
        previous_section = f"""
Previous plan structure (plan_version={supersedes}) — what was in place before this resequence:
{json.dumps(previous_plan_snapshot, ensure_ascii=False, indent=2)}
"""

    return f"""
Evaluate the execution_sequencer agent's plan for plan_version={plan_version}.

Project ID: {project_id}
Plan version: {plan_version}
Supersedes plan version: {supersedes}
Call type: {call_type}

System prompt that was active during sequencing:
---
{system_prompt}
---
{previous_section}
Full planning trace entry (includes input_snapshot with candidate tasks and output_snapshot with the generated plan):
{json.dumps(trace_entry, ensure_ascii=False, indent=2)}

Task outcomes (status and failure info for each task in this plan):
{json.dumps(task_outcomes, ensure_ascii=False, indent=2)}

Instructions:
- Evaluate the ordering logic against the candidate tasks' metadata (task_type, dependencies, etc.).
- Assess batch groupings and checkpoint quality.
- For non-completed tasks, assess whether ordering contributed to the failure.
- If this is a resequence (call_type=resequence), compare the new batch structure against the previous plan:
  assess which batches changed, why they were restructured, and whether the resequencing correctly addressed the failures.
- Reference specific batch names and task titles in your findings.
""".strip()


def _build_retry_prompt(*, project_id: int, plan_version: int | None, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "execution_sequencer_evaluator",
        "retry",
        {
            "project_id": project_id,
            "plan_version": plan_version,
            "validation_error": validation_error,
        },
    )
    return f"""
Your previous response for project_id={project_id}, plan_version={plan_version} was invalid.

Validation error:
{validation_error}

Correct the output and return valid JSON matching the schema.
Required fields: verdict (healthy|needs_attention|degraded), findings (string, min 20 chars), issues (list of strings), suggestions (list of strings).
""".strip()


def _evaluate_one_version(
    *,
    db: Session,
    project_id: int,
    trace_entry: dict,
    system_prompt: str,
    provider,
    previous_trace_entry: dict | None = None,
) -> AgentEvaluationOutput:
    task_ids = _extract_all_task_ids(trace_entry)
    task_outcomes = _build_task_outcomes(db, project_id, task_ids)

    previous_plan_snapshot: dict | None = None
    if previous_trace_entry is not None:
        previous_plan_snapshot = previous_trace_entry.get("output_snapshot") or None

    user_prompt = _build_per_version_user_prompt(
        project_id=project_id,
        trace_entry=trace_entry,
        system_prompt=system_prompt,
        task_outcomes=task_outcomes,
        previous_plan_snapshot=previous_plan_snapshot,
    )

    raw = provider.generate_structured(
        system_prompt=SEQUENCER_EVALUATOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="execution_sequencer_evaluator_output",
        json_schema=AgentEvaluationOutput.model_json_schema(),
    )

    try:
        return AgentEvaluationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_prompt = _build_retry_prompt(
            project_id=project_id,
            plan_version=trace_entry.get("plan_version"),
            validation_error=str(exc),
        )
        raw_retry = provider.generate_structured(
            system_prompt=SEQUENCER_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            schema_name="execution_sequencer_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )
        return AgentEvaluationOutput.model_validate(raw_retry)


def _aggregate_version_results(
    results: list[AgentEvaluationOutput],
    plan_versions: list[int | None],
) -> AgentEvaluationOutput:
    if not results:
        return AgentEvaluationOutput(
            verdict="healthy",
            findings="No sequencer trace entries found for this project.",
            issues=[],
            suggestions=[],
        )

    verdict_rank = {"healthy": 0, "needs_attention": 1, "degraded": 2}
    worst_verdict = max(results, key=lambda r: verdict_rank[r.verdict]).verdict

    findings_parts: list[str] = []
    for result, version in zip(results, plan_versions, strict=True):
        label = f"plan_version={version}" if version is not None else "unknown version"
        findings_parts.append(f"[{label}] {result.findings}")

    all_issues: list[str] = []
    for result, version in zip(results, plan_versions, strict=True):
        label = f"v{version}" if version is not None else "vX"
        for issue in result.issues:
            all_issues.append(f"[{label}] {issue}")

    all_suggestions: list[str] = []
    seen_suggestions: set[str] = set()
    for result in results:
        for suggestion in result.suggestions:
            if suggestion not in seen_suggestions:
                all_suggestions.append(suggestion)
                seen_suggestions.add(suggestion)

    return AgentEvaluationOutput(
        verdict=worst_verdict,
        findings="\n\n".join(findings_parts),
        issues=all_issues,
        suggestions=all_suggestions,
    )


class ExecutionSequencerEvaluator:
    """Evaluates the execution_sequencer agent for a project."""

    AGENT_NAME = "execution_sequencer"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        trace_entries = _load_sequencer_trace_entries(project_id)

        if not trace_entries:
            return EvaluatorOutput(result=None)

        system_prompt = resolve_system_prompt(
            "execution_sequencer",
            system_version=system_version,
        )
        provider = get_llm_provider()

        per_version_results: list[AgentEvaluationOutput] = []
        plan_versions: list[int | None] = []

        for i, entry in enumerate(trace_entries):
            plan_version = entry.get("plan_version")
            plan_versions.append(plan_version)
            previous_entry = trace_entries[i - 1] if i > 0 else None
            try:
                result = _evaluate_one_version(
                    db=db,
                    project_id=project_id,
                    trace_entry=entry,
                    system_prompt=system_prompt,
                    provider=provider,
                    previous_trace_entry=previous_entry,
                )
                per_version_results.append(result)
            except Exception:
                logger.exception(
                    "execution_sequencer_evaluator: failed to evaluate plan_version %s",
                    plan_version,
                )
                per_version_results.append(
                    AgentEvaluationOutput(
                        verdict="needs_attention",
                        findings=f"Evaluation failed for plan_version={plan_version} due to an internal error.",
                        issues=["Evaluation error — manual review needed for this plan version."],
                        suggestions=[],
                    )
                )

        return EvaluatorOutput(
            result=_aggregate_version_results(per_version_results, plan_versions)
        )
