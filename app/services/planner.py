import json

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.task import Task
from app.schemas.planner import PlannerOutput
from app.services.artifacts import create_artifact
from app.services.planner_client import call_planner_model


def validate_task_quality(tasks: list[Task]) -> None:
    vague_titles = {
        "crear backend",
        "hacer backend",
        "implementar sistema",
        "crear api",
        "hacer documentación",
        "documentación",
        "onboarding",
        "quickstart",
    }

    for task in tasks:
        title = task.title.strip().lower()

        if title in vague_titles:
            raise ValueError(f"Vague task title not allowed: {task.title}")

        if len((task.implementation_notes or "").strip()) < 60:
            raise ValueError(f"implementation_notes too short in task: {task.title}")

        if len((task.acceptance_criteria or "").strip()) < 30:
            raise ValueError(f"acceptance_criteria too short in task: {task.title}")

        if len((task.out_of_scope or "").strip()) < 20:
            raise ValueError(f"out_of_scope too short in task: {task.title}")

        if len((task.technical_constraints or "").strip()) < 20:
            raise ValueError(f"technical_constraints too short in task: {task.title}")


def validate_required_task_mix(tasks: list[Task]) -> None:
    task_types = {task.task_type for task in tasks}

    if "documentation" not in task_types:
        raise ValueError("Planner output must include at least one documentation task.")

    if "onboarding" not in task_types:
        raise ValueError("Planner output must include at least one onboarding task.")

    if "implementation" not in task_types:
        raise ValueError("Planner output must include at least one implementation task.")


def generate_project_plan(db: Session, project_id: int) -> dict:
    project = db.get(Project, project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")

    planner_output: PlannerOutput = call_planner_model(
        project_name=project.name,
        project_description=project.description or "",
    )

    created_tasks: list[Task] = []

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
            planning_level="high_level",
            executor_type="code_executor",
            sequence_order=index,
            status="pending",
            is_blocked=False,
            blocking_reason=None,
        )
        db.add(task)
        created_tasks.append(task)

    db.flush()

    validate_required_task_mix(created_tasks)
    validate_task_quality(created_tasks)

    db.commit()

    for task in created_tasks:
        db.refresh(task)

    plan_content = json.dumps(
        planner_output.model_dump(),
        ensure_ascii=False,
        indent=2,
    )

    artifact = create_artifact(
        db=db,
        project_id=project.id,
        task_id=None,
        artifact_type="project_plan",
        content=plan_content,
        created_by="planner_agent",
    )

    return {
        "project_id": project.id,
        "plan_summary": planner_output.plan_summary,
        "artifact_id": artifact.id,
        "tasks_created": len(created_tasks),
    }