import json
import re
from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import (
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Task,
)
from app.schemas.project_memory import (
    ProjectMemoryArtifactSummary,
    ProjectMemoryDecisionSignal,
    ProjectMemoryPathSignal,
    ProjectMemoryTaskSummary,
    ProjectOperationalContext,
)
from app.services.artifacts import create_artifact


PROJECT_OPERATIONAL_CONTEXT_ARTIFACT_TYPE = "project_operational_context"

_PATH_PATTERN = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+")
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")

_DECISION_ARTIFACT_TYPES = {
    "project_plan",
    "technical_refinement",
    "atomic_task_generation",
    "execution_plan",
    "recovery_decision",
    "evaluation_decision",
    "post_batch_result",
}

_RECENT_ARTIFACT_TYPES = {
    "project_plan",
    "technical_refinement",
    "atomic_task_generation",
    "execution_plan",
    "code_context_selection",
    "code_executor_context",
    "code_executor_working_set",
    "code_executor_edit_plan",
    "code_executor_result",
    "code_validation_result",
    "recovery_decision",
    "post_batch_result",
    "evaluation_decision",
}


class ProjectMemoryServiceError(Exception):
    """Base exception for project memory service errors."""


def _safe_json_loads(raw: str | None) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _truncate(text: str | None, limit: int = 400) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def _extract_paths_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return list(dict.fromkeys(item.strip() for item in _PATH_PATTERN.findall(text)))


def _artifact_summary_text(artifact: Artifact) -> str:
    payload = _safe_json_loads(artifact.content)

    if isinstance(payload, dict):
        candidates: list[str] = []

        for key in (
            "summary",
            "reason",
            "project_state_summary",
            "selection_rationale",
            "diff_summary",
            "work_summary",
            "output_snapshot",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

        if candidates:
            return _truncate(" | ".join(candidates), limit=500)

    return _truncate(artifact.content, limit=500)


def _extract_decision_signals_from_artifact(artifact: Artifact) -> list[str]:
    payload = _safe_json_loads(artifact.content)
    signals: list[str] = []

    if isinstance(payload, dict):
        for key in (
            "reason",
            "project_state_summary",
            "updated_execution_guidance",
            "selection_rationale",
            "summary",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                signals.append(value.strip())

        for key in ("detected_gaps", "notes", "decisions_taken", "local_decisions"):
            value = payload.get(key)
            if isinstance(value, list):
                signals.extend(str(item).strip() for item in value if str(item).strip())

    if not signals:
        raw = _truncate(artifact.content, limit=300)
        if raw:
            signals.append(raw)

    deduped: list[str] = []
    for item in signals:
        if item not in deduped:
            deduped.append(item)

    return deduped[:8]


def _extract_gap_signals_from_artifact(artifact: Artifact) -> list[str]:
    payload = _safe_json_loads(artifact.content)
    gaps: list[str] = []

    if isinstance(payload, dict):
        for key in (
            "detected_gaps",
            "unresolved_failures",
            "open_issues",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        summary = _first_non_empty(
                            item.get("why_it_still_matters"),
                            item.get("remaining_scope"),
                            item.get("blockers_found"),
                            item.get("covered_gap_summary"),
                        )
                        if summary:
                            gaps.append(summary)
                    elif isinstance(item, str) and item.strip():
                        gaps.append(item.strip())

        remaining_scope = payload.get("remaining_scope")
        if isinstance(remaining_scope, str) and remaining_scope.strip():
            gaps.append(remaining_scope.strip())

    deduped: list[str] = []
    for item in gaps:
        if item not in deduped:
            deduped.append(item)

    return deduped[:10]


def _extract_recovery_learnings_from_artifact(artifact: Artifact) -> list[str]:
    if artifact.artifact_type != "recovery_decision":
        return []

    payload = _safe_json_loads(artifact.content)
    learnings: list[str] = []

    if isinstance(payload, dict):
        reason = payload.get("reason")
        if isinstance(reason, str) and reason.strip():
            learnings.append(reason.strip())

        action = payload.get("action")
        if isinstance(action, str) and action.strip():
            learnings.append(f"Recovery action used: {action.strip()}")

        for key in ("covered_gap_summary", "validation_context_summary", "execution_context_summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                learnings.append(_truncate(value, limit=250))

    deduped: list[str] = []
    for item in learnings:
        if item not in deduped:
            deduped.append(item)

    return deduped[:6]


def _build_path_signals(
    tasks: list[Task],
    runs_by_task_id: dict[int, ExecutionRun],
    artifacts_by_task_id: dict[int | None, list[Artifact]],
    project_artifacts: list[Artifact],
    max_paths: int = 30,
) -> list[ProjectMemoryPathSignal]:
    counters: dict[str, ProjectMemoryPathSignal] = {}

    def ensure(path: str) -> ProjectMemoryPathSignal:
        if path not in counters:
            counters[path] = ProjectMemoryPathSignal(path=path)
        return counters[path]

    for task in tasks:
        task_texts = [
            task.title,
            task.description,
            task.summary,
            task.objective,
            task.proposed_solution,
            task.implementation_notes,
            task.implementation_steps,
            task.acceptance_criteria,
            task.tests_required,
            task.technical_constraints,
            task.out_of_scope,
        ]

        for text in task_texts:
            for path in _extract_paths_from_text(text):
                signal = ensure(path)
                signal.mention_count += 1
                if task.id not in signal.source_task_ids:
                    signal.source_task_ids.append(task.id)

        last_run = runs_by_task_id.get(task.id)
        if last_run:
            run_texts = [
                last_run.input_snapshot,
                last_run.output_snapshot,
                last_run.work_summary,
                last_run.work_details,
                last_run.artifacts_created,
                last_run.completed_scope,
                last_run.remaining_scope,
                last_run.blockers_found,
                last_run.validation_notes,
                last_run.error_message,
            ]
            for text in run_texts:
                for path in _extract_paths_from_text(text):
                    signal = ensure(path)
                    signal.mention_count += 1
                    if last_run.id not in signal.source_run_ids:
                        signal.source_run_ids.append(last_run.id)

        for artifact in artifacts_by_task_id.get(task.id, []):
            for path in _extract_paths_from_text(artifact.content):
                signal = ensure(path)
                signal.mention_count += 1
                if artifact.id not in signal.source_artifact_ids:
                    signal.source_artifact_ids.append(artifact.id)

    for artifact in project_artifacts:
        if artifact.task_id is None:
            for path in _extract_paths_from_text(artifact.content):
                signal = ensure(path)
                signal.mention_count += 1
                if artifact.id not in signal.source_artifact_ids:
                    signal.source_artifact_ids.append(artifact.id)

    ranked = sorted(
        counters.values(),
        key=lambda item: (
            item.mention_count,
            len(item.source_task_ids),
            len(item.source_artifact_ids),
            len(item.source_run_ids),
        ),
        reverse=True,
    )

    return ranked[:max_paths]


def _build_active_workstreams(tasks: list[Task], limit: int = 12) -> list[str]:
    seen: list[str] = []
    active_statuses = {
        TASK_STATUS_PENDING,
        TASK_STATUS_RUNNING,
        TASK_STATUS_AWAITING_VALIDATION,
        TASK_STATUS_PARTIAL,
    }

    for task in sorted(tasks, key=lambda item: item.id, reverse=True):
        if task.status not in active_statuses:
            continue

        signal = _first_non_empty(task.objective, task.summary, task.title)
        if not signal:
            continue

        signal = _truncate(signal, limit=180)
        if signal not in seen:
            seen.append(signal)

        if len(seen) >= limit:
            break

    return seen


def _build_task_memory(
    tasks: list[Task],
    runs_by_task_id: dict[int, ExecutionRun],
    artifacts_by_task_id: dict[int | None, list[Artifact]],
    limit: int = 40,
) -> list[ProjectMemoryTaskSummary]:
    ranked_tasks = sorted(tasks, key=lambda item: item.id, reverse=True)[:limit]
    memory: list[ProjectMemoryTaskSummary] = []

    for task in ranked_tasks:
        last_run = runs_by_task_id.get(task.id)
        task_artifacts = artifacts_by_task_id.get(task.id, [])

        artifact_types: list[str] = []
        for artifact in task_artifacts:
            if artifact.artifact_type not in artifact_types:
                artifact_types.append(artifact.artifact_type)

        memory.append(
            ProjectMemoryTaskSummary(
                task_id=task.id,
                parent_task_id=task.parent_task_id,
                title=task.title,
                planning_level=task.planning_level,
                status=task.status,
                priority=task.priority,
                task_type=task.task_type,
                objective=task.objective,
                last_run_id=last_run.id if last_run else None,
                last_run_status=last_run.status if last_run else None,
                work_summary=last_run.work_summary if last_run else None,
                completed_scope=last_run.completed_scope if last_run else None,
                remaining_scope=last_run.remaining_scope if last_run else None,
                blockers_found=last_run.blockers_found if last_run else None,
                validation_notes=last_run.validation_notes if last_run else None,
                artifact_types=artifact_types[:12],
            )
        )

    return memory


def _build_recent_completed_work(
    tasks: list[Task],
    runs_by_task_id: dict[int, ExecutionRun],
    limit: int = 12,
) -> list[str]:
    items: list[str] = []

    for task in sorted(tasks, key=lambda item: item.id, reverse=True):
        if task.status not in {TASK_STATUS_COMPLETED, TASK_STATUS_PARTIAL}:
            continue

        last_run = runs_by_task_id.get(task.id)
        summary = _first_non_empty(
            last_run.work_summary if last_run else None,
            last_run.completed_scope if last_run else None,
            task.objective,
            task.title,
        )
        if not summary:
            continue

        entry = f"Task {task.id} ({task.title}): {_truncate(summary, limit=220)}"
        if entry not in items:
            items.append(entry)

        if len(items) >= limit:
            break

    return items


def _build_recent_failure_learnings(
    runs: list[ExecutionRun],
    tasks_by_id: dict[int, Task],
    limit: int = 12,
) -> list[str]:
    items: list[str] = []

    for run in sorted(runs, key=lambda item: item.id, reverse=True):
        if run.status not in {"failed", "rejected", "partial"}:
            continue

        task = tasks_by_id.get(run.task_id)
        task_title = task.title if task else f"task {run.task_id}"

        parts: list[str] = [f"Run {run.id} for {task_title} ended as {run.status}"]

        if run.failure_type:
            parts.append(f"failure_type={run.failure_type}")
        if run.failure_code:
            parts.append(f"failure_code={run.failure_code}")

        blocker = _first_non_empty(run.blockers_found, run.remaining_scope, run.error_message)
        if blocker:
            parts.append(_truncate(blocker, limit=180))

        entry = " | ".join(parts)
        if entry not in items:
            items.append(entry)

        if len(items) >= limit:
            break

    return items


def _build_validation_learnings(
    runs: list[ExecutionRun],
    tasks_by_id: dict[int, Task],
    limit: int = 12,
) -> list[str]:
    items: list[str] = []

    for run in sorted(runs, key=lambda item: item.id, reverse=True):
        if not run.validation_notes or not run.validation_notes.strip():
            continue

        task = tasks_by_id.get(run.task_id)
        task_title = task.title if task else f"task {run.task_id}"
        entry = f"Validation note from run {run.id} ({task_title}): {_truncate(run.validation_notes, 220)}"

        if entry not in items:
            items.append(entry)

        if len(items) >= limit:
            break

    return items


def _build_recovery_learnings(
    project_artifacts: list[Artifact],
    limit: int = 12,
) -> list[str]:
    items: list[str] = []

    for artifact in sorted(project_artifacts, key=lambda item: item.id, reverse=True):
        for learning in _extract_recovery_learnings_from_artifact(artifact):
            if learning not in items:
                items.append(learning)

            if len(items) >= limit:
                return items

    return items


def _build_key_decisions(
    project_artifacts: list[Artifact],
    limit: int = 20,
) -> list[ProjectMemoryDecisionSignal]:
    decisions: list[ProjectMemoryDecisionSignal] = []

    for artifact in sorted(project_artifacts, key=lambda item: item.id, reverse=True):
        if artifact.artifact_type not in _DECISION_ARTIFACT_TYPES:
            continue

        for signal in _extract_decision_signals_from_artifact(artifact):
            decisions.append(
                ProjectMemoryDecisionSignal(
                    source_type=artifact.artifact_type,
                    source_id=artifact.id,
                    task_id=artifact.task_id,
                    summary=_truncate(signal, limit=300),
                )
            )
            if len(decisions) >= limit:
                return decisions

    return decisions


def _build_recent_artifact_summaries(
    project_artifacts: list[Artifact],
    limit: int = 20,
) -> list[ProjectMemoryArtifactSummary]:
    summaries: list[ProjectMemoryArtifactSummary] = []

    for artifact in sorted(project_artifacts, key=lambda item: item.id, reverse=True):
        if artifact.artifact_type not in _RECENT_ARTIFACT_TYPES:
            continue

        summaries.append(
            ProjectMemoryArtifactSummary(
                artifact_id=artifact.id,
                artifact_type=artifact.artifact_type,
                task_id=artifact.task_id,
                summary=_artifact_summary_text(artifact),
            )
        )

        if len(summaries) >= limit:
            break

    return summaries


def _build_open_gaps(
    tasks: list[Task],
    runs_by_task_id: dict[int, ExecutionRun],
    project_artifacts: list[Artifact],
    limit: int = 20,
) -> list[str]:
    gaps: list[str] = []

    for task in sorted(tasks, key=lambda item: item.id, reverse=True):
        if task.is_blocked and task.blocking_reason:
            entry = f"Task {task.id} blocked: {_truncate(task.blocking_reason, 220)}"
            if entry not in gaps:
                gaps.append(entry)

        if task.status in {TASK_STATUS_FAILED, TASK_STATUS_PARTIAL}:
            last_run = runs_by_task_id.get(task.id)
            summary = _first_non_empty(
                last_run.remaining_scope if last_run else None,
                last_run.blockers_found if last_run else None,
                last_run.error_message if last_run else None,
            )
            if summary:
                entry = f"Task {task.id} unresolved: {_truncate(summary, 220)}"
                if entry not in gaps:
                    gaps.append(entry)

        if len(gaps) >= limit:
            return gaps

    for artifact in sorted(project_artifacts, key=lambda item: item.id, reverse=True):
        for gap in _extract_gap_signals_from_artifact(artifact):
            if gap not in gaps:
                gaps.append(_truncate(gap, 220))

            if len(gaps) >= limit:
                return gaps

    return gaps


def _build_context_summary(
    *,
    project: Project,
    total_tasks: int,
    pending_task_ids: list[int],
    active_task_ids: list[int],
    completed_task_ids: list[int],
    failed_task_ids: list[int],
    blocked_task_ids: list[int],
    active_workstreams: list[str],
    recent_completed_work: list[str],
    open_gaps: list[str],
    referenced_paths: list[ProjectMemoryPathSignal],
) -> str:
    top_paths = ", ".join(item.path for item in referenced_paths[:8]) or "none"
    active_streams = "; ".join(active_workstreams[:5]) or "none"
    completed = "; ".join(recent_completed_work[:4]) or "none"
    gaps = "; ".join(open_gaps[:4]) or "none"

    return (
        f"Project operational context for '{project.name}'. "
        f"Goal: {project.description or project.name}. "
        f"Total tasks={total_tasks}, pending={len(pending_task_ids)}, "
        f"active={len(active_task_ids)}, completed={len(completed_task_ids)}, "
        f"failed={len(failed_task_ids)}, blocked={len(blocked_task_ids)}. "
        f"Active workstreams: {active_streams}. "
        f"Recent completed work: {completed}. "
        f"Open gaps: {gaps}. "
        f"Frequently referenced paths: {top_paths}."
    )


def build_project_operational_context(
    db: Session,
    project_id: int,
) -> ProjectOperationalContext:
    project = db.get(Project, project_id)
    if not project:
        raise ProjectMemoryServiceError(f"Project {project_id} not found")

    tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .order_by(Task.id.asc())
        .all()
    )

    task_ids = [task.id for task in tasks]

    runs = []
    if task_ids:
        runs = (
            db.query(ExecutionRun)
            .filter(ExecutionRun.task_id.in_(task_ids))
            .order_by(ExecutionRun.id.asc())
            .all()
        )

    project_artifacts = (
        db.query(Artifact)
        .filter(Artifact.project_id == project_id)
        .order_by(Artifact.id.asc())
        .all()
    )

    tasks_by_id = {task.id: task for task in tasks}

    runs_by_task_id: dict[int, ExecutionRun] = {}
    for run in runs:
        runs_by_task_id[run.task_id] = run

    artifacts_by_task_id: dict[int | None, list[Artifact]] = defaultdict(list)
    for artifact in project_artifacts:
        artifacts_by_task_id[artifact.task_id].append(artifact)

    pending_task_ids = [task.id for task in tasks if task.status == TASK_STATUS_PENDING]
    active_task_ids = [
        task.id
        for task in tasks
        if task.status in {TASK_STATUS_RUNNING, TASK_STATUS_AWAITING_VALIDATION, TASK_STATUS_PARTIAL}
    ]
    completed_task_ids = [task.id for task in tasks if task.status == TASK_STATUS_COMPLETED]
    failed_task_ids = [task.id for task in tasks if task.status == TASK_STATUS_FAILED]
    blocked_task_ids = [task.id for task in tasks if task.is_blocked]

    active_workstreams = _build_active_workstreams(tasks)
    recent_completed_work = _build_recent_completed_work(tasks, runs_by_task_id)
    recent_failure_learnings = _build_recent_failure_learnings(runs, tasks_by_id)
    validation_learnings = _build_validation_learnings(runs, tasks_by_id)
    recovery_learnings = _build_recovery_learnings(project_artifacts)
    key_decisions = _build_key_decisions(project_artifacts)
    referenced_paths = _build_path_signals(
        tasks=tasks,
        runs_by_task_id=runs_by_task_id,
        artifacts_by_task_id=artifacts_by_task_id,
        project_artifacts=project_artifacts,
    )
    recent_artifact_summaries = _build_recent_artifact_summaries(project_artifacts)
    task_memory = _build_task_memory(tasks, runs_by_task_id, artifacts_by_task_id)
    open_gaps = _build_open_gaps(tasks, runs_by_task_id, project_artifacts)

    summary = _build_context_summary(
        project=project,
        total_tasks=len(tasks),
        pending_task_ids=pending_task_ids,
        active_task_ids=active_task_ids,
        completed_task_ids=completed_task_ids,
        failed_task_ids=failed_task_ids,
        blocked_task_ids=blocked_task_ids,
        active_workstreams=active_workstreams,
        recent_completed_work=recent_completed_work,
        open_gaps=open_gaps,
        referenced_paths=referenced_paths,
    )

    return ProjectOperationalContext(
        project_id=project.id,
        project_name=project.name,
        project_goal=project.description or project.name,
        total_tasks=len(tasks),
        pending_task_ids=pending_task_ids,
        active_task_ids=active_task_ids,
        completed_task_ids=completed_task_ids,
        failed_task_ids=failed_task_ids,
        blocked_task_ids=blocked_task_ids,
        active_workstreams=active_workstreams,
        recent_completed_work=recent_completed_work,
        recent_failure_learnings=recent_failure_learnings,
        validation_learnings=validation_learnings,
        recovery_learnings=recovery_learnings,
        open_gaps=open_gaps,
        key_decisions=key_decisions,
        referenced_paths=referenced_paths,
        recent_artifact_summaries=recent_artifact_summaries,
        task_memory=task_memory,
        summary=summary,
    )


def persist_project_operational_context(
    db: Session,
    project_context: ProjectOperationalContext,
    created_by: str = "project_memory_service",
) -> Artifact:
    content = json.dumps(
        project_context.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )
    return create_artifact(
        db=db,
        project_id=project_context.project_id,
        task_id=None,
        artifact_type=PROJECT_OPERATIONAL_CONTEXT_ARTIFACT_TYPE,
        content=content,
        created_by=created_by,
    )


def build_and_persist_project_operational_context(
    db: Session,
    project_id: int,
    created_by: str = "project_memory_service",
) -> ProjectOperationalContext:
    context = build_project_operational_context(
        db=db,
        project_id=project_id,
    )
    persist_project_operational_context(
        db=db,
        project_context=context,
        created_by=created_by,
    )
    return context