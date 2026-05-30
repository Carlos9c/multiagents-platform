"""
Tests for the context_selection_agent evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.context_selection_agent_evaluator import (
    ContextSelectionAgentEvaluator,
    _select_runs,
)
from app.services.supervisor.trace_writer import EXECUTION_TRACE_FILENAME

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_context_selection_entry(
    task_id: int = 1,
    run_id: int = 100,
    call_type: str = "initial",
    candidate_count: int = 5,
    selected_count: int = 2,
) -> dict:
    candidates = [{"task_id": i, "title": f"Task {i}"} for i in range(candidate_count)]
    selected = [
        {"task_id": i, "title": f"Task {i}", "selection_reason": "same_functional_surface"}
        for i in range(selected_count)
    ]
    return {
        "agent": "context_selection_agent",
        "project_id": 1,
        "task_id": task_id,
        "run_id": run_id,
        "call_type": call_type,
        "inputs": {
            "current_task_title": f"Task {task_id} title",
            "current_task_type": "implementation",
            "current_task_objective": "Implement feature X",
            "current_task_acceptance_criteria": "Feature X works correctly",
            "relevant_files": ["app/models/user.py"],
            "key_decisions": [],
            "candidate_count": candidate_count,
            "candidate_titles": candidates,
        },
        "output_snapshot": {
            "selected_count": selected_count,
            "selected_tasks": selected,
            "not_selected_count": candidate_count - selected_count,
        },
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


def test_select_runs_prioritises_initial_over_skipped():
    entries = [
        _make_context_selection_entry(task_id=1, call_type="skipped"),
        _make_context_selection_entry(task_id=2, call_type="initial"),
        _make_context_selection_entry(task_id=3, call_type="initial"),
        _make_context_selection_entry(task_id=4, call_type="skipped"),
    ]
    result = _select_runs(entries)
    # initial should come first
    assert result[0]["call_type"] == "initial"
    assert result[1]["call_type"] == "initial"


def test_select_runs_caps_at_15():
    entries = [_make_context_selection_entry(task_id=i) for i in range(20)]
    result = _select_runs(entries)
    assert len(result) == 15


# ---------------------------------------------------------------------------
# ContextSelectionAgentEvaluator.evaluate — no trace
# ---------------------------------------------------------------------------


def test_context_selection_evaluator_returns_healthy_when_no_trace(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="No context selection trace")
    meta_dir = tmp_path / "meta"
    meta_dir.mkdir()
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
        return_value=paths,
    ):
        evaluator = ContextSelectionAgentEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


# ---------------------------------------------------------------------------
# ContextSelectionAgentEvaluator.evaluate — normal path
# ---------------------------------------------------------------------------


def test_context_selection_evaluator_calls_llm_once(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Context selection eval project")
    paths = _make_mock_paths(tmp_path, [_make_context_selection_entry()])

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "healthy",
        "findings": "The agent selected 2 of 5 candidates with concrete reasons.",
        "issues": [],
        "suggestions": [],
    }

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = ContextSelectionAgentEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_context_selection_evaluator_includes_file_index_in_prompt(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
    tmp_path: Path,
):
    """The user prompt must include the historical_file_index."""
    proj = make_project(name="File index test project")
    task1 = make_task(project_id=proj.id)
    run1 = make_execution_run(task_id=task1.id)
    run1.changed_files = json.dumps([{"path": "app/models/user.py"}])
    db_session.commit()

    paths = _make_mock_paths(tmp_path, [_make_context_selection_entry()])

    captured: list[str] = []
    mock_provider = MagicMock()

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return {
            "verdict": "healthy",
            "findings": "Selections are well-justified and the file index was used.",
            "issues": [],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        ContextSelectionAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured) == 1
    prompt = captured[0]
    # The file index should appear in the prompt
    assert "app/models/user.py" in prompt
    # The trace data should appear too
    assert "context_selection_agent" in prompt


def test_context_selection_evaluator_needs_attention_verdict(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Needs attention project")
    # All skipped entries — always nothing selected despite having candidates
    entries = [
        _make_context_selection_entry(
            task_id=i, call_type="initial", selected_count=0, candidate_count=8
        )
        for i in range(3)
    ]
    paths = _make_mock_paths(tmp_path, entries)

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "needs_attention",
        "findings": "The agent consistently selected 0 of 8 candidates across all runs.",
        "issues": ["Systematic under-selection despite populated catalog"],
        "suggestions": ["Review selection rules and reasons"],
    }

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        result = ContextSelectionAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "needs_attention"
    assert len(result.result.issues) == 1


def test_context_selection_evaluator_retries_on_invalid_output(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Retry test project")
    paths = _make_mock_paths(tmp_path, [_make_context_selection_entry()])

    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = [
        # First call: missing findings field → invalid
        {"verdict": "healthy"},
        # Second call: valid
        {
            "verdict": "healthy",
            "findings": "Good selections with concrete reasons on retry.",
            "issues": [],
            "suggestions": [],
        },
    ]

    with (
        patch(
            "app.services.supervisor.evaluators._execution_helpers._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.context_selection_agent_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        result = ContextSelectionAgentEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert mock_provider.generate_structured.call_count == 2
    assert result.result is not None
    assert result.result.verdict == "healthy"
