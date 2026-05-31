"""
Supervisor evaluator for the atomic_task_generator agent.

One LLM evaluation call per parent (high-level) task.  Aggregates all
per-parent results into a single AgentEvaluationOutput for the project.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.execution_run import ExecutionRun
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_FAILED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
    Task,
    format_acceptance_criteria,
)
from app.services.llm.factory import get_llm_provider
from app.services.project_storage import ProjectStorageService
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.prompt_resolver import resolve_system_prompt
from app.services.supervisor.trace_writer import PLANNING_TRACE_FILENAME

logger = logging.getLogger(__name__)

ATOMIC_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("atomic_task_generator_evaluator")

_NON_COMPLETED_STATUSES = {
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
    TASK_STATUS_FOLLOWED_UP,
}

_storage = ProjectStorageService()


def _load_atomic_trace_entries(project_id: int) -> dict[int, dict]:
    """Return a mapping of {parent_task_id: trace_entry} from planning_trace.jsonl."""
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME

    if not trace_path.exists():
        return {}

    result: dict[int, dict] = {}
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") != "atomic_task_generator":
                continue
            parent_id = entry.get("inputs", {}).get("parent_task_id")
            if parent_id is not None:
                # Later entries (reatomize calls) overwrite earlier ones per parent
                result[int(parent_id)] = entry
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "atomic_task_generator_evaluator: invalid JSONL line for project %s", project_id
            )
    return result


def _get_failure_info_for_task(db: Session, task: Task) -> dict | None:
    """Return the most recent ExecutionRun failure details for a non-completed task."""
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
        "work_summary": run.work_summary,
    }


def _build_atomic_tasks_context(db: Session, parent_task: Task) -> list[dict]:
    """Build the atomic_tasks context list for a single parent task."""
    children = (
        db.query(Task)
        .filter(
            Task.project_id == parent_task.project_id,
            Task.parent_task_id == parent_task.id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .order_by(Task.sequence_order)
        .all()
    )

    result: list[dict] = []
    for child in children:
        entry: dict = {
            "task_id": child.id,
            "title": child.title,
            "status": child.status,
            "is_recovery_task": child.is_recovery_task,
            "followup_depth": child.followup_depth,
            "estimated_complexity": child.estimated_complexity,
            "depends_on_task_titles": child.depends_on_task_titles or [],
        }
        if child.status in _NON_COMPLETED_STATUSES:
            failure_info = _get_failure_info_for_task(db, child)
            if failure_info:
                entry["failure_info"] = failure_info
        result.append(entry)
    return result


def _build_per_parent_user_prompt(
    *,
    project_id: int,
    parent_task: Task,
    system_prompt: str,
    trace_entry: dict | None,
    atomic_tasks: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "atomic_task_generator_evaluator",
        "main",
        {
            "project_id": project_id,
            "parent_task_id": parent_task.id,
            "parent_task_title": parent_task.title,
            "parent_task_acceptance_criteria": format_acceptance_criteria(
                parent_task.acceptance_criteria
            ),
            "system_prompt": system_prompt,
            "trace_entry": trace_entry or {},
            "atomic_tasks": atomic_tasks,
        },
    )
    return f"""
Evaluate the atomic_task_generator agent's decomposition for this parent task.

Project ID: {project_id}

Parent task:
- task_id: {parent_task.id}
- title: {parent_task.title}
- acceptance_criteria:
{format_acceptance_criteria(parent_task.acceptance_criteria) or "(not set)"}
- description: {parent_task.description or "(not set)"}
- objective: {parent_task.objective or "(not set)"}

System prompt that was active during decomposition:
---
{system_prompt}
---

Planning trace entry for this decomposition call:
{json.dumps(trace_entry, ensure_ascii=False, indent=2) if trace_entry else "(no trace entry found)"}

Atomic tasks produced (with outcomes):
{json.dumps(atomic_tasks, ensure_ascii=False, indent=2)}

Instructions:
- Assess whether the atomic tasks collectively cover the parent's acceptance criteria scope.
- Assess granularity: are tasks appropriately sized (one deliverable, one validation boundary)?
- For non-completed tasks, use the failure_info to assess whether the failure is attributable to task definition quality or other factors.
- Reference specific task titles and failure details in your findings.
""".strip()


def _build_retry_prompt(*, project_id: int, parent_task_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "atomic_task_generator_evaluator",
        "retry",
        {
            "project_id": project_id,
            "parent_task_id": parent_task_id,
            "validation_error": validation_error,
        },
    )
    return f"""
Your previous response for project_id={project_id}, parent_task_id={parent_task_id} was invalid.

Validation error:
{validation_error}

Correct the output and return valid JSON matching the schema.
Required fields: verdict (healthy|needs_attention|degraded), findings (string, min 20 chars), issues (list of strings), suggestions (list of strings).
""".strip()


def _evaluate_one_parent(
    *,
    db: Session,
    project_id: int,
    parent_task: Task,
    system_prompt: str,
    trace_entries: dict[int, dict],
    provider,
) -> AgentEvaluationOutput:
    trace_entry = trace_entries.get(parent_task.id)
    atomic_tasks = _build_atomic_tasks_context(db, parent_task)

    user_prompt = _build_per_parent_user_prompt(
        project_id=project_id,
        parent_task=parent_task,
        system_prompt=system_prompt,
        trace_entry=trace_entry,
        atomic_tasks=atomic_tasks,
    )

    raw = provider.generate_structured(
        system_prompt=ATOMIC_EVALUATOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="atomic_task_generator_evaluator_output",
        json_schema=AgentEvaluationOutput.model_json_schema(),
    )

    try:
        return AgentEvaluationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_prompt = _build_retry_prompt(
            project_id=project_id,
            parent_task_id=parent_task.id,
            validation_error=str(exc),
        )
        raw_retry = provider.generate_structured(
            system_prompt=ATOMIC_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            schema_name="atomic_task_generator_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )
        return AgentEvaluationOutput.model_validate(raw_retry)


def _aggregate_per_parent_results(
    results: list[AgentEvaluationOutput],
    parent_titles: list[str],
) -> AgentEvaluationOutput:
    """Merge per-parent evaluations into a single project-level evaluation."""
    if not results:
        return AgentEvaluationOutput(
            verdict="healthy",
            findings="No parent tasks found to evaluate.",
            issues=[],
            suggestions=[],
        )

    # Worst verdict wins
    verdict_rank = {"healthy": 0, "needs_attention": 1, "degraded": 2}
    worst_verdict = max(results, key=lambda r: verdict_rank[r.verdict]).verdict

    # Combine findings, prefixed with the parent task context
    findings_parts: list[str] = []
    for result, title in zip(results, parent_titles, strict=True):
        findings_parts.append(f"[{title}] {result.findings}")

    all_issues: list[str] = []
    for result, title in zip(results, parent_titles, strict=True):
        for issue in result.issues:
            all_issues.append(f"[{title}] {issue}")

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


class AtomicTaskGeneratorEvaluator:
    """Evaluates the atomic_task_generator agent for a project."""

    AGENT_NAME = "atomic_task_generator"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        # High-level parent tasks only
        parent_tasks = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.parent_task_id.is_(None),
            )
            .order_by(Task.sequence_order)
            .all()
        )

        if not parent_tasks:
            return EvaluatorOutput(result=None)

        trace_entries = _load_atomic_trace_entries(project_id)
        system_prompt = resolve_system_prompt(
            "atomic_task_generator",
            system_version=system_version,
        )
        provider = get_llm_provider()

        per_parent_results: list[AgentEvaluationOutput] = []
        for parent_task in parent_tasks:
            try:
                result = _evaluate_one_parent(
                    db=db,
                    project_id=project_id,
                    parent_task=parent_task,
                    system_prompt=system_prompt,
                    trace_entries=trace_entries,
                    provider=provider,
                )
                per_parent_results.append(result)
            except Exception:
                logger.exception(
                    "atomic_task_generator_evaluator: failed to evaluate parent_task %s",
                    parent_task.id,
                )
                per_parent_results.append(
                    AgentEvaluationOutput(
                        verdict="needs_attention",
                        findings=f"Evaluation failed for parent task '{parent_task.title}' due to an internal error.",
                        issues=["Evaluation error — manual review needed for this parent task."],
                        suggestions=[],
                    )
                )

        return EvaluatorOutput(
            result=_aggregate_per_parent_results(
                per_parent_results,
                parent_titles=[t.title for t in parent_tasks],
            )
        )
