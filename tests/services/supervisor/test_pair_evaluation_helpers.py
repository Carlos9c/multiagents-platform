"""
Tests for the _pair_evaluation_helpers shared module.

Covers: artifact loading, validator result extraction,
3-tier task selection, and build_pair_evaluation_context.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.task import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_REATOMIZED,
)
from app.services.supervisor.evaluators._pair_evaluation_helpers import (
    _select_task_evidences,
    build_pair_evaluation_context,
    get_validator_result_from_artifact,
    load_artifact_for_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_evidence(
    task_id: int = 1,
    status: str = TASK_STATUS_COMPLETED,
    had_budget_exceeded: bool = False,
    run_count: int = 1,
) -> dict:
    return {
        "task_id": task_id,
        "task_title": f"Task {task_id}",
        "task_type": "implementation",
        "task_objective": "Do something",
        "acceptance_criteria": "Must work",
        "task_status": status,
        "had_budget_exceeded": had_budget_exceeded,
        "runs": [{"run_id": i} for i in range(run_count)],
    }


def _make_artifact_payload(run_id: int, decision: str = "completed") -> dict:
    return {
        "execution_run_id": run_id,
        "decision": decision,
        "validator_results": [
            {
                "validator_key": "code_change_agent_validator",
                "decision": decision,
                "summary": f"Summary for run {run_id}",
                "findings": [],
                "partial_annotations": [],
            }
        ],
        "aggregation": {"winning_decision": decision},
    }


# ---------------------------------------------------------------------------
# load_artifact_for_run
# ---------------------------------------------------------------------------


def test_load_artifact_for_run_returns_none_when_no_artifacts(
    db_session: Session,
    make_project,
    make_task,
):
    proj = make_project(name="No artifact project")
    task = make_task(project_id=proj.id)
    result = load_artifact_for_run(db_session, run_id=999, task_id=task.id)
    assert result is None


def test_load_artifact_for_run_finds_matching_run_id(
    db_session: Session,
    make_project,
    make_task,
    make_artifact,
):
    proj = make_project(name="Artifact project")
    task = make_task(project_id=proj.id)
    run_id = 42
    payload = _make_artifact_payload(run_id=run_id)
    make_artifact(
        project_id=proj.id,
        task_id=task.id,
        artifact_type="validation_result",
        content=json.dumps(payload),
    )

    result = load_artifact_for_run(db_session, run_id=run_id, task_id=task.id)
    assert result is not None
    assert result["execution_run_id"] == run_id
    assert result["decision"] == "completed"


def test_load_artifact_for_run_returns_none_when_run_id_not_matched(
    db_session: Session,
    make_project,
    make_task,
    make_artifact,
):
    proj = make_project(name="Artifact mismatch project")
    task = make_task(project_id=proj.id)
    payload = _make_artifact_payload(run_id=100)
    make_artifact(
        project_id=proj.id,
        task_id=task.id,
        artifact_type="validation_result",
        content=json.dumps(payload),
    )

    # Different run_id → no match
    result = load_artifact_for_run(db_session, run_id=999, task_id=task.id)
    assert result is None


# ---------------------------------------------------------------------------
# get_validator_result_from_artifact
# ---------------------------------------------------------------------------


def test_get_validator_result_returns_matching_key():
    artifact = {
        "validator_results": [
            {"validator_key": "code_change_agent_validator", "decision": "completed"},
            {"validator_key": "command_runner_agent_validator", "decision": "partial"},
        ]
    }
    result = get_validator_result_from_artifact(artifact, "code_change_agent_validator")
    assert result is not None
    assert result["decision"] == "completed"


def test_get_validator_result_returns_none_when_key_not_found():
    artifact = {
        "validator_results": [
            {"validator_key": "code_change_agent_validator", "decision": "completed"},
        ]
    }
    result = get_validator_result_from_artifact(artifact, "unknown_validator")
    assert result is None


def test_get_validator_result_returns_none_when_no_validator_results():
    artifact = {"decision": "completed"}
    result = get_validator_result_from_artifact(artifact, "code_change_agent_validator")
    assert result is None


# ---------------------------------------------------------------------------
# _select_task_evidences
# ---------------------------------------------------------------------------


def test_select_task_evidences_tier1_takes_priority_over_tier2_and_tier3():
    evidences = [
        _make_task_evidence(task_id=1, status=TASK_STATUS_COMPLETED, had_budget_exceeded=True),
        _make_task_evidence(task_id=2, status=TASK_STATUS_FAILED),
        _make_task_evidence(task_id=3, status=TASK_STATUS_COMPLETED, run_count=5),
    ]
    result = _select_task_evidences(evidences)
    # tier1 (failed) goes first
    assert result[0]["task_id"] == 2


def test_select_task_evidences_tier2_follows_tier1():
    evidences = [
        _make_task_evidence(task_id=1, status=TASK_STATUS_COMPLETED, had_budget_exceeded=True),
        _make_task_evidence(task_id=2, status=TASK_STATUS_PARTIAL),
    ]
    result = _select_task_evidences(evidences)
    task_ids = [e["task_id"] for e in result]
    # tier1 (partial) before tier2 (budget_exceeded)
    assert task_ids.index(2) < task_ids.index(1)


def test_select_task_evidences_tier3_capped_at_2():
    # 5 completed tasks with varying run counts, no tier1/tier2
    evidences = [
        _make_task_evidence(task_id=i, status=TASK_STATUS_COMPLETED, run_count=i)
        for i in range(1, 6)
    ]
    result = _select_task_evidences(evidences)
    # tier3 is capped at 2
    assert len(result) == 2
    # Should be the two tasks with most runs (4 and 5)
    task_ids = {e["task_id"] for e in result}
    assert task_ids == {4, 5}


def test_select_task_evidences_caps_at_max_tasks():
    # 15 tier1 tasks — only 10 returned
    evidences = [_make_task_evidence(task_id=i, status=TASK_STATUS_FAILED) for i in range(15)]
    result = _select_task_evidences(evidences, max_tasks=10)
    assert len(result) == 10


def test_select_task_evidences_tier1_statuses_are_all_included():
    statuses = [
        TASK_STATUS_PARTIAL,
        TASK_STATUS_FAILED,
        TASK_STATUS_REATOMIZED,
        "followed_up",
    ]
    evidences = [_make_task_evidence(task_id=i, status=s) for i, s in enumerate(statuses, start=1)]
    result = _select_task_evidences(evidences)
    assert len(result) == 4  # all 4 are tier1


def test_select_task_evidences_tier1_excluded_from_tier2():
    """A tier1 task that also had budget_exceeded must not appear twice."""
    evidences = [
        _make_task_evidence(task_id=1, status=TASK_STATUS_PARTIAL, had_budget_exceeded=True),
    ]
    result = _select_task_evidences(evidences)
    # Appears only once
    assert len(result) == 1
    assert result[0]["task_id"] == 1


# ---------------------------------------------------------------------------
# build_pair_evaluation_context
# ---------------------------------------------------------------------------


def test_build_pair_evaluation_context_returns_empty_when_no_runs(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No runs project")
    with (
        patch(
            "app.services.supervisor.evaluators._pair_evaluation_helpers._get_budget_exceeded_task_ids",
            return_value=set(),
        ),
    ):
        ctx = build_pair_evaluation_context(
            db_session,
            proj.id,
            agent_name="code_change_agent",
            validator_name="code_change_agent_validator",
        )
    assert ctx["tasks"] == []
    assert ctx["agent_name"] == "code_change_agent"
    assert ctx["validator_name"] == "code_change_agent_validator"


def test_build_pair_evaluation_context_groups_tasks_by_task_id(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj = make_project(name="Context grouping project")
    task1 = make_task(project_id=proj.id, title="Task 1", status=TASK_STATUS_PARTIAL)
    task2 = make_task(project_id=proj.id, title="Task 2", status=TASK_STATUS_COMPLETED)

    seq = json.dumps(["code_change_agent"])
    # Two runs for task1, one for task2
    make_execution_run(task_id=task1.id, execution_agent_sequence=seq)
    make_execution_run(task_id=task1.id, execution_agent_sequence=seq)
    make_execution_run(task_id=task2.id, execution_agent_sequence=seq)

    with (
        patch(
            "app.services.supervisor.evaluators._pair_evaluation_helpers._get_budget_exceeded_task_ids",
            return_value=set(),
        ),
        patch(
            "app.services.supervisor.evaluators._pair_evaluation_helpers._load_command_trace_entries_for_run",
            return_value=[],
        ),
    ):
        ctx = build_pair_evaluation_context(
            db_session,
            proj.id,
            agent_name="code_change_agent",
            validator_name="code_change_agent_validator",
        )

    # task1 is tier1 (partial), task2 would be tier3 but max 2 in tier3
    task_ids = {t["task_id"] for t in ctx["tasks"]}
    assert task1.id in task_ids
    # task1 has 2 runs
    task1_evidence = next(t for t in ctx["tasks"] if t["task_id"] == task1.id)
    assert len(task1_evidence["runs"]) == 2


def test_build_pair_evaluation_context_marks_budget_exceeded_tasks(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj = make_project(name="Budget exceeded project")
    task = make_task(project_id=proj.id, title="Budget task", status=TASK_STATUS_COMPLETED)
    seq = json.dumps(["code_change_agent"])
    make_execution_run(task_id=task.id, execution_agent_sequence=seq)

    with (
        patch(
            "app.services.supervisor.evaluators._pair_evaluation_helpers._get_budget_exceeded_task_ids",
            return_value={task.id},
        ),
        patch(
            "app.services.supervisor.evaluators._pair_evaluation_helpers._load_command_trace_entries_for_run",
            return_value=[],
        ),
    ):
        ctx = build_pair_evaluation_context(
            db_session,
            proj.id,
            agent_name="code_change_agent",
            validator_name="code_change_agent_validator",
        )

    assert len(ctx["tasks"]) == 1
    assert ctx["tasks"][0]["had_budget_exceeded"] is True
