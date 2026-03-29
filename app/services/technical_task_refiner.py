import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.project import Project
from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
    TASK_STATUS_PENDING,
    Task,
)
from app.schemas.technical_task_refiner import TechnicalTaskRefinementOutput
from app.services.technical_task_refiner_client import (
    call_technical_task_refiner_model,
)


def _format_bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def _validate_parent_task(project: Project, task: Task) -> None:
    if task.project_id != project.id:
        raise ValueError("Task does not belong to the given project")

    if task.planning_level != PLANNING_LEVEL_HIGH_LEVEL:
        raise ValueError("Only high_level tasks can be refined")


def _validate_refined_task_quality(tasks: list[Task]) -> None:
    for task in tasks:
        if len((task.proposed_solution or "").strip()) < 40:
            raise ValueError(
                f"proposed_solution too short in refined task: {task.title}"
            )

        if len((task.implementation_steps or "").strip()) < 20:
            raise ValueError(
                f"implementation_steps too short in refined task: {task.title}"
            )

        if len((task.tests_required or "").strip()) < 10:
            raise ValueError(f"tests_required too short in refined task: {task.title}")

        if len((task.acceptance_criteria or "").strip()) < 20:
            raise ValueError(
                f"acceptance_criteria too short in refined task: {task.title}"
            )


def refine_high_level_task(
    db: Session,
    *,
    project_id: int,
    task_id: int,
) -> dict:
    project = db.get(Project, project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")

    parent_task = db.get(Task, task_id)
    if not parent_task:
        raise ValueError(f"Task {task_id} not found")

    _validate_parent_task(project, parent_task)

    existing_children = (
        db.query(Task)
        .filter(
            Task.parent_task_id == parent_task.id,
            Task.planning_level == PLANNING_LEVEL_REFINED,
        )
        .order_by(Task.sequence_order.asc(), Task.id.asc())
        .all()
    )

    if existing_children:
        existing_artifact = (
            db.query(Artifact)
            .filter(
                Artifact.project_id == project.id,
                Artifact.task_id == parent_task.id,
                Artifact.artifact_type == "technical_refinement",
            )
            .order_by(Artifact.id.desc())
            .first()
        )

        return {
            "project_id": project.id,
            "parent_task_id": parent_task.id,
            "artifact_id": existing_artifact.id if existing_artifact else None,
            "tasks_created": 0,
            "tasks_reused": len(existing_children),
            "refined_task_ids": [task.id for task in existing_children],
        }

    refinement_output: TechnicalTaskRefinementOutput = (
        call_technical_task_refiner_model(
            project_name=project.name,
            project_description=project.description or "",
            parent_task_title=parent_task.title,
            parent_task_description=parent_task.description or "",
            parent_task_summary=parent_task.summary or "",
            parent_task_objective=parent_task.objective or "",
            parent_task_type=parent_task.task_type,
            parent_task_implementation_notes=parent_task.implementation_notes or "",
            parent_task_acceptance_criteria=parent_task.acceptance_criteria or "",
            parent_task_technical_constraints=parent_task.technical_constraints or "",
            parent_task_out_of_scope=parent_task.out_of_scope or "",
        )
    )

    created_tasks: list[Task] = []

    for index, refined in enumerate(refinement_output.refined_tasks, start=1):
        task = Task(
            project_id=project.id,
            parent_task_id=parent_task.id,
            title=refined.title,
            description=refined.description,
            summary=refined.summary,
            objective=refined.objective,
            proposed_solution=refined.proposed_solution,
            implementation_notes=None,
            implementation_steps=_format_bullet_list(refined.implementation_steps),
            acceptance_criteria=refined.acceptance_criteria,
            tests_required=_format_bullet_list(refined.tests_required),
            technical_constraints=refined.technical_constraints,
            out_of_scope=refined.out_of_scope,
            priority=refined.priority,
            task_type=refined.task_type,
            planning_level=PLANNING_LEVEL_REFINED,
            executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
            sequence_order=index,
            status=TASK_STATUS_PENDING,
            is_blocked=False,
            blocking_reason=None,
        )
        db.add(task)
        created_tasks.append(task)

    db.flush()
    _validate_refined_task_quality(created_tasks)

    artifact = Artifact(
        project_id=project.id,
        task_id=parent_task.id,
        artifact_type="technical_refinement",
        content=json.dumps(
            refinement_output.model_dump(), ensure_ascii=False, indent=2
        ),
        created_by="technical_task_refiner_agent",
    )
    db.add(artifact)

    db.commit()

    for task in created_tasks:
        db.refresh(task)
    db.refresh(artifact)

    return {
        "project_id": project.id,
        "parent_task_id": parent_task.id,
        "artifact_id": artifact.id,
        "tasks_created": len(created_tasks),
        "tasks_reused": 0,
        "refined_task_ids": [task.id for task in created_tasks],
    }
