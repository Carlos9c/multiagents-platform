"""
Trace writers for planning and execution agents.

Each agent call appends one JSON line to either:
  {project_meta_dir}/planning_trace.jsonl   — planning pipeline agents
  {project_meta_dir}/execution_trace.jsonl  — orchestrator + subagents

Files are append-only and never truncated by this module.  If the project
storage is not accessible the write is silently skipped and an error is
logged — a trace failure must never propagate to callers.

Format: one JSON object per line (JSONL).  Consumers (Supervisor) iterate
the file line-by-line and can filter by ``agent`` or ``call_type``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.services.project_storage import ProjectStorageService

logger = logging.getLogger(__name__)

PLANNING_TRACE_FILENAME = "planning_trace.jsonl"
EXECUTION_TRACE_FILENAME = "execution_trace.jsonl"

_storage = ProjectStorageService()


def append_planning_trace(
    *,
    project_id: int,
    entry: dict[str, Any],
) -> None:
    """Append one JSON entry to the project's planning_trace.jsonl.

    The entry dict is written as a single line.  This function never raises;
    all errors are logged at WARNING level so that planning clients are not
    interrupted.

    Parameters
    ----------
    project_id:
        ID of the project whose planning trace should be updated.
    entry:
        Arbitrary JSON-serialisable dict produced by the calling planning
        client.  Must include at least ``agent`` and ``call_type`` keys.
    """
    try:
        paths = _storage.ensure_project_storage(project_id)
        trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME
        line = json.dumps(entry, ensure_ascii=False)
        with trace_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.warning(
            "planning_trace_write_failed project_id=%s agent=%s",
            project_id,
            entry.get("agent", "unknown"),
            exc_info=True,
        )


def append_execution_trace(
    *,
    project_id: int,
    entry: dict[str, Any],
) -> None:
    """Append one JSON entry to the project's execution_trace.jsonl.

    The entry dict is written as a single line.  This function never raises;
    all errors are logged at WARNING level so that execution clients are not
    interrupted.

    Parameters
    ----------
    project_id:
        ID of the project whose execution trace should be updated.
    entry:
        Arbitrary JSON-serialisable dict produced by the calling execution
        agent.  Must include at least ``agent`` and ``task_id`` keys.
    """
    try:
        paths = _storage.ensure_project_storage(project_id)
        trace_path: Path = paths.project_meta_dir / EXECUTION_TRACE_FILENAME
        line = json.dumps(entry, ensure_ascii=False)
        with trace_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.warning(
            "execution_trace_write_failed project_id=%s agent=%s",
            project_id,
            entry.get("agent", "unknown"),
            exc_info=True,
        )
