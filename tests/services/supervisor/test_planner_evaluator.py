"""
Tests for the planner evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_REATOMIZED,
)
from app.services.supervisor.evaluators.planner_evaluator import (
    PlannerEvaluator,
    _build_user_prompt,
    _compute_task_stats,
    _load_planner_trace_entries,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(make_project) -> Project:
    return make_project(name="Test Project", description="A simple test project.")


@pytest.fixture()
def planning_trace_file(tmp_path: Path) -> Path:
    """Return a path where a planning_trace.jsonl can be written."""
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    return meta_dir / "planning_trace.jsonl"


# ---------------------------------------------------------------------------
# _load_planner_trace_entries
# ---------------------------------------------------------------------------


def test_planner_evaluator_returns_not_supervised_when_no_trace(
    db_session: Session, make_project, tmp_path: Path
):
    proj = make_project(name="No-trace project")
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"

    with patch(
        "app.services.supervisor.evaluators.planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = PlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert result.result is None


def test_load_planner_trace_entries_returns_empty_when_no_file(tmp_path: Path):
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"
    with patch(
        "app.services.supervisor.evaluators.planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_planner_trace_entries(1)
    assert result == []


def test_load_planner_trace_entries_filters_by_agent(tmp_path: Path, planning_trace_file: Path):
    planning_trace_file.write_text(
        json.dumps({"agent": "planner", "call_type": "initial"})
        + "\n"
        + json.dumps({"agent": "atomic_task_generator", "call_type": "initial"})
        + "\n"
        + json.dumps({"agent": "planner", "call_type": "evolutionary"})
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = planning_trace_file.parent
    with patch(
        "app.services.supervisor.evaluators.planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_planner_trace_entries(1)
    assert len(result) == 2
    assert all(e["agent"] == "planner" for e in result)


def test_load_planner_trace_entries_skips_invalid_json(tmp_path: Path, planning_trace_file: Path):
    planning_trace_file.write_text(
        '{"agent": "planner", "call_type": "initial"}\n'
        "THIS IS NOT JSON\n"
        '{"agent": "planner", "call_type": "evolutionary"}\n',
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = planning_trace_file.parent
    with patch(
        "app.services.supervisor.evaluators.planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_planner_trace_entries(1)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# _compute_task_stats
# ---------------------------------------------------------------------------


def test_compute_task_stats_returns_correct_counts(
    db_session: Session,
    make_project,
    make_task,
):
    proj = make_project(name="Stats test")
    # High-level parent
    parent = make_task(
        project_id=proj.id,
        title="Parent task",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    # Atomic children
    make_task(
        project_id=proj.id,
        title="Child 1",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_COMPLETED,
    )
    make_task(
        project_id=proj.id,
        title="Child 2",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_REATOMIZED,
    )
    make_task(
        project_id=proj.id,
        title="Child 3",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_FAILED,
    )

    stats = _compute_task_stats(db_session, proj.id)

    assert stats["total_atomic_tasks"] == 3
    assert len(stats["high_level_tasks"]) == 1
    hl = stats["high_level_tasks"][0]
    assert hl["atomic_tasks_count"] == 3
    assert hl["completed_count"] == 1
    assert hl["cancelled_count"] == 0
    assert hl["reatomize_events"] == 1
    # No cancelled tasks → denominator is 3, completion_rate = 1/3
    assert hl["completion_rate"] == round(1 / 3, 2)


def test_compute_task_stats_excludes_cancelled_from_completion_rate(
    db_session: Session,
    make_project,
    make_task,
):
    """Cancelled tasks (user decisions) must not count against completion_rate.

    Scenario: 1 completed, 1 failed, 2 cancelled.
    completion_rate = completed / (total - cancelled) = 1 / (4 - 2) = 0.5.
    If cancelled were included: 1/4 = 0.25 — misleadingly low.
    """
    proj = make_project(name="Cancelled stats test")
    parent = make_task(
        project_id=proj.id,
        title="Feature module",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Core impl",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_COMPLETED,
    )
    make_task(
        project_id=proj.id,
        title="Broken task",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_FAILED,
    )
    make_task(
        project_id=proj.id,
        title="Cancelled A",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_CANCELLED,
    )
    make_task(
        project_id=proj.id,
        title="Cancelled B",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_CANCELLED,
    )

    stats = _compute_task_stats(db_session, proj.id)

    hl = stats["high_level_tasks"][0]
    assert hl["atomic_tasks_count"] == 4
    assert hl["cancelled_count"] == 2
    assert hl["completed_count"] == 1
    # denominator = 4 - 2 = 2 (excludes cancelled)
    assert hl["completion_rate"] == 0.5

    # Global stats also correct
    assert stats["total_atomic_tasks"] == 4
    assert stats["total_cancelled_tasks"] == 2
    assert stats["overall_completion_rate"] == 0.5


def test_compute_task_stats_all_cancelled_gives_none_rate(
    db_session: Session,
    make_project,
    make_task,
):
    """When all tasks are cancelled, executable_total=0 → completion_rate=None."""
    proj = make_project(name="All cancelled")
    parent = make_task(
        project_id=proj.id,
        title="Module",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Cancelled 1",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_CANCELLED,
    )
    make_task(
        project_id=proj.id,
        title="Cancelled 2",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_CANCELLED,
    )

    stats = _compute_task_stats(db_session, proj.id)

    hl = stats["high_level_tasks"][0]
    assert hl["cancelled_count"] == 2
    assert hl["completion_rate"] is None  # executable_total = 0
    assert stats["overall_completion_rate"] is None


def test_compute_task_stats_handles_no_children(
    db_session: Session,
    make_project,
    make_task,
):
    proj = make_project(name="No children project")
    make_task(
        project_id=proj.id,
        title="Parent only",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )

    stats = _compute_task_stats(db_session, proj.id)
    assert stats["total_atomic_tasks"] == 0
    assert stats["overall_completion_rate"] is None


# ---------------------------------------------------------------------------
# _build_user_prompt — validate_builder_inputs parity
# ---------------------------------------------------------------------------


def test_build_user_prompt_validates_builder_inputs():
    """build_user_prompt must pass validate_builder_inputs without raising."""
    # Should not raise
    _build_user_prompt(
        project_id=1,
        project_name="Test",
        project_description="A description.",
        system_prompt="You are an evaluator.",
        trace_entries=[{"agent": "planner", "reasoning": "decided to split"}],
        task_stats={"total_atomic_tasks": 5, "high_level_tasks": []},
    )


# ---------------------------------------------------------------------------
# PlannerEvaluator.evaluate — LLM call is mocked
# ---------------------------------------------------------------------------


def _make_llm_output() -> dict:
    return {
        "verdict": "healthy",
        "findings": "The planner produced well-scoped tasks covering the full project scope.",
        "issues": [],
        "suggestions": ["Consider adding more implementation_notes detail."],
    }


def test_planner_evaluator_calls_llm_and_returns_output(
    db_session: Session,
    make_project,
    make_task,
    tmp_path: Path,
):
    proj = make_project(name="Eval test")
    parent = make_task(
        project_id=proj.id,
        title="Backend API",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Create endpoints",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_COMPLETED,
    )

    # Mock the storage paths
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "agent": "planner",
                "call_type": "initial",
                "project_id": proj.id,
                "reasoning": "Split into backend and frontend workstreams.",
                "tasks_produced_count": 2,
                "task_titles": ["Backend API", "Frontend"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _make_llm_output()

    with (
        patch(
            "app.services.supervisor.evaluators.planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = PlannerEvaluator()
        result = evaluator.evaluate(
            db=db_session,
            project_id=proj.id,
            project_name=proj.name,
            project_description=proj.description or "",
        )

    assert result.result is not None
    assert result.result.verdict == "healthy"
    assert "well-scoped" in result.result.findings
    assert mock_provider.generate_structured.called


def test_planner_evaluator_retries_on_validation_error(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Retry test")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "agent": "planner",
                "call_type": "initial",
                "project_id": proj.id,
                "reasoning": "Split into backend and frontend.",
                "tasks_produced_count": 1,
                "task_titles": ["Backend API"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    # First call returns invalid output (missing required fields), second is valid
    mock_provider.generate_structured.side_effect = [
        {"verdict": "INVALID"},  # will fail validation
        _make_llm_output(),
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = PlannerEvaluator()
        result = evaluator.evaluate(
            db=db_session,
            project_id=proj.id,
            project_name=proj.name,
            project_description=proj.description or "",
        )

    assert result.result is not None
    assert result.result.verdict == "healthy"
    assert mock_provider.generate_structured.call_count == 2
