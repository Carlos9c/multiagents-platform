import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.project import Project
from app.models.task import (
    EXECUTION_ENGINE,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
    PENDING_ENGINE_ROUTING_EXECUTOR,
    Task,
)
from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.atomic_task_generator_client import call_atomic_task_generator_model


AVAILABLE_EXECUTORS = [EXECUTION_ENGINE]

ALLOWED_PARENT_PLANNING_LEVELS = {
    PLANNING_LEVEL_HIGH_LEVEL,
    PLANNING_LEVEL_REFINED,
}

MAX_ATOMIC_TASKS_PER_PARENT = 8
MAX_IMPLEMENTATION_STEPS_PER_ATOMIC = 20


class AtomicTaskGenerationError(ValueError):
    """Raised when generated atomic tasks do not meet minimum structural requirements."""


def _format_bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def _validate_parent_task(project: Project, task: Task) -> None:
    if task.project_id != project.id:
        raise AtomicTaskGenerationError("Task does not belong to the given project")

    if task.planning_level not in ALLOWED_PARENT_PLANNING_LEVELS:
        raise AtomicTaskGenerationError(
            "Only high_level or refined tasks can be converted to atomic tasks. "
            f"Received planning_level='{task.planning_level}'."
        )


def _implementation_steps_count(implementation_steps: str) -> int:
    lines = [line.strip() for line in implementation_steps.splitlines() if line.strip()]
    return sum(1 for line in lines if line.startswith("- "))


def _validate_atomic_task_quality(tasks: list[Task]) -> None:
    if not tasks:
        raise AtomicTaskGenerationError("Atomic generation produced no tasks.")

    seen_titles: set[str] = set()

    for task in tasks:
        normalized_title = (task.title or "").strip().lower()
        if normalized_title in seen_titles:
            raise AtomicTaskGenerationError(f"Duplicate atomic task title detected: {task.title}")
        seen_titles.add(normalized_title)

        if len((task.title or "").strip()) < 8:
            raise AtomicTaskGenerationError("Atomic task title is too short to be actionable.")

        if len((task.description or "").strip()) < 20:
            raise AtomicTaskGenerationError(f"description too short in atomic task: {task.title}")

        if len((task.summary or "").strip()) < 10:
            raise AtomicTaskGenerationError(f"summary too short in atomic task: {task.title}")

        if len((task.objective or "").strip()) < 10:
            raise AtomicTaskGenerationError(f"objective too short in atomic task: {task.title}")

        if len((task.proposed_solution or "").strip()) < 20:
            raise AtomicTaskGenerationError(
                f"proposed_solution too short in atomic task: {task.title}"
            )

        if len((task.implementation_steps or "").strip()) < 10:
            raise AtomicTaskGenerationError(
                f"implementation_steps too short in atomic task: {task.title}"
            )

        if len((task.acceptance_criteria or "").strip()) < 20:
            raise AtomicTaskGenerationError(
                f"acceptance_criteria too short in atomic task: {task.title}"
            )

        step_count = _implementation_steps_count(task.implementation_steps or "")
        if step_count > MAX_IMPLEMENTATION_STEPS_PER_ATOMIC:
            raise AtomicTaskGenerationError(
                f"Atomic task appears too large for one execution unit: {task.title}"
            )


def _get_existing_atomic_children(db: Session, parent_task_id: int) -> list[Task]:
    return (
        db.query(Task)
        .filter(
            Task.parent_task_id == parent_task_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .order_by(Task.sequence_order.asc(), Task.id.asc())
        .all()
    )


def _get_latest_atomic_generation_artifact(
    db: Session,
    *,
    project_id: int,
    parent_task_id: int,
) -> Artifact | None:
    return (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.task_id == parent_task_id,
            Artifact.artifact_type == "atomic_task_generation",
        )
        .order_by(Artifact.id.desc())
        .first()
    )


def _build_reuse_response(
    *,
    project: Project,
    parent_task: Task,
    existing_children: list[Task],
    existing_artifact: Artifact | None,
) -> dict:
    return {
        "project_id": project.id,
        "parent_task_id": parent_task.id,
        "parent_planning_level": parent_task.planning_level,
        "artifact_id": existing_artifact.id if existing_artifact else None,
        "tasks_created": 0,
        "tasks_reused": len(existing_children),
        "atomic_task_ids": [task.id for task in existing_children],
        "available_executors": AVAILABLE_EXECUTORS,
    }


def _build_created_response(
    *,
    project: Project,
    parent_task: Task,
    created_tasks: list[Task],
    artifact: Artifact,
) -> dict:
    return {
        "project_id": project.id,
        "parent_task_id": parent_task.id,
        "parent_planning_level": parent_task.planning_level,
        "artifact_id": artifact.id,
        "tasks_created": len(created_tasks),
        "tasks_reused": 0,
        "atomic_task_ids": [task.id for task in created_tasks],
        "available_executors": AVAILABLE_EXECUTORS,
    }


def generate_atomic_tasks(
    db: Session,
    *,
    project_id: int,
    task_id: int,
) -> dict:
    project = db.get(Project, project_id)
    if not project:
        raise AtomicTaskGenerationError(f"Project {project_id} not found")

    parent_task = db.get(Task, task_id)
    if not parent_task:
        raise AtomicTaskGenerationError(f"Task {task_id} not found")

    _validate_parent_task(project, parent_task)

    existing_children = _get_existing_atomic_children(db, parent_task.id)

    if existing_children:
        existing_artifact = _get_latest_atomic_generation_artifact(
            db=db,
            project_id=project.id,
            parent_task_id=parent_task.id,
        )
        return _build_reuse_response(
            project=project,
            parent_task=parent_task,
            existing_children=existing_children,
            existing_artifact=existing_artifact,
        )

    atomic_output: AtomicTaskGenerationOutput = call_atomic_task_generator_model(
        project_name=project.name,
        project_description=project.description or "",
        parent_task_title=parent_task.title,
        parent_task_description=parent_task.description or "",
        parent_task_summary=parent_task.summary or "",
        parent_task_objective=parent_task.objective or "",
        parent_task_type=parent_task.task_type,
        parent_task_planning_level=parent_task.planning_level,
        parent_task_proposed_solution=parent_task.proposed_solution or "",
        parent_task_implementation_steps=parent_task.implementation_steps or "",
        parent_task_acceptance_criteria=parent_task.acceptance_criteria or "",
        parent_task_tests_required=parent_task.tests_required or "",
        parent_task_technical_constraints=parent_task.technical_constraints or "",
        parent_task_out_of_scope=parent_task.out_of_scope or "",
        available_executors=AVAILABLE_EXECUTORS,
    )

    created_tasks: list[Task] = []

    for index, atomic in enumerate(atomic_output.atomic_tasks, start=1):
        task = Task(
            project_id=project.id,
            parent_task_id=parent_task.id,
            title=atomic.title,
            description=atomic.description,
            summary=atomic.summary,
            objective=atomic.objective,
            proposed_solution=atomic.proposed_solution,
            implementation_notes=None,
            implementation_steps=_format_bullet_list(atomic.implementation_steps),
            acceptance_criteria=atomic.acceptance_criteria,
            tests_required=_format_bullet_list(atomic.tests_required),
            technical_constraints=atomic.technical_constraints,
            out_of_scope=atomic.out_of_scope,
            priority=atomic.priority,
            task_type=atomic.task_type,
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=PENDING_ENGINE_ROUTING_EXECUTOR,
            sequence_order=index,
            status="pending",
            is_blocked=False,
            blocking_reason=None,
        )
        db.add(task)
        created_tasks.append(task)

    db.flush()

    _validate_atomic_task_quality(created_tasks)

    artifact_payload = {
        "parent_task_id": parent_task.id,
        "project_id": project.id,
        "available_executors": AVAILABLE_EXECUTORS,
        "atomic_tasks": [
            {
                "title": task.title,
                "description": task.description,
                "summary": task.summary,
                "objective": task.objective,
                "proposed_solution": task.proposed_solution,
                "implementation_steps": task.implementation_steps,
                "acceptance_criteria": task.acceptance_criteria,
                "tests_required": task.tests_required,
                "technical_constraints": task.technical_constraints,
                "out_of_scope": task.out_of_scope,
                "priority": task.priority,
                "task_type": task.task_type,
                "planning_level": task.planning_level,
                "executor_type": task.executor_type,
                "sequence_order": task.sequence_order,
            }
            for task in created_tasks
        ],
    }

    artifact = Artifact(
        project_id=project.id,
        task_id=parent_task.id,
        artifact_type="atomic_task_generation",
        content=json.dumps(artifact_payload, ensure_ascii=False, indent=2),
        created_by="atomic_task_generator",
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    for task in created_tasks:
        db.refresh(task)

    return _build_created_response(
        project=project,
        parent_task=parent_task,
        created_tasks=created_tasks,
        artifact=artifact,
    )
