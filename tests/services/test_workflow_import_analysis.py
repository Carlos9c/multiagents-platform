"""Tests for analysis-aware planning in project_workflow_service._run_planner_if_needed."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.project_workflow_service import _run_planner_if_needed


def test_planner_skips_when_high_level_tasks_exist(db_session, make_project, make_task):
    project = make_project()
    make_task(project_id=project.id, planning_level="high_level")

    with (
        patch("app.services.project_workflow_service.generate_project_plan") as mock_base,
        patch(
            "app.services.project_workflow_service.generate_project_plan_with_analysis"
        ) as mock_evo,
    ):
        result = _run_planner_if_needed(db=db_session, project_id=project.id)

    assert result is True
    mock_base.assert_not_called()
    mock_evo.assert_not_called()


def test_planner_uses_base_planner_when_no_stored_analysis(db_session, make_project):
    project = make_project()

    with (
        patch("app.services.project_workflow_service.CodebaseAnalysisService") as MockAnalysis,
        patch("app.services.project_workflow_service.generate_project_plan") as mock_base,
        patch(
            "app.services.project_workflow_service.generate_project_plan_with_analysis"
        ) as mock_evo,
    ):
        MockAnalysis.return_value.get_analysis.return_value = None
        result = _run_planner_if_needed(db=db_session, project_id=project.id)

    assert result is True
    mock_base.assert_called_once_with(db=db_session, project_id=project.id)
    mock_evo.assert_not_called()


def test_planner_uses_evolutionary_planner_when_stored_analysis_exists(db_session, make_project):
    project = make_project()
    fake_analysis = MagicMock()

    with (
        patch("app.services.project_workflow_service.CodebaseAnalysisService") as MockAnalysis,
        patch("app.services.project_workflow_service.generate_project_plan") as mock_base,
        patch(
            "app.services.project_workflow_service.generate_project_plan_with_analysis"
        ) as mock_evo,
    ):
        MockAnalysis.return_value.get_analysis.return_value = fake_analysis
        result = _run_planner_if_needed(db=db_session, project_id=project.id)

    assert result is True
    mock_evo.assert_called_once_with(db=db_session, project_id=project.id, analysis=fake_analysis)
    mock_base.assert_not_called()
