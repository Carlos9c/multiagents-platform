from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import TASK_STATUS_COMPLETED, TASK_STATUS_PENDING, Task
from app.schemas.planner import PlannedTask, PlannerOutput
from app.schemas.project_start import ProjectStartRequest
from app.services.analysis.base import CodebaseAnalysis, FileAnalysis
from app.services.plan_history_service import PlanHistoryService
from app.services.project_start_service import ActiveTasksError, ProjectStartService
from app.services.project_storage import ProjectStorageService

# ── helpers ───────────────────────────────────────────────────────────────────


def _planned_task() -> PlannedTask:
    return PlannedTask(
        title="Implement the core system features and components",
        description="Build the primary system components and features needed for the project.",
        summary="Core system implementation delivering the main capability.",
        objective="Have a working system that fulfils the project requirements.",
        implementation_notes=(
            "Design the main modules, implement the core business logic, and wire up all "
            "dependencies correctly following the project conventions."
        ),
        acceptance_criteria="The system behaves as specified and passes all defined acceptance checks.",
        technical_constraints="Must use the established stack and conventions without introducing new frameworks.",
        out_of_scope="Performance optimisation, deployment automation, and user documentation.",
        priority="high",
        task_type="implementation",
    )


def fake_planner_output() -> PlannerOutput:
    return PlannerOutput(
        plan_summary="A coherent test plan covering the full scope of the described project.",
        tasks=[_planned_task() for _ in range(4)],
    )


def fake_analysis() -> CodebaseAnalysis:
    return CodebaseAnalysis(
        analyzed_at="2026-01-01T00:00:00+00:00",
        source_path="/some/path",
        total_files=1,
        project_summary="A simple test project.",
        main_technologies=["Python"],
        entry_points=["main.py"],
        files=[
            FileAnalysis(
                path="main.py",
                file_type="code",
                language="Python",
                summary="Entry point.",
                key_elements=[],
                dependencies=[],
            )
        ],
    )


def make_service(
    tmp_path: Path,
    *,
    existing_analysis: CodebaseAnalysis | None = None,
) -> tuple[ProjectStartService, MagicMock, MagicMock]:
    """Returns (service, mock_import_svc, mock_analysis_svc)."""
    storage = ProjectStorageService(root=tmp_path / "root")
    plan_history = PlanHistoryService()

    import_svc = MagicMock()
    import_svc.import_source.return_value = MagicMock()

    analysis_svc = MagicMock()
    analysis_svc.analyze.return_value = fake_analysis()
    analysis_svc.get_analysis.return_value = existing_analysis

    svc = ProjectStartService(
        storage_service=storage,
        import_service=import_svc,
        analysis_service=analysis_svc,
        plan_history_service=plan_history,
    )
    return svc, import_svc, analysis_svc


# ── Validation ────────────────────────────────────────────────────────────────


def test_missing_name_raises(db_session: Session, tmp_path: Path):
    svc, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="name is required"):
        svc.start(db=db_session, request=ProjectStartRequest())


def test_project_id_without_source_raises(db_session: Session, tmp_path: Path):
    svc, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="source_path is required"):
        svc.start(db=db_session, request=ProjectStartRequest(project_id=1))


# ── Scenario 1: new project, no source ───────────────────────────────────────


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_1_creates_project_and_tasks(
    mock_base, mock_evo, db_session: Session, tmp_path: Path
):
    mock_base.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path)

    response = svc.start(db=db_session, request=ProjectStartRequest(name="My Project"))

    assert response.project_id > 0
    assert response.tasks_created == 4
    assert response.artifact_id > 0


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_1_uses_base_planner(mock_base, mock_evo, db_session: Session, tmp_path: Path):
    mock_base.return_value = fake_planner_output()
    svc, import_svc, analysis_svc = make_service(tmp_path)

    svc.start(db=db_session, request=ProjectStartRequest(name="My Project"))

    mock_base.assert_called_once()
    mock_evo.assert_not_called()
    import_svc.import_source.assert_not_called()
    analysis_svc.analyze.assert_not_called()


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_1_analysis_performed_false(
    mock_base, mock_evo, db_session: Session, tmp_path: Path
):
    mock_base.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path)

    response = svc.start(db=db_session, request=ProjectStartRequest(name="My Project"))

    assert not response.analysis_performed


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_1_records_plan_history_cycle_1(
    mock_base, mock_evo, db_session: Session, tmp_path: Path
):
    mock_base.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path)

    response = svc.start(db=db_session, request=ProjectStartRequest(name="My Project"))

    assert response.cycle_number == 1


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_1_updates_last_planned_at(
    mock_base, mock_evo, db_session: Session, tmp_path: Path
):
    mock_base.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path)

    response = svc.start(db=db_session, request=ProjectStartRequest(name="My Project"))

    project = db_session.get(Project, response.project_id)
    assert project is not None
    assert project.last_planned_at is not None


# ── Scenario 2: new project + source_path ────────────────────────────────────


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_2_imports_and_analyzes(mock_base, mock_evo, db_session: Session, tmp_path: Path):
    mock_evo.return_value = fake_planner_output()
    svc, import_svc, analysis_svc = make_service(tmp_path)

    svc.start(
        db=db_session,
        request=ProjectStartRequest(name="My Project", source_path=str(tmp_path)),
    )

    import_svc.import_source.assert_called_once()
    analysis_svc.analyze.assert_called_once()


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_2_uses_evolutionary_planner(
    mock_base, mock_evo, db_session: Session, tmp_path: Path
):
    mock_evo.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path)

    svc.start(
        db=db_session,
        request=ProjectStartRequest(name="My Project", source_path=str(tmp_path)),
    )

    mock_evo.assert_called_once()
    mock_base.assert_not_called()


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_2_analysis_performed_true(
    mock_base, mock_evo, db_session: Session, tmp_path: Path
):
    mock_evo.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path)

    response = svc.start(
        db=db_session,
        request=ProjectStartRequest(name="My Project", source_path=str(tmp_path)),
    )

    assert response.analysis_performed


# ── Scenario 3: existing project + source + manual_update=False ──────────────


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_3_imports_but_skips_reanalysis(
    mock_base, mock_evo, db_session: Session, tmp_path: Path, make_project
):
    mock_evo.return_value = fake_planner_output()
    svc, import_svc, analysis_svc = make_service(tmp_path, existing_analysis=fake_analysis())
    project = make_project()

    svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
            manual_update=False,
        ),
    )

    import_svc.import_source.assert_called_once()
    analysis_svc.analyze.assert_not_called()
    analysis_svc.get_analysis.assert_called_once()


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_3_uses_existing_analysis_for_evolutionary_planner(
    mock_base, mock_evo, db_session: Session, tmp_path: Path, make_project
):
    mock_evo.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path, existing_analysis=fake_analysis())
    project = make_project()

    svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
            manual_update=False,
        ),
    )

    mock_evo.assert_called_once()
    mock_base.assert_not_called()


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_3_no_existing_analysis_falls_back_to_base_planner(
    mock_base, mock_evo, db_session: Session, tmp_path: Path, make_project
):
    mock_base.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path, existing_analysis=None)
    project = make_project()

    svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
            manual_update=False,
        ),
    )

    mock_base.assert_called_once()
    mock_evo.assert_not_called()


# ── Scenario 4: existing project + source + manual_update=True ───────────────


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_scenario_4_imports_and_reanalyzes(
    mock_base, mock_evo, db_session: Session, tmp_path: Path, make_project
):
    mock_evo.return_value = fake_planner_output()
    svc, import_svc, analysis_svc = make_service(tmp_path)
    project = make_project()

    response = svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
            manual_update=True,
        ),
    )

    import_svc.import_source.assert_called_once()
    analysis_svc.analyze.assert_called_once()
    assert response.analysis_performed
    mock_evo.assert_called_once()
    mock_base.assert_not_called()


# ── Guardrail ─────────────────────────────────────────────────────────────────


def test_guardrail_blocks_when_non_terminal_tasks_exist(
    db_session: Session, tmp_path: Path, make_project, make_task
):
    svc, _, _ = make_service(tmp_path)
    project = make_project()
    make_task(project_id=project.id, status=TASK_STATUS_PENDING)

    with pytest.raises(ActiveTasksError):
        svc.start(
            db=db_session,
            request=ProjectStartRequest(
                project_id=project.id,
                source_path=str(tmp_path),
            ),
        )


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_guardrail_allows_when_all_tasks_terminal(
    mock_base, mock_evo, db_session: Session, tmp_path: Path, make_project, make_task
):
    mock_evo.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path, existing_analysis=fake_analysis())
    project = make_project()
    make_task(project_id=project.id, status=TASK_STATUS_COMPLETED)

    response = svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
        ),
    )

    assert response.project_id == project.id


def test_project_not_found_raises(db_session: Session, tmp_path: Path):
    from app.services.project_start_service import ProjectNotFoundError

    svc, _, _ = make_service(tmp_path)
    with pytest.raises(ProjectNotFoundError, match="not found"):
        svc.start(
            db=db_session,
            request=ProjectStartRequest(project_id=9999, source_path=str(tmp_path)),
        )


def test_source_path_not_found_raises(db_session: Session, tmp_path: Path):
    from app.services.project_start_service import SourcePathNotFoundError

    svc, _, _ = make_service(tmp_path)
    with pytest.raises(SourcePathNotFoundError, match="does not exist"):
        svc.start(
            db=db_session,
            request=ProjectStartRequest(
                name="Test",
                description="desc",
                source_path=str(tmp_path / "nonexistent"),
            ),
        )


# ── Cycle number ──────────────────────────────────────────────────────────────


@patch("app.services.project_start_service.call_evolutionary_planner_model")
@patch("app.services.project_start_service.call_planner_model")
def test_cycle_number_increments_on_second_run(
    mock_base, mock_evo, db_session: Session, tmp_path: Path, make_project, make_task
):
    mock_evo.return_value = fake_planner_output()
    svc, _, _ = make_service(tmp_path, existing_analysis=fake_analysis())
    project = make_project()

    # first cycle
    first = svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
        ),
    )
    assert first.cycle_number == 1

    # mark tasks as terminal so the guardrail passes
    db_session.query(Task).filter(Task.project_id == project.id).update(
        {"status": TASK_STATUS_COMPLETED}
    )
    db_session.commit()

    # second cycle
    mock_evo.return_value = fake_planner_output()
    second = svc.start(
        db=db_session,
        request=ProjectStartRequest(
            project_id=project.id,
            source_path=str(tmp_path),
        ),
    )
    assert second.cycle_number == 2
