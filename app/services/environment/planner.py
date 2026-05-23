from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import Task
from app.services.artifacts import create_artifact
from app.services.environment.catalog import (
    catalog_dockerfile_path,
    get_all_entries,
    get_entry_by_image,
    select_catalog_image,
)
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
    """Determine the runtime environment for the project.

    1. An LLM catalog selector picks the best curated base image (or None).
    2. The environment planner LLM decides which packages to install on top.
    3. If the catalog matched, its runtime_type and image override the planner's choice.
    """
    project = db.get(Project, project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")

    task_dicts = [_task_to_dict(t) for t in atomic_tasks]
    catalog_entries = get_all_entries()

    logger.info(
        "environment_planner_started project_id=%s atomic_task_count=%d",
        project_id,
        len(task_dicts),
    )

    # --- Step 1: catalog selection via LLM ---
    selection = select_catalog_image(
        project_name=project.name,
        project_description=project.description or "",
        task_dicts=task_dicts,
        entries=catalog_entries,
    )

    catalog_entry = None
    if selection.selected_image:
        catalog_entry = get_entry_by_image(selection.selected_image)
        if catalog_entry is None:
            # LLM returned an image name not in the catalog — treat as no match.
            logger.warning(
                "catalog_selector_unknown_image project_id=%s image=%s",
                project_id,
                selection.selected_image,
            )

    if catalog_entry:
        logger.info(
            "environment_planner_catalog_match project_id=%s image=%s runtime_type=%s",
            project_id,
            catalog_entry.image_name,
            catalog_entry.runtime_type,
        )

    # --- Step 2: dependency planning via LLM ---
    # Pass the catalog choice as a hint so the planner selects compatible packages.
    catalog_hint = (
        f"{catalog_entry.image_name} ({catalog_entry.description})"
        if catalog_entry
        else None
    )

    output = call_environment_planner(
        project_name=project.name,
        project_description=project.description or "",
        atomic_tasks=task_dicts,
        catalog_hint=catalog_hint,
    )

    # --- Step 3: build RuntimeSpec, catalog overrides image + runtime_type ---
    resolved_runtime_type = catalog_entry.runtime_type if catalog_entry else output.runtime_type
    resolved_image = catalog_entry.image_name if catalog_entry else output.image
    resolved_dockerfile_path = (
        str(catalog_dockerfile_path(catalog_entry)) if catalog_entry else None
    )

    spec = RuntimeSpec(
        runtime_type=resolved_runtime_type,
        image=resolved_image,
        dockerfile_path=resolved_dockerfile_path,
        dependencies=[
            PinnedDependency(
                name=dep.name,
                version=dep.version,
                extras=dep.extras,
            )
            for dep in output.dependencies
        ],
        environment_variables={ev.key: ev.value for ev in output.environment_variables},
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
        "environment_planner_completed project_id=%s runtime_type=%s image=%s packages=%d catalog_used=%s",
        project_id,
        spec.runtime_type,
        spec.image,
        len(spec.dependencies),
        catalog_entry is not None,
    )

    return spec
