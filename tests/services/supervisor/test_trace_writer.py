"""
Tests for planning and execution trace writers.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.supervisor.trace_writer import (
    EXECUTION_TRACE_FILENAME,
    PLANNING_TRACE_FILENAME,
    append_execution_trace,
    append_planning_trace,
)


@pytest.fixture()
def mock_storage_paths(tmp_path: Path):
    project_meta = tmp_path / "project_meta"
    project_meta.mkdir(parents=True)
    paths = MagicMock()
    paths.project_meta_dir = project_meta
    return paths


def test_append_planning_trace_creates_file(tmp_path: Path, mock_storage_paths):
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        return_value=mock_storage_paths,
    ):
        append_planning_trace(
            project_id=1,
            entry={"agent": "planner", "call_type": "initial", "project_id": 1},
        )

    trace_path = mock_storage_paths.project_meta_dir / PLANNING_TRACE_FILENAME
    assert trace_path.exists()
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["agent"] == "planner"
    assert data["call_type"] == "initial"


def test_append_planning_trace_appends_multiple_entries(tmp_path: Path, mock_storage_paths):
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        return_value=mock_storage_paths,
    ):
        append_planning_trace(
            project_id=1,
            entry={"agent": "planner", "call_type": "initial"},
        )
        append_planning_trace(
            project_id=1,
            entry={
                "agent": "atomic_task_generator",
                "call_type": "initial",
                "inputs": {"parent_task_id": 10},
            },
        )

    trace_path = mock_storage_paths.project_meta_dir / PLANNING_TRACE_FILENAME
    lines = [ln for ln in trace_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["agent"] == "planner"
    assert json.loads(lines[1])["agent"] == "atomic_task_generator"


def test_append_planning_trace_does_not_raise_on_storage_error():
    """A storage failure must be silently swallowed and logged, never propagated."""
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        side_effect=OSError("disk full"),
    ):
        # Should not raise
        append_planning_trace(
            project_id=999,
            entry={"agent": "planner", "call_type": "initial"},
        )


def test_append_planning_trace_serializes_nested_objects(tmp_path: Path, mock_storage_paths):
    entry = {
        "agent": "execution_sequencer",
        "call_type": "initial",
        "output_snapshot": {"plan_version": 1, "batches": [{"batch_id": "b1", "task_ids": [1, 2]}]},
    }
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        return_value=mock_storage_paths,
    ):
        append_planning_trace(project_id=2, entry=entry)

    trace_path = mock_storage_paths.project_meta_dir / PLANNING_TRACE_FILENAME
    data = json.loads(trace_path.read_text(encoding="utf-8").strip())
    assert data["output_snapshot"]["plan_version"] == 1
    assert data["output_snapshot"]["batches"][0]["task_ids"] == [1, 2]


# ---------------------------------------------------------------------------
# append_execution_trace tests
# ---------------------------------------------------------------------------


def test_append_execution_trace_creates_file(mock_storage_paths):
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        return_value=mock_storage_paths,
    ):
        append_execution_trace(
            project_id=1,
            entry={
                "agent": "orchestrator",
                "task_id": 10,
                "run_id": 100,
                "final_decision": "finish",
            },
        )

    trace_path = mock_storage_paths.project_meta_dir / EXECUTION_TRACE_FILENAME
    assert trace_path.exists()
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["agent"] == "orchestrator"
    assert data["final_decision"] == "finish"


def test_append_execution_trace_appends_multiple_entries(mock_storage_paths):
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        return_value=mock_storage_paths,
    ):
        append_execution_trace(
            project_id=1,
            entry={"agent": "orchestrator", "task_id": 10, "final_decision": "finish"},
        )
        append_execution_trace(
            project_id=1,
            entry={
                "agent": "context_selection_agent",
                "task_id": 10,
                "call_type": "initial",
                "output_snapshot": {"selected_count": 2},
            },
        )

    trace_path = mock_storage_paths.project_meta_dir / EXECUTION_TRACE_FILENAME
    lines = [ln for ln in trace_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["agent"] == "orchestrator"
    assert json.loads(lines[1])["agent"] == "context_selection_agent"


def test_append_execution_trace_does_not_raise_on_storage_error():
    """A storage failure must be silently swallowed and logged, never propagated."""
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        side_effect=OSError("disk full"),
    ):
        append_execution_trace(
            project_id=999,
            entry={"agent": "orchestrator", "task_id": 1, "final_decision": "reject"},
        )


def test_append_execution_trace_uses_separate_file_from_planning(mock_storage_paths):
    """Planning and execution traces must write to different files."""
    with patch(
        "app.services.supervisor.trace_writer._storage.ensure_project_storage",
        return_value=mock_storage_paths,
    ):
        append_planning_trace(project_id=1, entry={"agent": "planner", "call_type": "initial"})
        append_execution_trace(
            project_id=1,
            entry={"agent": "orchestrator", "task_id": 5, "final_decision": "finish"},
        )

    planning_path = mock_storage_paths.project_meta_dir / PLANNING_TRACE_FILENAME
    execution_path = mock_storage_paths.project_meta_dir / EXECUTION_TRACE_FILENAME
    assert planning_path.exists()
    assert execution_path.exists()
    assert planning_path != execution_path

    planning_data = json.loads(planning_path.read_text(encoding="utf-8").strip())
    execution_data = json.loads(execution_path.read_text(encoding="utf-8").strip())
    assert planning_data["agent"] == "planner"
    assert execution_data["agent"] == "orchestrator"
