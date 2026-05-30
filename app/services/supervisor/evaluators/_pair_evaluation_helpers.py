"""
Shared helpers for executor/validator pair supervisor evaluators.

Used by code_change_agent_evaluator, command_runner_agent_evaluator,
test_builder_agent_evaluator, document_writer_agent_evaluator, and their
corresponding validator evaluators.

Core concept: both the executor evaluator and the validator evaluator for a pair
share the same task selection.  build_pair_evaluation_context() is deterministic;
both evaluators call it independently with the same arguments and receive the
same 10 tasks.

Task selection tiers (in priority order):
  Tier 1 — task_status in {partial, failed, reatomized, followed_up}
  Tier 2 — had_budget_exceeded=True (completed but the orchestrator exhausted budget)
  Tier 3 — tasks with the highest run count (most retries), capped at 2
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import (
    TASK_STATUS_FAILED,
    TASK_STATUS_FOLLOWED_UP,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
    Task,
)
from app.services.supervisor.evaluators._execution_helpers import (
    agent_participated_in_run,
    load_execution_trace_entries,
)

logger = logging.getLogger(__name__)

_VALIDATION_RESULT_ARTIFACT_TYPE = "validation_result"
_MAX_TASKS_PER_EVALUATION = 10
_MAX_TIER3_TASKS = 2

_TIER1_STATUSES = {
    TASK_STATUS_PARTIAL,
    TASK_STATUS_FAILED,
    TASK_STATUS_REATOMIZED,
    TASK_STATUS_FOLLOWED_UP,
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def load_artifact_for_run(
    db: Session,
    run_id: int,
    task_id: int,
) -> dict[str, Any] | None:
    """Return the validation_result artifact payload for this specific run, or None."""
    artifacts = (
        db.query(Artifact)
        .filter(
            Artifact.task_id == task_id,
            Artifact.artifact_type == _VALIDATION_RESULT_ARTIFACT_TYPE,
        )
        .order_by(Artifact.id.asc())
        .all()
    )
    for artifact in artifacts:
        try:
            payload = json.loads(artifact.content)
            if payload.get("execution_run_id") == run_id:
                return payload
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def get_validator_result_from_artifact(
    artifact: dict[str, Any],
    validator_key: str,
) -> dict[str, Any] | None:
    """Extract the per-validator entry from artifact['validator_results']."""
    for vr in artifact.get("validator_results", []):
        if vr.get("validator_key") == validator_key:
            return vr
    return None


def _parse_blockers(blockers_found: str | None) -> list[str]:
    if not blockers_found:
        return []
    try:
        parsed = json.loads(blockers_found)
        if isinstance(parsed, list):
            return [str(b) for b in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return [b.strip() for b in blockers_found.split(";") if b.strip()]


def _parse_changed_files(changed_files: str | None) -> list[str]:
    if not changed_files:
        return []
    try:
        entries = json.loads(changed_files)
    except (json.JSONDecodeError, TypeError):
        return []
    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict):
            p = entry.get("path") or entry.get("file_path") or ""
            if p:
                paths.append(str(p).strip())
    return paths


def _load_command_trace_entries_for_run(
    project_id: int,
    run_id: int,
) -> list[dict[str, Any]]:
    """Return command_runner_agent trace entries that belong to this specific run_id."""
    all_entries = load_execution_trace_entries(project_id, agent="command_runner_agent")
    return [e for e in all_entries if e.get("run_id") == run_id]


def _get_budget_exceeded_task_ids(project_id: int) -> set[int]:
    """Return the set of task_ids that had at least one budget_exceeded orchestrator run."""
    entries = load_execution_trace_entries(project_id, agent="orchestrator")
    return {
        e["task_id"]
        for e in entries
        if e.get("final_decision") == "budget_exceeded" and "task_id" in e
    }


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------


def _build_run_evidence(
    db: Session,
    run: ExecutionRun,
    *,
    project_id: int,
    validator_key: str,
    include_command_trace: bool,
) -> dict[str, Any]:
    """Build a compact evidence dict for one ExecutionRun."""
    artifact = load_artifact_for_run(db, run.id, run.task_id)

    # Joint decision comes from the aggregated artifact-level field
    joint_decision: str | None = None
    if artifact is not None:
        joint_decision = artifact.get("decision") or (artifact.get("aggregation") or {}).get(
            "winning_decision"
        )

    # Per-validator result (for cross-context)
    validator_result: dict[str, Any] | None = None
    if artifact is not None:
        vr = get_validator_result_from_artifact(artifact, validator_key)
        if vr is not None:
            validator_result = {
                "decision": vr.get("decision"),
                "summary": vr.get("summary"),
                "validated_scope": vr.get("validated_scope"),
                "missing_scope": vr.get("missing_scope"),
                "blockers": vr.get("blockers", []),
                "findings": vr.get("findings", []),
                "partial_annotations": vr.get("partial_annotations", []),
                "unconsumed_evidence_ids": vr.get("unconsumed_evidence_ids", []),
            }

    command_trace_entries: list[dict[str, Any]] = []
    if include_command_trace:
        command_trace_entries = _load_command_trace_entries_for_run(project_id, run.id)

    agent_sequence: list[str] = []
    if run.execution_agent_sequence:
        try:
            agent_sequence = json.loads(run.execution_agent_sequence)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "run_id": run.id,
        "attempt_number": run.attempt_number,
        "run_status": run.status,
        "failure_type": run.failure_type,
        "failure_code": run.failure_code,
        "work_summary": run.work_summary,
        "completed_scope": run.completed_scope,
        "remaining_scope": run.remaining_scope,
        "blockers_found": _parse_blockers(run.blockers_found),
        "changed_files": _parse_changed_files(run.changed_files),
        "agent_sequence": agent_sequence,
        # command_runner_agent only — empty list for other agents
        "command_trace_entries": command_trace_entries,
        # cross-context: joint decision for executor evaluator
        "joint_validation_decision": joint_decision,
        # cross-context: per-validator result (executor → sees validator; validator → primary data)
        "validator_result": validator_result,
    }


def _build_task_evidence(
    db: Session,
    task: Task,
    runs: list[ExecutionRun],
    *,
    project_id: int,
    validator_key: str,
    include_command_trace: bool,
) -> dict[str, Any]:
    """Build a compact evidence dict for one Task and all its ExecutionRuns."""
    run_evidences = [
        _build_run_evidence(
            db,
            run,
            project_id=project_id,
            validator_key=validator_key,
            include_command_trace=include_command_trace,
        )
        for run in runs
    ]
    return {
        "task_id": task.id,
        "task_title": task.title,
        "task_type": task.task_type,
        "task_objective": task.objective,
        "acceptance_criteria": task.acceptance_criteria,
        "task_status": task.status,
        "had_budget_exceeded": False,  # filled by build_pair_evaluation_context
        "runs": run_evidences,
    }


# ---------------------------------------------------------------------------
# Task selection
# ---------------------------------------------------------------------------


def _select_task_evidences(
    task_evidences: list[dict[str, Any]],
    *,
    max_tasks: int = _MAX_TASKS_PER_EVALUATION,
) -> list[dict[str, Any]]:
    """Return up to max_tasks task evidences sorted by priority tier.

    Tier 1: task_status in {partial, failed, reatomized, followed_up}
    Tier 2: had_budget_exceeded=True
    Tier 3: highest run count (max _MAX_TIER3_TASKS items)
    """
    tier1 = [e for e in task_evidences if e["task_status"] in _TIER1_STATUSES]
    tier1_ids = {e["task_id"] for e in tier1}

    tier2 = [
        e for e in task_evidences if e["task_id"] not in tier1_ids and e.get("had_budget_exceeded")
    ]
    tier2_ids = {e["task_id"] for e in tier2}

    remaining = [
        e for e in task_evidences if e["task_id"] not in tier1_ids and e["task_id"] not in tier2_ids
    ]
    tier3 = sorted(remaining, key=lambda e: len(e["runs"]), reverse=True)[:_MAX_TIER3_TASKS]

    combined = tier1 + tier2 + tier3
    return combined[:max_tasks]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_pair_evaluation_context(
    db: Session,
    project_id: int,
    *,
    agent_name: str,
    validator_name: str,
    max_tasks: int = _MAX_TASKS_PER_EVALUATION,
) -> dict[str, Any]:
    """Build a shared PairEvaluationContext for an executor/validator pair.

    Both the executor evaluator and the validator evaluator call this function
    independently.  The result is deterministic for the same inputs.

    Returns a JSON-serializable dict with keys:
      project_id, agent_name, validator_name, tasks (list of task evidence dicts)
    """
    rows = (
        db.query(ExecutionRun, Task)
        .join(Task, ExecutionRun.task_id == Task.id)
        .filter(Task.project_id == project_id)
        .order_by(ExecutionRun.id.asc())
        .all()
    )

    agent_rows = [(run, task) for run, task in rows if agent_participated_in_run(run, agent_name)]

    if not agent_rows:
        return {
            "project_id": project_id,
            "agent_name": agent_name,
            "validator_name": validator_name,
            "tasks": [],
        }

    # Group runs by task_id
    task_runs: dict[int, list[ExecutionRun]] = defaultdict(list)
    task_objects: dict[int, Task] = {}
    for run, task in agent_rows:
        task_runs[task.id].append(run)
        task_objects[task.id] = task

    # Find tasks where any run had budget_exceeded
    budget_exceeded_task_ids = _get_budget_exceeded_task_ids(project_id)

    include_command_trace = agent_name == "command_runner_agent"

    # Build task evidence for each task group
    all_task_evidences: list[dict[str, Any]] = []
    for task_id, runs in task_runs.items():
        task = task_objects[task_id]
        evidence = _build_task_evidence(
            db,
            task,
            runs,
            project_id=project_id,
            validator_key=validator_name,
            include_command_trace=include_command_trace,
        )
        evidence["had_budget_exceeded"] = task_id in budget_exceeded_task_ids
        all_task_evidences.append(evidence)

    selected = _select_task_evidences(all_task_evidences, max_tasks=max_tasks)

    return {
        "project_id": project_id,
        "agent_name": agent_name,
        "validator_name": validator_name,
        "tasks": selected,
    }
