"""
Shared helpers for execution-layer supervisor evaluators.

Used by orchestrator_evaluator, context_selection_agent_evaluator, and
environment_manager_agent_evaluator.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.execution_run import ExecutionRun
from app.models.task import Task
from app.services.project_storage import ProjectStorageService
from app.services.supervisor.trace_writer import EXECUTION_TRACE_FILENAME

logger = logging.getLogger(__name__)

_storage = ProjectStorageService()

_MAX_RUNS_PER_EVALUATION = 15


def load_execution_trace_entries(
    project_id: int,
    *,
    agent: str,
) -> list[dict[str, Any]]:
    """Return all execution_trace.jsonl entries for a given agent, in file order."""
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / EXECUTION_TRACE_FILENAME

    if not trace_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") == agent:
                entries.append(entry)
        except json.JSONDecodeError:
            logger.warning(
                "_execution_helpers: invalid JSONL line for project %s agent %s",
                project_id,
                agent,
            )
    return entries


def get_project_execution_runs_with_tasks(
    db: Session,
    project_id: int,
) -> list[tuple[ExecutionRun, Task]]:
    """Return all (run, task) pairs for a project, ordered by run.id ascending."""
    rows = (
        db.query(ExecutionRun, Task)
        .join(Task, ExecutionRun.task_id == Task.id)
        .filter(Task.project_id == project_id)
        .order_by(ExecutionRun.id.asc())
        .all()
    )
    return list(rows)


def agent_participated_in_run(run: ExecutionRun, agent_name: str) -> bool:
    """Return True if agent_name appears in the run's execution_agent_sequence."""
    if not run.execution_agent_sequence:
        return False
    try:
        seq = json.loads(run.execution_agent_sequence)
        return agent_name in seq
    except (json.JSONDecodeError, TypeError):
        return False


def select_runs_for_evaluation(
    runs_with_tasks: list[tuple[ExecutionRun, Task]],
    *,
    agent_name: str | None = None,
    prefer_statuses: list[str] | None = None,
) -> list[tuple[ExecutionRun, Task]]:
    """Return up to _MAX_RUNS_PER_EVALUATION runs, optionally filtered and prioritised.

    Parameters
    ----------
    runs_with_tasks:
        All (run, task) pairs for the project.
    agent_name:
        When set, keep only runs where this agent appeared in execution_agent_sequence.
        When None, all runs are considered.
    prefer_statuses:
        When set, runs with these statuses are moved to the front before applying the cap.
        Typical use: prefer failed/partial runs to surface problems first.
    """
    if agent_name is not None:
        filtered = [(r, t) for r, t in runs_with_tasks if agent_participated_in_run(r, agent_name)]
    else:
        filtered = list(runs_with_tasks)

    if not prefer_statuses:
        return filtered[:_MAX_RUNS_PER_EVALUATION]

    priority_set = set(prefer_statuses)
    prioritised = [pair for pair in filtered if pair[0].status in priority_set]
    rest = [pair for pair in filtered if pair[0].status not in priority_set]
    combined = prioritised + rest
    return combined[:_MAX_RUNS_PER_EVALUATION]


def build_historical_file_index(
    db: Session,
    project_id: int,
) -> dict[str, list[int]]:
    """Build {file_path: [task_ids]} for all completed tasks in the project.

    Considers all ExecutionRun rows with non-empty changed_files for tasks
    belonging to this project.  The same file may appear under multiple task IDs
    if it was modified by successive tasks.
    """
    rows = (
        db.query(ExecutionRun.task_id, ExecutionRun.changed_files)
        .join(Task, ExecutionRun.task_id == Task.id)
        .filter(
            Task.project_id == project_id,
            ExecutionRun.changed_files.isnot(None),
        )
        .all()
    )

    index: dict[str, list[int]] = {}
    for task_id, changed_files_json in rows:
        if not changed_files_json:
            continue
        try:
            files = json.loads(changed_files_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(files, list):
            continue
        for file_entry in files:
            # changed_files may be stored as list[str] or list[dict] depending on the run
            if isinstance(file_entry, str):
                path = file_entry
            elif isinstance(file_entry, dict):
                path = file_entry.get("path") or file_entry.get("file_path") or ""
            else:
                continue
            path = path.strip()
            if not path:
                continue
            if path not in index:
                index[path] = []
            if task_id not in index[path]:
                index[path].append(task_id)

    return index


def get_system_versions_for_runs(db: Session, run_ids: list[int]) -> list[str]:
    """Return unique non-null system_version values for the given ExecutionRun IDs."""
    if not run_ids:
        return []
    rows = db.query(ExecutionRun.system_version).filter(ExecutionRun.id.in_(run_ids)).all()
    return sorted({row[0] for row in rows if row[0]})


def serialize_run_for_evaluator(
    run: ExecutionRun,
    task: Task,
) -> dict[str, Any]:
    """Serialize an (ExecutionRun, Task) pair into a compact dict for evaluator prompts."""
    blockers: list[str] = []
    if run.blockers_found:
        try:
            blockers = json.loads(run.blockers_found)
        except (json.JSONDecodeError, TypeError):
            blockers = [run.blockers_found]

    validation_notes: list[str] = []
    if run.validation_notes:
        try:
            validation_notes = json.loads(run.validation_notes)
        except (json.JSONDecodeError, TypeError):
            validation_notes = [run.validation_notes]

    agent_sequence: list[str] = []
    if run.execution_agent_sequence:
        try:
            agent_sequence = json.loads(run.execution_agent_sequence)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "task_title": task.title,
        "task_type": task.task_type,
        "status": run.status,
        "failure_type": run.failure_type,
        "execution_agent_sequence": agent_sequence,
        "work_summary": run.work_summary,
        "blockers_found": blockers,
        "validation_notes": validation_notes,
    }
