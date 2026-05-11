from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.planner import PlannerOutput
from app.schemas.project_start import ProjectStartRequest, ProjectStartResponse
from app.services.analysis import CodebaseAnalysis, CodebaseAnalysisService
from app.services.artifacts import create_artifact
from app.services.import_source_service import ImportSourceService
from app.services.plan_history_service import PlanHistoryService
from app.services.planner import validate_task_quality
from app.services.planner_client import call_evolutionary_planner_model, call_planner_model
from app.services.project_storage import ProjectStoragePaths, ProjectStorageService

logger = logging.getLogger(__name__)


class ActiveTasksError(Exception):
    """Raised when a project has non-terminal tasks blocking a new planning cycle."""


class ProjectNotFoundError(Exception):
    """Raised when the requested project_id does not exist in the database."""


class SourcePathNotFoundError(Exception):
    """Raised when source_path is provided but the filesystem path does not exist."""


class ProjectStartService:
    """
    Unified service for starting or continuing a project.

    Handles five scenarios based on the request payload:
      1. No source_path, no project_id  → new project + base plan
      2. source_path, no project_id     → new project + import + analyze + evolutionary plan
      3. source_path + project_id + manual_update=False
                                        → existing project + import (no re-analyze) + evolutionary plan
      4. source_path + project_id + manual_update=True
                                        → existing project + import + re-analyze + evolutionary plan
      5. project_id + no source_path    → raises ValueError (400)
    """

    def __init__(
        self,
        storage_service: ProjectStorageService | None = None,
        import_service: ImportSourceService | None = None,
        analysis_service: CodebaseAnalysisService | None = None,
        plan_history_service: PlanHistoryService | None = None,
    ) -> None:
        self._storage = storage_service or ProjectStorageService()
        self._import = import_service or ImportSourceService(storage_service=self._storage)
        self._analysis = analysis_service or CodebaseAnalysisService(storage_service=self._storage)
        self._plan_history = plan_history_service or PlanHistoryService()

    def start(self, db: Session, request: ProjectStartRequest) -> ProjectStartResponse:
        self._validate_request(request)

        if request.project_id is None:
            return self._start_new_project(db, request)
        else:
            return self._continue_project(db, request)

    # ── private: routing ──────────────────────────────────────────────────

    def _validate_request(self, request: ProjectStartRequest) -> None:
        if request.project_id is None and not request.name:
            raise ValueError("name is required when creating a new project")
        if request.project_id is not None and request.source_path is None and not request.use_existing_source:
            raise ValueError(
                "source_path is required when project_id is provided "
                "(or set use_existing_source=True to reuse the existing source_dir)"
            )
        if request.source_path is not None:
            resolved = Path(request.source_path).expanduser().resolve()
            if not resolved.exists():
                raise SourcePathNotFoundError(f"Source path does not exist: {request.source_path}")

    def _start_new_project(self, db: Session, request: ProjectStartRequest) -> ProjectStartResponse:
        project = Project(
            name=request.name,
            description=request.description,
            enable_technical_refinement=request.enable_technical_refinement,
            plan_version=1,
        )
        db.add(project)
        db.flush()

        paths = self._storage.ensure_project_storage(project.id)
        analysis: CodebaseAnalysis | None = None
        analysis_performed = False

        if request.source_path is not None:
            # Scenario 2
            self._import.import_source(project.id, request.source_path)
            analysis = self._analysis.analyze(project.id, request.source_path)
            analysis_performed = True
            planner_output = call_evolutionary_planner_model(
                project_name=project.name,
                project_description=project.description or "",
                codebase_analysis=analysis,
            )
        else:
            # Scenario 1
            planner_output = call_planner_model(
                project_name=project.name,
                project_description=project.description or "",
            )

        response = self._finalize(
            db=db,
            project=project,
            paths=paths,
            planner_output=planner_output,
            source_path=request.source_path,
            analysis_performed=analysis_performed,
        )
        db.commit()
        return response

    def _continue_project(self, db: Session, request: ProjectStartRequest) -> ProjectStartResponse:
        project = db.get(Project, request.project_id)
        if not project:
            raise ProjectNotFoundError(f"Project {request.project_id} not found")

        self._check_no_active_tasks(db, project)

        paths = self._storage.ensure_project_storage(project.id)

        analysis: CodebaseAnalysis | None
        analysis_performed = False

        if request.use_existing_source:
            # Scenario 6: disruptive restart — source_dir is already canonical, reuse cached analysis
            analysis = self._analysis.get_analysis(project.id)
        else:
            # Import always for standard continuation scenarios
            self._import.import_source(project.id, request.source_path)  # type: ignore[arg-type]

            if request.manual_update:
                # Scenario 4: re-analyze
                analysis = self._analysis.analyze(project.id, request.source_path)  # type: ignore[arg-type]
                analysis_performed = True
            else:
                # Scenario 3: reuse existing analysis
                analysis = self._analysis.get_analysis(project.id)

        if analysis is not None:
            planner_output = call_evolutionary_planner_model(
                project_name=project.name,
                project_description=project.description or "",
                codebase_analysis=analysis,
            )
        else:
            planner_output = call_planner_model(
                project_name=project.name,
                project_description=project.description or "",
            )

        response = self._finalize(
            db=db,
            project=project,
            paths=paths,
            planner_output=planner_output,
            source_path=request.source_path,
            analysis_performed=analysis_performed,
        )
        db.commit()
        return response

    # ── private: guardrail ────────────────────────────────────────────────

    def _check_no_active_tasks(self, db: Session, project: Project) -> None:
        active_count = (
            db.query(Task)
            .filter(
                Task.project_id == project.id,
                Task.status.notin_(TERMINAL_TASK_STATUSES),
            )
            .count()
        )
        if active_count > 0:
            raise ActiveTasksError(
                f"Project {project.id} has {active_count} non-terminal task(s). "
                "Finish or terminate them before starting a new planning cycle."
            )

    # ── private: shared finalization ──────────────────────────────────────

    def _finalize(
        self,
        db: Session,
        project: Project,
        paths: ProjectStoragePaths,
        planner_output: PlannerOutput,
        source_path: str | None,
        analysis_performed: bool,
    ) -> ProjectStartResponse:
        created_tasks = self._create_tasks(db, project, planner_output)
        db.flush()
        validate_task_quality(created_tasks)

        plan_content = json.dumps(planner_output.model_dump(), ensure_ascii=False, indent=2)
        artifact = create_artifact(
            db=db,
            project_id=project.id,
            task_id=None,
            artifact_type="project_plan",
            content=plan_content,
            created_by="project_start_service",
        )
        db.flush()

        self._plan_history.record_cycle(
            project_meta_dir=paths.project_meta_dir,
            objective=project.description or project.name,
            source_path=source_path,
        )
        cycle_number = len(self._plan_history.get_history(paths.project_meta_dir))

        project.last_planned_at = datetime.now(timezone.utc)

        logger.info(
            "project_start_complete project_id=%s cycle=%d tasks=%d analysis=%s",
            project.id,
            cycle_number,
            len(created_tasks),
            analysis_performed,
        )

        return ProjectStartResponse(
            project_id=project.id,
            plan_summary=planner_output.plan_summary,
            artifact_id=artifact.id,
            tasks_created=len(created_tasks),
            analysis_performed=analysis_performed,
            cycle_number=cycle_number,
        )

    def _create_tasks(
        self, db: Session, project: Project, planner_output: PlannerOutput
    ) -> list[Task]:
        tasks: list[Task] = []
        for index, planned_task in enumerate(planner_output.tasks, start=1):
            task = Task(
                project_id=project.id,
                parent_task_id=None,
                title=planned_task.title,
                description=planned_task.description,
                summary=planned_task.summary,
                objective=planned_task.objective,
                proposed_solution=None,
                implementation_notes=planned_task.implementation_notes,
                implementation_steps=None,
                acceptance_criteria=planned_task.acceptance_criteria,
                tests_required=None,
                technical_constraints=planned_task.technical_constraints,
                out_of_scope=planned_task.out_of_scope,
                priority=planned_task.priority,
                task_type=planned_task.task_type,
                planning_level=PLANNING_LEVEL_HIGH_LEVEL,
                executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
                sequence_order=index,
                status=TASK_STATUS_PENDING,
                is_blocked=False,
                blocking_reason=None,
            )
            db.add(task)
            tasks.append(task)
        return tasks
