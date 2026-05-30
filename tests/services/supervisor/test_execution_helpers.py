"""
Tests for _execution_helpers shared utilities.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators._execution_helpers import (
    _MAX_RUNS_PER_EVALUATION,
    agent_participated_in_run,
    build_historical_file_index,
    load_execution_trace_entries,
    select_runs_for_evaluation,
    serialize_run_for_evaluator,
)
from app.services.supervisor.trace_writer import EXECUTION_TRACE_FILENAME

# ---------------------------------------------------------------------------
# load_execution_trace_entries
# ---------------------------------------------------------------------------


def test_load_execution_trace_entries_filters_by_agent(tmp_path: Path):
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    trace_path = meta_dir / EXECUTION_TRACE_FILENAME
    trace_path.write_text(
        json.dumps({"agent": "orchestrator", "task_id": 1, "final_decision": "finish"})
        + "\n"
        + json.dumps({"agent": "context_selection_agent", "task_id": 1, "call_type": "initial"})
        + "\n"
        + json.dumps({"agent": "orchestrator", "task_id": 2, "final_decision": "reject"})
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
        return_value=paths,
    ):
        result = load_execution_trace_entries(1, agent="orchestrator")

    assert len(result) == 2
    assert all(e["agent"] == "orchestrator" for e in result)
    assert result[0]["final_decision"] == "finish"
    assert result[1]["final_decision"] == "reject"


def test_load_execution_trace_entries_returns_empty_when_no_file(tmp_path: Path):
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"

    with patch(
        "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
        return_value=paths,
    ):
        result = load_execution_trace_entries(99, agent="orchestrator")

    assert result == []


def test_load_execution_trace_entries_skips_invalid_json(tmp_path: Path):
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    trace_path = meta_dir / EXECUTION_TRACE_FILENAME
    trace_path.write_text(
        "not json\n"
        + json.dumps({"agent": "orchestrator", "task_id": 5, "final_decision": "finish"})
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
        return_value=paths,
    ):
        result = load_execution_trace_entries(1, agent="orchestrator")

    assert len(result) == 1
    assert result[0]["task_id"] == 5


# ---------------------------------------------------------------------------
# agent_participated_in_run
# ---------------------------------------------------------------------------


def test_agent_participated_in_run_true_when_present(
    make_execution_run, make_task, make_project, db_session
):
    proj = make_project()
    task = make_task(project_id=proj.id)
    run = make_execution_run(
        task_id=task.id,
        execution_agent_sequence=json.dumps(["context_selection_agent", "code_change_agent"]),
    )
    assert agent_participated_in_run(run, "code_change_agent") is True
    assert agent_participated_in_run(run, "context_selection_agent") is True


def test_agent_participated_in_run_false_when_absent(
    make_execution_run, make_task, make_project, db_session
):
    proj = make_project()
    task = make_task(project_id=proj.id)
    run = make_execution_run(
        task_id=task.id,
        execution_agent_sequence=json.dumps(["context_selection_agent", "code_change_agent"]),
    )
    assert agent_participated_in_run(run, "environment_manager_agent") is False


def test_agent_participated_in_run_false_when_no_sequence(
    make_execution_run, make_task, make_project, db_session
):
    proj = make_project()
    task = make_task(project_id=proj.id)
    run = make_execution_run(task_id=task.id)
    assert agent_participated_in_run(run, "code_change_agent") is False


# ---------------------------------------------------------------------------
# select_runs_for_evaluation
# ---------------------------------------------------------------------------


def _make_run_task_pair(status: str):
    run = MagicMock()
    run.status = status
    run.execution_agent_sequence = json.dumps(["context_selection_agent", "code_change_agent"])
    task = MagicMock()
    return run, task


def test_select_runs_caps_at_max(tmp_path):
    pairs = [_make_run_task_pair("succeeded") for _ in range(20)]
    result = select_runs_for_evaluation(pairs)
    assert len(result) == _MAX_RUNS_PER_EVALUATION


def test_select_runs_prefers_given_statuses():
    failed_pairs = [_make_run_task_pair("failed") for _ in range(3)]
    succeeded_pairs = [_make_run_task_pair("succeeded") for _ in range(3)]
    all_pairs = succeeded_pairs + failed_pairs  # failed at the end

    result = select_runs_for_evaluation(
        all_pairs,
        prefer_statuses=["failed"],
    )
    # Failed runs should be at the front
    assert all(r.status == "failed" for r, _ in result[:3])


def test_select_runs_filters_by_agent_name():
    with_agent = []
    for _ in range(3):
        run = MagicMock()
        run.status = "succeeded"
        run.execution_agent_sequence = json.dumps(["environment_manager_agent"])
        with_agent.append((run, MagicMock()))

    without_agent = []
    for _ in range(3):
        run = MagicMock()
        run.status = "succeeded"
        run.execution_agent_sequence = json.dumps(["code_change_agent"])
        without_agent.append((run, MagicMock()))

    result = select_runs_for_evaluation(
        with_agent + without_agent,
        agent_name="environment_manager_agent",
    )
    assert len(result) == 3


# ---------------------------------------------------------------------------
# build_historical_file_index
# ---------------------------------------------------------------------------


def test_build_historical_file_index_builds_from_changed_files(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj = make_project()
    task1 = make_task(project_id=proj.id)
    task2 = make_task(project_id=proj.id)

    run1 = make_execution_run(task_id=task1.id)
    run1.changed_files = json.dumps([{"path": "app/models/user.py"}, {"path": "app/api/users.py"}])

    run2 = make_execution_run(task_id=task2.id)
    run2.changed_files = json.dumps(
        [{"path": "app/models/user.py"}, {"path": "tests/test_users.py"}]
    )

    db_session.commit()

    index = build_historical_file_index(db_session, proj.id)

    assert "app/models/user.py" in index
    assert task1.id in index["app/models/user.py"]
    assert task2.id in index["app/models/user.py"]
    assert "app/api/users.py" in index
    assert task1.id in index["app/api/users.py"]
    assert "tests/test_users.py" in index
    assert task2.id in index["tests/test_users.py"]


def test_build_historical_file_index_handles_string_list_format(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    """changed_files may be a JSON list of strings (legacy format)."""
    proj = make_project()
    task = make_task(project_id=proj.id)
    run = make_execution_run(task_id=task.id)
    run.changed_files = json.dumps(["app/models/user.py", "app/api/users.py"])
    db_session.commit()

    index = build_historical_file_index(db_session, proj.id)

    assert "app/models/user.py" in index
    assert task.id in index["app/models/user.py"]


def test_build_historical_file_index_returns_empty_for_project_without_runs(
    db_session: Session,
    make_project,
):
    proj = make_project()
    index = build_historical_file_index(db_session, proj.id)
    assert index == {}


# ---------------------------------------------------------------------------
# serialize_run_for_evaluator
# ---------------------------------------------------------------------------


def test_serialize_run_for_evaluator_basic(make_execution_run, make_task, make_project, db_session):
    proj = make_project()
    task = make_task(project_id=proj.id, task_type="implementation")
    run = make_execution_run(task_id=task.id)
    run.status = "succeeded"
    run.failure_type = None
    run.work_summary = "Implemented the user model"
    run.blockers_found = json.dumps(["ENV_INSTALL_FAILED:torch"])
    run.execution_agent_sequence = json.dumps(["context_selection_agent", "code_change_agent"])
    db_session.commit()

    result = serialize_run_for_evaluator(run, task)

    assert result["run_id"] == run.id
    assert result["task_id"] == task.id
    assert result["task_title"] == task.title
    assert result["task_type"] == "implementation"
    assert result["status"] == "succeeded"
    assert result["work_summary"] == "Implemented the user model"
    assert "ENV_INSTALL_FAILED:torch" in result["blockers_found"]
    assert "code_change_agent" in result["execution_agent_sequence"]


def test_serialize_run_for_evaluator_handles_missing_fields(
    make_execution_run, make_task, make_project, db_session
):
    proj = make_project()
    task = make_task(project_id=proj.id)
    run = make_execution_run(task_id=task.id)
    db_session.commit()

    result = serialize_run_for_evaluator(run, task)
    assert result["blockers_found"] == []
    assert result["validation_notes"] == []
    assert result["execution_agent_sequence"] == []
