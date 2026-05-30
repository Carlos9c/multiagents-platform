"""
Tests for _recovery_evaluation_helpers.

Covers: evaluation_decision artifact loading, recovery_decision artifact loading
with cross-context, and recovery_assignment trio grouping.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.execution_run import EXECUTION_RUN_STATUS_FAILED
from app.services.supervisor.evaluators._recovery_evaluation_helpers import (
    load_evaluation_decision_artifacts,
    load_recovery_assignment_trios,
    load_recovery_decision_artifacts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_eval_decision_payload(
    decision: str = "stage_completed",
    artifact_id: int | None = None,
) -> dict:
    payload: dict = {
        "decision": decision,
        "decision_summary": f"Stage {decision} after batch evaluation.",
        "stage_goals_satisfied": decision == "stage_completed",
        "project_stage_closed": decision == "stage_completed",
        "recovery_strategy": "none",
        "evaluated_batches": [
            {
                "batch_id": "batch-1",
                "outcome": "successful",
                "summary": "All tasks completed.",
                "failed_task_ids": [],
                "partial_task_ids": [],
                "completed_task_ids": [1, 2],
            }
        ],
        "key_risks": [],
        "notes": [],
    }
    if artifact_id is not None:
        payload["artifact_id"] = artifact_id
    return payload


def _make_recovery_decision_payload(
    action: str = "reatomize",
    source_task_id: int = 10,
    source_run_id: int = 100,
) -> dict:
    return {
        "source_task_id": source_task_id,
        "source_run_id": source_run_id,
        "action": action,
        "confidence": "medium",
        "reason": "Task scope was too broad to complete atomically.",
        "covered_gap_summary": "Split into smaller focused tasks.",
        "requires_manual_review": False,
        "still_blocks_progress": True,
        "created_tasks": [
            {
                "title": "Implement auth module",
                "description": "Create the authentication module with JWT support.",
                "task_type": "implementation",
            }
        ],
    }


def _make_assignment_input_payload(project_id: int = 1) -> dict:
    return {
        "project_id": project_id,
        "project_goal": "Build a web application.",
        "resolved_intent_type": "assign",
        "resolved_mutation_scope": "assignment",
        "new_tasks": [
            {
                "task_id": 50,
                "title": "Add login endpoint",
                "description": "Implement POST /login route.",
                "task_type": "implementation",
            }
        ],
        "live_plan_summary": {
            "plan_version": 1,
            "current_batch_id": "batch-1",
            "current_batch_name": "Foundation",
            "remaining_batches": [],
        },
    }


def _make_assignment_output_payload() -> dict:
    return {
        "strategy": "continue_with_assignment",
        "task_assessments": [
            {
                "task_id": 50,
                "impact_type": "immediate_blocking",
                "suggested_cluster_id": "cluster-1",
                "rationale": "Login endpoint blocks the next batch.",
            }
        ],
        "clusters": [
            {
                "cluster_id": "cluster-1",
                "task_ids_in_execution_order": [50],
                "impact_type": "immediate_blocking",
                "placement_relation": "before_next_useful_progress",
                "rationale": "Must run before the next batch.",
            }
        ],
        "notes": [],
    }


def _make_compiled_plan_payload(assigned_task_ids: list[int] | None = None) -> dict:
    return {
        "strategy": "continue_with_assignment",
        "requires_replan": False,
        "assigned_task_ids": assigned_task_ids or [50],
        "unassigned_task_ids": [],
        "compiled_cluster_assignments": [
            {
                "cluster_id": "cluster-1",
                "task_ids_in_execution_order": [50],
                "impact_type": "immediate_blocking",
                "placement_relation": "before_next_useful_progress",
                "batch_assignment_mode": "new_patch_batch",
                "target_batch_id": "patch-batch-1",
                "target_batch_name": "Patch 1",
            }
        ],
    }


# ---------------------------------------------------------------------------
# load_evaluation_decision_artifacts
# ---------------------------------------------------------------------------


def test_load_evaluation_decisions_returns_empty_when_no_artifacts(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No eval decisions")
    result = load_evaluation_decision_artifacts(db_session, proj.id)
    assert result == []


def test_load_evaluation_decisions_returns_parsed_content(
    db_session: Session,
    make_project,
    make_artifact,
):
    proj = make_project(name="Eval decisions project")
    payload = _make_eval_decision_payload(decision="stage_incomplete")
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="evaluation_decision",
        content=json.dumps(payload),
    )

    result = load_evaluation_decision_artifacts(db_session, proj.id)
    assert len(result) == 1
    assert result[0]["decision"] == "stage_incomplete"
    assert "artifact_id" in result[0]


def test_load_evaluation_decisions_caps_at_max_artifacts(
    db_session: Session,
    make_project,
    make_artifact,
):
    proj = make_project(name="Many eval decisions")
    for _i in range(12):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="evaluation_decision",
            content=json.dumps(_make_eval_decision_payload()),
        )

    result = load_evaluation_decision_artifacts(db_session, proj.id, max_artifacts=8)
    assert len(result) == 8


def test_load_evaluation_decisions_ordered_by_artifact_id(
    db_session: Session,
    make_project,
    make_artifact,
):
    proj = make_project(name="Ordered eval decisions")
    for decision in ["stage_completed", "stage_incomplete", "manual_review_required"]:
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="evaluation_decision",
            content=json.dumps(_make_eval_decision_payload(decision=decision)),
        )

    result = load_evaluation_decision_artifacts(db_session, proj.id)
    assert len(result) == 3
    assert result[0]["decision"] == "stage_completed"
    assert result[1]["decision"] == "stage_incomplete"
    assert result[2]["decision"] == "manual_review_required"


# ---------------------------------------------------------------------------
# load_recovery_decision_artifacts
# ---------------------------------------------------------------------------


def test_load_recovery_decisions_returns_empty_when_no_artifacts(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No recovery decisions")
    result = load_recovery_decision_artifacts(db_session, proj.id)
    assert result == []


def test_load_recovery_decisions_includes_source_run_cross_context(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
    make_artifact,
):
    proj = make_project(name="Recovery cross-context project")
    task = make_task(project_id=proj.id, title="Failing task")
    run = make_execution_run(
        task_id=task.id,
        status=EXECUTION_RUN_STATUS_FAILED,
        failure_type="validation_failed",
    )

    payload = _make_recovery_decision_payload(
        source_task_id=task.id,
        source_run_id=run.id,
    )
    make_artifact(
        project_id=proj.id,
        task_id=task.id,
        artifact_type="recovery_decision",
        content=json.dumps(payload),
    )

    result = load_recovery_decision_artifacts(db_session, proj.id)
    assert len(result) == 1

    decision = result[0]
    assert decision["action"] == "reatomize"
    assert "artifact_id" in decision

    # Cross-context from ExecutionRun
    assert decision["source_run_context"] is not None
    assert decision["source_run_context"]["failure_type"] == "validation_failed"
    assert decision["source_run_context"]["run_id"] == run.id

    # Cross-context from Task
    assert decision["source_task_context"] is not None
    assert decision["source_task_context"]["task_id"] == task.id


def test_load_recovery_decisions_handles_missing_source_run_gracefully(
    db_session: Session,
    make_project,
    make_task,
    make_artifact,
):
    proj = make_project(name="Missing source run project")
    task = make_task(project_id=proj.id)

    payload = _make_recovery_decision_payload(
        source_task_id=task.id,
        source_run_id=99999,  # non-existent run
    )
    make_artifact(
        project_id=proj.id,
        task_id=task.id,
        artifact_type="recovery_decision",
        content=json.dumps(payload),
    )

    result = load_recovery_decision_artifacts(db_session, proj.id)
    assert len(result) == 1
    assert result[0]["source_run_context"] is None


def test_load_recovery_decisions_caps_at_max_artifacts(
    db_session: Session,
    make_project,
    make_task,
    make_artifact,
):
    proj = make_project(name="Many recovery decisions")
    task = make_task(project_id=proj.id)
    for _i in range(15):
        make_artifact(
            project_id=proj.id,
            task_id=task.id,
            artifact_type="recovery_decision",
            content=json.dumps(_make_recovery_decision_payload(source_task_id=task.id)),
        )

    result = load_recovery_decision_artifacts(db_session, proj.id, max_artifacts=10)
    assert len(result) == 10


# ---------------------------------------------------------------------------
# load_recovery_assignment_trios
# ---------------------------------------------------------------------------


def test_load_recovery_assignment_trios_returns_empty_when_no_artifacts(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No assignment artifacts")
    result = load_recovery_assignment_trios(db_session, proj.id)
    assert result == []


def test_load_recovery_assignment_trios_groups_consecutive_artifacts(
    db_session: Session,
    make_project,
    make_artifact,
):
    proj = make_project(name="Assignment trios project")
    # Create one complete trio in order
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_input",
        content=json.dumps(_make_assignment_input_payload(proj.id)),
    )
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_output",
        content=json.dumps(_make_assignment_output_payload()),
    )
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_compiled_plan",
        content=json.dumps(_make_compiled_plan_payload()),
    )

    result = load_recovery_assignment_trios(db_session, proj.id)
    assert len(result) == 1

    trio = result[0]
    assert trio["trio_index"] == 0
    assert trio["input"] is not None
    assert trio["output"] is not None
    assert trio["compiled_plan"] is not None

    assert trio["input"]["resolved_intent_type"] == "assign"
    assert trio["output"]["strategy"] == "continue_with_assignment"
    assert trio["compiled_plan"]["requires_replan"] is False


def test_load_recovery_assignment_trios_handles_two_complete_trios(
    db_session: Session,
    make_project,
    make_artifact,
):
    proj = make_project(name="Two trios project")
    for _ in range(2):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="recovery_assignment_input",
            content=json.dumps(_make_assignment_input_payload(proj.id)),
        )
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="recovery_assignment_output",
            content=json.dumps(_make_assignment_output_payload()),
        )
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="recovery_assignment_compiled_plan",
            content=json.dumps(_make_compiled_plan_payload()),
        )

    result = load_recovery_assignment_trios(db_session, proj.id)
    assert len(result) == 2
    assert result[0]["trio_index"] == 0
    assert result[1]["trio_index"] == 1


def test_load_recovery_assignment_trios_caps_at_max_trios(
    db_session: Session,
    make_project,
    make_artifact,
):
    proj = make_project(name="Many trios project")
    for _ in range(8):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="recovery_assignment_input",
            content=json.dumps(_make_assignment_input_payload(proj.id)),
        )
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="recovery_assignment_output",
            content=json.dumps(_make_assignment_output_payload()),
        )
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="recovery_assignment_compiled_plan",
            content=json.dumps(_make_compiled_plan_payload()),
        )

    result = load_recovery_assignment_trios(db_session, proj.id, max_trios=5)
    assert len(result) == 5


def test_load_recovery_assignment_trios_skips_incomplete_trailing_trio(
    db_session: Session,
    make_project,
    make_artifact,
):
    """An incomplete trailing trio (only input present) is silently skipped."""
    proj = make_project(name="Incomplete trio project")
    # One complete trio
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_input",
        content=json.dumps(_make_assignment_input_payload(proj.id)),
    )
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_output",
        content=json.dumps(_make_assignment_output_payload()),
    )
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_compiled_plan",
        content=json.dumps(_make_compiled_plan_payload()),
    )
    # Incomplete second trio (only input)
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="recovery_assignment_input",
        content=json.dumps(_make_assignment_input_payload(proj.id)),
    )

    result = load_recovery_assignment_trios(db_session, proj.id)
    assert len(result) == 1
