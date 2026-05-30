"""
Tests for the environment_manager_agent evaluator.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.execution_run import EXECUTION_RUN_STATUS_FAILED, EXECUTION_RUN_STATUS_SUCCEEDED
from app.services.supervisor.evaluators.environment_manager_agent_evaluator import (
    EnvironmentManagerAgentEvaluator,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_run_with_env_manager(
    db_session,
    make_project,
    make_task,
    make_execution_run,
    *,
    status: str = EXECUTION_RUN_STATUS_SUCCEEDED,
    blockers: list[str] | None = None,
    task_type: str = "implementation",
):
    proj = make_project()
    task = make_task(project_id=proj.id, task_type=task_type)
    run = make_execution_run(
        task_id=task.id,
        execution_agent_sequence=json.dumps(
            ["context_selection_agent", "environment_manager_agent", "command_runner_agent"]
        ),
    )
    run.status = status
    if blockers is not None:
        run.blockers_found = json.dumps(blockers)
    db_session.commit()
    return proj, task, run


# ---------------------------------------------------------------------------
# EnvironmentManagerAgentEvaluator.evaluate — no runs with env_manager
# ---------------------------------------------------------------------------


def test_env_manager_evaluator_returns_healthy_when_never_invoked(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    """If env_manager never ran, return healthy with explanatory note."""
    proj = make_project(name="No env manager project")
    task = make_task(project_id=proj.id)
    run = make_execution_run(
        task_id=task.id,
        execution_agent_sequence=json.dumps(["context_selection_agent", "code_change_agent"]),
    )
    run.status = EXECUTION_RUN_STATUS_SUCCEEDED
    db_session.commit()

    evaluator = EnvironmentManagerAgentEvaluator()
    result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


# ---------------------------------------------------------------------------
# EnvironmentManagerAgentEvaluator.evaluate — normal path
# ---------------------------------------------------------------------------


def test_env_manager_evaluator_calls_llm_once(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj, task, run = _make_run_with_env_manager(
        db_session,
        make_project,
        make_task,
        make_execution_run,
        status=EXECUTION_RUN_STATUS_SUCCEEDED,
    )

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "healthy",
        "findings": "environment_manager_agent successfully installed missing packages in 1 run.",
        "issues": [],
        "suggestions": [],
    }

    with (
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = EnvironmentManagerAgentEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_env_manager_evaluator_degraded_for_repeated_failures(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj = make_project(name="Repeated failures project")
    runs = []
    for i in range(3):
        task = make_task(project_id=proj.id)
        run = make_execution_run(
            task_id=task.id,
            execution_agent_sequence=json.dumps(
                ["context_selection_agent", "environment_manager_agent"]
            ),
        )
        run.status = EXECUTION_RUN_STATUS_FAILED
        run.blockers_found = json.dumps([f"ENV_INSTALL_FAILED:torch=={i}"])
        runs.append((run, task))
    db_session.commit()

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "degraded",
        "findings": "3 out of 3 env_manager runs ended with ENV_INSTALL_FAILED — torch package repeatedly fails to install.",
        "issues": ["torch installation fails repeatedly"],
        "suggestions": ["Add torch to the base RuntimeSpec or approve it manually"],
    }

    with (
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        result = EnvironmentManagerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "degraded"
    assert len(result.result.issues) >= 1


def test_env_manager_evaluator_prioritises_failed_runs(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    """Failed runs should be included in the evaluation even with many succeeded runs."""
    proj = make_project(name="Priority test project")

    # Add many succeeded runs
    for _ in range(12):
        task = make_task(project_id=proj.id)
        run = make_execution_run(
            task_id=task.id,
            execution_agent_sequence=json.dumps(["environment_manager_agent"]),
        )
        run.status = EXECUTION_RUN_STATUS_SUCCEEDED

    # Add 2 failed runs
    failed_tasks = []
    for _ in range(2):
        task = make_task(project_id=proj.id)
        run = make_execution_run(
            task_id=task.id,
            execution_agent_sequence=json.dumps(["environment_manager_agent"]),
        )
        run.status = EXECUTION_RUN_STATUS_FAILED
        run.blockers_found = json.dumps(["ENV_INSTALL_FAILED:scipy"])
        failed_tasks.append(task.title)

    db_session.commit()

    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return {
            "verdict": "needs_attention",
            "findings": "2 failed runs detected with ENV_INSTALL_FAILED.",
            "issues": ["scipy install failed twice"],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        EnvironmentManagerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    # Failed runs content should appear in the prompt
    assert "ENV_INSTALL_FAILED" in captured[0]


def test_env_manager_evaluator_retries_on_invalid_output(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj, task, run = _make_run_with_env_manager(
        db_session,
        make_project,
        make_task,
        make_execution_run,
        status=EXECUTION_RUN_STATUS_SUCCEEDED,
    )

    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        # First call: missing findings → invalid
        {"verdict": "healthy"},
        # Retry: valid
        {
            "verdict": "healthy",
            "findings": "Package installation succeeded on retry evaluation.",
            "issues": [],
            "suggestions": [],
        },
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_manager_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        result = EnvironmentManagerAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"
