from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import Task
from app.services.artifacts import create_artifact
from app.services.environment.contracts import PinnedDependency, RuntimeSpec, SpecChange
from app.services.environment.planner_client import call_environment_planner

logger = logging.getLogger(__name__)


def _task_to_dict(task: Task) -> dict:
    return {
        "title": task.title or "",
        "proposed_solution": task.proposed_solution or "",
        "technical_constraints": task.technical_constraints or "",
        "tests_required": task.tests_required or "",
    }


def plan_runtime_environment(
    db: Session,
    project_id: int,
    atomic_tasks: list[Task],
) -> RuntimeSpec:
    """Call the LLM to determine the runtime environment for all atomic tasks.

    Stores the resulting RuntimeSpec in Project.runtime_spec and returns it.
    """
    project = db.get(Project, project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")

    task_dicts = [_task_to_dict(t) for t in atomic_tasks]

    logger.info(
        "environment_planner_started project_id=%s atomic_task_count=%d",
        project_id,
        len(task_dicts),
    )

    output = call_environment_planner(
        project_name=project.name,
        project_description=project.description or "",
        atomic_tasks=task_dicts,
    )

    spec = RuntimeSpec(
        runtime_type=output.runtime_type,
        image=output.image,
        dependencies=[
            PinnedDependency(
                name=dep.name,
                version=dep.version,
                extras=dep.extras,
            )
            for dep in output.dependencies
        ],
        environment_variables=output.environment_variables,
        change_log=[
            SpecChange(
                change_type="initial",
                packages_affected=[dep.name for dep in output.dependencies],
                reason=output.planning_rationale,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
            )
        ],
    )

    project.runtime_spec = spec.model_dump()
    db.flush()

    create_artifact(
        db=db,
        project_id=project_id,
        artifact_type="runtime_environment_spec",
        content=json.dumps(spec.model_dump(), indent=2),
        created_by="environment_planner",
        auto_commit=False,
    )
    db.commit()

    logger.info(
        "environment_planner_completed project_id=%s runtime_type=%s image=%s packages=%d",
        project_id,
        spec.runtime_type,
        spec.image,
        len(spec.dependencies),
    )

    return spec
