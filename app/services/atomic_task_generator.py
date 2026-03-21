import json
import re

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.project import Project
from app.models.task import Task
from app.schemas.atomic_task_generator import AtomicTaskGenerationOutput
from app.services.atomic_task_generator_client import call_atomic_task_generator_model


PENDING_ATOMIC_ASSIGNMENT_EXECUTOR = "pending_atomic_assignment"
AVAILABLE_EXECUTORS = ["code_executor"]
MAX_ATOMIC_TASKS_PER_REFINED = 8

COMPOUND_TITLE_PATTERNS = [
    r"\bdefinir y\b",
    r"\bidentificar y\b",
    r"\bdocumentar y\b",
    r"\bredactar y\b",
    r"\banalizar y\b",
    r"\bcrear y\b",
    r"\bextraer y\b",
    r"\bintegrar y\b",
    r"\bvalidar y\b",
    r"\borganizar y\b",
]

BROAD_DESCRIPTION_PATTERNS = [
    r"\bjunto con\b",
    r"\basí como\b",
    r"\by también\b",
    r"\btanto .* como\b",
]

DOCUMENT_LIKE_TASK_TYPES = {"requirements", "documentation", "onboarding"}
STRICTER_TASK_TYPES = {"implementation", "testing", "review"}


def _format_bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def _validate_parent_task(project: Project, task: Task) -> None:
    if task.project_id != project.id:
        raise ValueError("Task does not belong to the given project")

    if task.planning_level != "refined":
        raise ValueError("Only refined tasks can be converted to atomic tasks")

    if task.executor_type != PENDING_ATOMIC_ASSIGNMENT_EXECUTOR:
        raise ValueError("Refined task must still be pending atomic executor assignment")


def _looks_compound_title(title: str) -> bool:
    normalized = title.strip().lower()
    return any(re.search(pattern, normalized) for pattern in COMPOUND_TITLE_PATTERNS)


def _looks_broad_description(description: str) -> bool:
    normalized = (description or "").strip().lower()
    return any(re.search(pattern, normalized) for pattern in BROAD_DESCRIPTION_PATTERNS)


def _implementation_steps_count(implementation_steps: str) -> int:
    lines = [line.strip() for line in implementation_steps.splitlines() if line.strip()]
    return sum(1 for line in lines if line.startswith("- "))


def _normalized_title(title: str | None) -> str:
    return (title or "").strip().lower()


def _count_atomicity_risk_signals(task: Task) -> int:
    risk_signals = 0
    title = _normalized_title(task.title)
    description = (task.description or "").strip().lower()

    if _looks_compound_title(title):
        risk_signals += 1

    if _looks_broad_description(description):
        risk_signals += 1

    if "integrar" in title and "redact" in title:
        risk_signals += 1

    if "documentar" in title and "definir" in title:
        risk_signals += 1

    if "analizar" in title and "redact" in title:
        risk_signals += 1

    if "extraer" in title and "integrar" in title:
        risk_signals += 1

    return risk_signals


def _validate_atomic_step_count(task: Task) -> None:
    step_count = _implementation_steps_count(task.implementation_steps or "")
    task_type = (task.task_type or "").strip().lower()
    risk_signals = _count_atomicity_risk_signals(task)

    # Para tareas más cercanas a ejecución o validación, mantenemos guardrails más estrictos.
    if task_type in STRICTER_TASK_TYPES:
        if step_count > 6:
            raise ValueError(
                f"Atomic task has too many implementation steps and may be too broad: {task.title}"
            )
        return

    # Para tareas documentales/requirements no rechazamos solo por número de pasos.
    # Rechazamos cuando hay exceso de pasos junto con señales semánticas de mezcla.
    if task_type in DOCUMENT_LIKE_TASK_TYPES:
        if step_count > 10 and risk_signals >= 1:
            raise ValueError(
                f"Atomic task is too broad for a document-like task: {task.title}"
            )
        if step_count > 8 and risk_signals >= 2:
            raise ValueError(
                f"Atomic task mixes too many concerns for a document-like task: {task.title}"
            )
        return

    # Regla general para otros tipos: combinar tamaño y señales semánticas.
    if step_count > 8 and risk_signals >= 1:
        raise ValueError(
            f"Atomic task appears too broad based on combined signals: {task.title}"
        )
    if step_count > 6 and risk_signals >= 2:
        raise ValueError(
            f"Atomic task appears too broad based on multiple atomicity risks: {task.title}"
        )


def _validate_atomic_task_quality(tasks: list[Task], available_executors: list[str]) -> None:
    if len(tasks) > MAX_ATOMIC_TASKS_PER_REFINED:
        raise ValueError(
            f"Atomic generation produced too many tasks ({len(tasks)}). "
            f"Maximum allowed in this phase is {MAX_ATOMIC_TASKS_PER_REFINED}."
        )

    for task in tasks:
        if task.executor_type not in available_executors:
            raise ValueError(f"Invalid executor_type in atomic task: {task.executor_type}")

        if len((task.proposed_solution or "").strip()) < 30:
            raise ValueError(f"proposed_solution too short in atomic task: {task.title}")

        if len((task.implementation_steps or "").strip()) < 10:
            raise ValueError(f"implementation_steps too short in atomic task: {task.title}")

        if len((task.tests_required or "").strip()) < 10:
            raise ValueError(f"tests_required too short in atomic task: {task.title}")

        if len((task.acceptance_criteria or "").strip()) < 20:
            raise ValueError(f"acceptance_criteria too short in atomic task: {task.title}")

        # Señales semánticas fuertes: estas siguen siendo motivo de rechazo directo.
        normalized_title = _normalized_title(task.title)

        if _looks_compound_title(task.title):
            raise ValueError(
                f"Atomic task title suggests multiple responsibilities: {task.title}"
            )

        if _looks_broad_description(task.description or ""):
            raise ValueError(
                f"Atomic task description suggests multiple combined actions: {task.title}"
            )

        if "integrar" in normalized_title and "redact" in normalized_title:
            raise ValueError(
                f"Atomic task title mixes content creation and integration: {task.title}"
            )

        if "documentar" in normalized_title and "definir" in normalized_title:
            raise ValueError(
                f"Atomic task title mixes definition and documentation: {task.title}"
            )

        if "analizar" in normalized_title and "redact" in normalized_title:
            raise ValueError(
                f"Atomic task title mixes analysis and writing: {task.title}"
            )

        # El número de pasos ya no se usa como regla universal,
        # sino como validación contextual por tipo de tarea.
        _validate_atomic_step_count(task)


def generate_atomic_tasks(
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
            Task.planning_level == "atomic",
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
                Artifact.artifact_type == "atomic_task_generation",
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
            "atomic_task_ids": [task.id for task in existing_children],
            "available_executors": AVAILABLE_EXECUTORS,
        }

    atomic_output: AtomicTaskGenerationOutput = call_atomic_task_generator_model(
        project_name=project.name,
        project_description=project.description or "",
        refined_task_title=parent_task.title,
        refined_task_description=parent_task.description or "",
        refined_task_summary=parent_task.summary or "",
        refined_task_objective=parent_task.objective or "",
        refined_task_type=parent_task.task_type,
        refined_task_proposed_solution=parent_task.proposed_solution or "",
        refined_task_implementation_steps=parent_task.implementation_steps or "",
        refined_task_acceptance_criteria=parent_task.acceptance_criteria or "",
        refined_task_tests_required=parent_task.tests_required or "",
        refined_task_technical_constraints=parent_task.technical_constraints or "",
        refined_task_out_of_scope=parent_task.out_of_scope or "",
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
            planning_level="atomic",
            executor_type=atomic.executor_type,
            sequence_order=index,
            status="pending",
            is_blocked=False,
            blocking_reason=None,
        )
        db.add(task)
        created_tasks.append(task)

    db.flush()
    _validate_atomic_task_quality(created_tasks, AVAILABLE_EXECUTORS)

    artifact = Artifact(
        project_id=project.id,
        task_id=parent_task.id,
        artifact_type="atomic_task_generation",
        content=json.dumps(atomic_output.model_dump(), ensure_ascii=False, indent=2),
        created_by="atomic_task_generator_agent",
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
        "atomic_task_ids": [task.id for task in created_tasks],
        "available_executors": AVAILABLE_EXECUTORS,
    }