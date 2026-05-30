"""
Tests for RecoveryAssignmentEvaluator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.recovery_assignment_evaluator import (
    RecoveryAssignmentEvaluator,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The recovery_assignment agent produced well-structured cluster proposals.",
    "issues": [],
    "suggestions": [],
}

_MODULE = "app.services.supervisor.evaluators.recovery_assignment_evaluator"


def _one_trio_context() -> list[dict]:
    return [
        {
            "trio_index": 0,
            "input": {
                "project_id": 1,
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
            },
            "output": {
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
            },
            "compiled_plan": {
                "strategy": "continue_with_assignment",
                "requires_replan": False,
                "assigned_task_ids": [50],
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
            },
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_healthy_when_no_trios(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No assignment trios")
    with patch(
        f"{_MODULE}.load_recovery_assignment_trios",
        return_value=[],
    ):
        result = RecoveryAssignmentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_calls_llm_once_when_trios_present(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Recovery assignment LLM test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

    with (
        patch(
            f"{_MODULE}.load_recovery_assignment_trios",
            return_value=_one_trio_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a recovery assignment supervisor.",
        ),
    ):
        result = RecoveryAssignmentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_user_prompt_includes_trio_data(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Recovery assignment prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_recovery_assignment_trios",
            return_value=_one_trio_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a recovery assignment supervisor.",
        ),
    ):
        RecoveryAssignmentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert str(proj.id) in prompt
    assert "continue_with_assignment" in prompt


def test_retries_on_invalid_llm_output(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Recovery assignment retry test")
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        {"verdict": "healthy"},  # missing findings → ValidationError
        _HEALTHY_RESPONSE,
    ]

    with (
        patch(
            f"{_MODULE}.load_recovery_assignment_trios",
            return_value=_one_trio_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value="You are a recovery assignment supervisor.",
        ),
    ):
        result = RecoveryAssignmentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"


def test_system_prompt_passed_to_user_prompt(
    db_session: Session,
    make_project,
):
    """resolve_system_prompt output appears in the LLM user prompt."""
    proj = make_project(name="Recovery assignment system prompt test")
    captured: list[str] = []
    mock_provider = MagicMock()
    sentinel = "UNIQUE_RECOVERY_ASSIGNMENT_SYSTEM_PROMPT_CONTENT"

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return _HEALTHY_RESPONSE

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            f"{_MODULE}.load_recovery_assignment_trios",
            return_value=_one_trio_context(),
        ),
        patch(
            f"{_MODULE}.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            f"{_MODULE}.resolve_system_prompt",
            return_value=sentinel,
        ),
    ):
        RecoveryAssignmentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert sentinel in captured[0]


def test_agent_name_is_recovery_assignment():
    assert RecoveryAssignmentEvaluator.AGENT_NAME == "recovery_assignment"
