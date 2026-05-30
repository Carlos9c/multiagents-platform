"""
Tests for the orchestrator evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.orchestrator_evaluator import (
    OrchestratorEvaluator,
    _select_runs,
)
from app.services.supervisor.trace_writer import EXECUTION_TRACE_FILENAME

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_orchestrator_entry(
    task_id: int = 1,
    run_id: int = 100,
    final_decision: str = "finish",
    forced_terminal: bool = False,
    invalid_decision_count: int = 0,
    normalized_decision_count: int = 0,
    steps_used: int = 3,
    agent_calls_used: int = 3,
    repair_attempts_used: int = 0,
) -> dict:
    return {
        "agent": "orchestrator",
        "project_id": 1,
        "task_id": task_id,
        "run_id": run_id,
        "execution_agent_sequence": [
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
        ],
        "steps_used": steps_used,
        "agent_calls_used": agent_calls_used,
        "repair_attempts_used": repair_attempts_used,
        "final_decision": final_decision,
        "forced_terminal": forced_terminal,
        "budget_max": {"max_steps": 8, "max_agent_calls": 8, "max_repair_attempts": 2},
        "invalid_decision_count": invalid_decision_count,
        "normalized_decision_count": normalized_decision_count,
        "error_diagnoses": [],
    }


def _make_mock_paths(tmp_path: Path, entries: list[dict]) -> MagicMock:
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    trace_path = meta_dir / EXECUTION_TRACE_FILENAME
    content = "".join(json.dumps(e) + "\n" for e in entries)
    trace_path.write_text(content, encoding="utf-8")
    paths = MagicMock()
    paths.project_meta_dir = meta_dir
    return paths


# ---------------------------------------------------------------------------
# _select_runs
# ---------------------------------------------------------------------------


def test_select_runs_prioritises_failed_decisions():
    entries = [
        _make_orchestrator_entry(task_id=1, final_decision="finish"),
        _make_orchestrator_entry(task_id=2, final_decision="reject"),
        _make_orchestrator_entry(task_id=3, final_decision="budget_exceeded"),
        _make_orchestrator_entry(task_id=4, final_decision="finish"),
    ]
    result = _select_runs(entries)
    # Reject and budget_exceeded should come first
    assert result[0]["final_decision"] in {"reject", "budget_exceeded", "env_manager_rejected"}
    assert result[1]["final_decision"] in {"reject", "budget_exceeded", "env_manager_rejected"}


def test_select_runs_caps_at_15():
    entries = [_make_orchestrator_entry(task_id=i) for i in range(20)]
    result = _select_runs(entries)
    assert len(result) == 15


# ---------------------------------------------------------------------------
# OrchestratorEvaluator.evaluate — no trace
# ---------------------------------------------------------------------------


def test_orchestrator_evaluator_returns_healthy_when_no_trace(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="No orchestrator trace")
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
        return_value=paths,
    ):
        evaluator = OrchestratorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


# ---------------------------------------------------------------------------
# OrchestratorEvaluator.evaluate — normal path
# ---------------------------------------------------------------------------


def test_orchestrator_evaluator_calls_llm_once(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Orchestrator eval project")
    paths = _make_mock_paths(tmp_path, [_make_orchestrator_entry()])

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "healthy",
        "findings": "The orchestrator consistently made voluntary finish decisions with low invalids.",
        "issues": [],
        "suggestions": [],
    }

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = OrchestratorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_orchestrator_evaluator_degraded_verdict_propagates(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Degraded orchestrator project")
    entries = [
        _make_orchestrator_entry(
            task_id=i,
            final_decision="budget_exceeded",
            forced_terminal=True,
            invalid_decision_count=3,
            normalized_decision_count=2,
        )
        for i in range(5)
    ]
    paths = _make_mock_paths(tmp_path, entries)

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "degraded",
        "findings": "All runs ended with budget_exceeded and high invalid_decision_count.",
        "issues": ["5/5 runs exhausted budget", "Average 3 invalid decisions per run"],
        "suggestions": ["Improve orchestrator prompt to reduce invalid decisions"],
    }

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = OrchestratorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "degraded"
    assert len(result.result.issues) == 2


def test_orchestrator_evaluator_prompt_includes_trace_data(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """The user prompt must include run data from the trace entries."""
    proj = make_project(name="Prompt content project")
    entry = _make_orchestrator_entry(
        task_id=42,
        final_decision="reject",
        invalid_decision_count=2,
    )
    paths = _make_mock_paths(tmp_path, [entry])

    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return {
            "verdict": "needs_attention",
            "findings": "One reject with 2 invalid decisions detected.",
            "issues": ["reject decision with 2 invalids"],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        OrchestratorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    assert "reject" in prompt
    assert "invalid_decision_count" in prompt


def test_orchestrator_evaluator_retries_on_invalid_output(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """If first LLM response is invalid, evaluator retries once."""
    proj = make_project(name="Retry test project")
    paths = _make_mock_paths(tmp_path, [_make_orchestrator_entry()])

    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        # First call returns invalid (missing required field)
        {"verdict": "healthy"},
        # Retry returns valid
        {
            "verdict": "needs_attention",
            "findings": "One run had forced terminal after retry.",
            "issues": ["forced terminal detected"],
            "suggestions": [],
        },
    ]

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.orchestrator_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        result = OrchestratorEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "needs_attention"
