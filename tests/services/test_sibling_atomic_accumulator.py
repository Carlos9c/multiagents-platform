"""
Tests for the sibling accumulator in _run_atomic_generation_phase (Phase 2).

Verifies that atomic tasks generated from the first parent are passed as
sibling_atomic_summary to subsequent generate_atomic_tasks calls, and that
the first call receives None (no siblings yet).
"""

from __future__ import annotations

from unittest.mock import patch

from app.models.task import (
    EXECUTION_ENGINE,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
)
from app.services.project_workflow_service import _run_atomic_generation_phase


def _make_atomic_task(make_task, *, project_id, parent_task_id, title, task_type):
    """Create an atomic task in the DB under a given parent."""
    return make_task(
        project_id=project_id,
        parent_task_id=parent_task_id,
        planning_level=PLANNING_LEVEL_ATOMIC,
        title=title,
        task_type=task_type,
        executor_type=EXECUTION_ENGINE,
        status="pending",
    )


def test_first_atomic_call_receives_no_siblings(db_session, make_project, make_task):
    """The first generate_atomic_tasks call must receive sibling_atomic_summary=None."""
    project = make_project()
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)

    with patch("app.services.project_workflow_service.generate_atomic_tasks") as mock_gen:
        mock_gen.return_value = {"atomic_task_ids": [], "tasks_created": 0}
        _run_atomic_generation_phase(
            db=db_session,
            project_id=project.id,
            enable_technical_refinement=False,
        )

    mock_gen.assert_called_once()
    kwargs = mock_gen.call_args.kwargs
    assert kwargs["sibling_atomic_summary"] is None


def test_second_atomic_call_receives_siblings_from_first(db_session, make_project, make_task):
    """The second generate_atomic_tasks call must receive the siblings from the first."""
    project = make_project()
    # Two high-level tasks with no atomic children — both will be processed (id order)
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)

    # stub_parent already has atomic children — it will be SKIPPED by _has_atomic_children.
    # We put the "returned" atomic tasks under it so db.get() finds them.
    stub_parent = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)
    atomic_a = _make_atomic_task(
        make_task,
        project_id=project.id,
        parent_task_id=stub_parent.id,
        title="Set up database models",
        task_type="implementation",
    )
    atomic_b = _make_atomic_task(
        make_task,
        project_id=project.id,
        parent_task_id=stub_parent.id,
        title="Write tests for database models",
        task_type="testing",
    )

    # Call 1 → parent1; Call 2 → parent2 (stub_parent is skipped)
    call_results = [
        {"atomic_task_ids": [atomic_a.id, atomic_b.id], "tasks_created": 2},
        {"atomic_task_ids": [], "tasks_created": 0},
    ]

    with patch(
        "app.services.project_workflow_service.generate_atomic_tasks",
        side_effect=call_results,
    ) as mock_gen:
        _run_atomic_generation_phase(
            db=db_session,
            project_id=project.id,
            enable_technical_refinement=False,
        )

    assert mock_gen.call_count == 2
    first_siblings = mock_gen.call_args_list[0].kwargs["sibling_atomic_summary"]
    second_siblings = mock_gen.call_args_list[1].kwargs["sibling_atomic_summary"]

    assert first_siblings is None
    assert second_siblings is not None
    assert len(second_siblings) == 2
    titles = {s["title"] for s in second_siblings}
    types = {s["task_type"] for s in second_siblings}
    assert titles == {"Set up database models", "Write tests for database models"}
    assert types == {"implementation", "testing"}


def test_sibling_accumulator_includes_reused_tasks(db_session, make_project, make_task):
    """Reused atomic tasks (returned by the first call) must also appear as siblings."""
    project = make_project()
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)

    stub_parent = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)
    reused = _make_atomic_task(
        make_task,
        project_id=project.id,
        parent_task_id=stub_parent.id,
        title="Configure CI pipeline",
        task_type="configuration",
    )

    call_results = [
        {"atomic_task_ids": [reused.id], "tasks_reused": 1, "tasks_created": 0},
        {"atomic_task_ids": [], "tasks_created": 0},
    ]

    with patch(
        "app.services.project_workflow_service.generate_atomic_tasks",
        side_effect=call_results,
    ) as mock_gen:
        _run_atomic_generation_phase(
            db=db_session,
            project_id=project.id,
            enable_technical_refinement=False,
        )

    assert mock_gen.call_count == 2
    second_siblings = mock_gen.call_args_list[1].kwargs["sibling_atomic_summary"]
    assert second_siblings is not None
    assert len(second_siblings) == 1
    assert second_siblings[0]["title"] == "Configure CI pipeline"
    assert second_siblings[0]["task_type"] == "configuration"


def test_sibling_accumulator_empty_when_no_task_ids_returned(db_session, make_project, make_task):
    """When the first call returns no atomic_task_ids, the second receives None."""
    project = make_project()
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)
    make_task(project_id=project.id, planning_level=PLANNING_LEVEL_HIGH_LEVEL)

    call_results = [
        {"atomic_task_ids": [], "tasks_created": 0},
        {"atomic_task_ids": [], "tasks_created": 0},
    ]

    with patch(
        "app.services.project_workflow_service.generate_atomic_tasks",
        side_effect=call_results,
    ) as mock_gen:
        _run_atomic_generation_phase(
            db=db_session,
            project_id=project.id,
            enable_technical_refinement=False,
        )

    assert mock_gen.call_count == 2
    second_siblings = mock_gen.call_args_list[1].kwargs["sibling_atomic_summary"]
    assert second_siblings is None
