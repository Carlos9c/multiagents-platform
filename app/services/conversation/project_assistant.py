"""Project Assistant Service — Aria.

State machine that manages the full conversation lifecycle:
  GATHERING_REQUIREMENTS → READY_TO_START → EXECUTING → AWAITING_REVIEW → EXECUTING → …

Phases:
- gathering_requirements: Aria asks questions to build the requirements draft
- ready_to_start: Aria has enough info; waits for user confirmation (chat or button)
- executing: Plan is running; only review interactions are allowed
- awaiting_review: A task is blocked and needs user guidance
- completed: All tasks finished
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_AWAITING_REVIEW,
    CONVERSATION_PHASE_COMPLETED,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_READY,
    CONVERSATION_STATUS_ACTIVE,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    Conversation,
    ConversationMessage,
)
from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.project_start import ProjectStartRequest
from app.services.conversation.requirements_evaluator import (
    ConversationTurn,
    RequirementsEvaluatorInput,
    evaluate_requirements,
    get_intro_message,
)
from app.services.conversation.resumption_service import resume_after_review
from app.services.conversation.review_evaluator import (
    BlockedTaskContext,
    ReviewEvaluatorInput,
    evaluate_review,
)
from app.services.project_start_service import ProjectStartService
from app.services.tasks import mark_task_failed

logger = logging.getLogger(__name__)

REVIEW_ATTEMPTS_LIMIT = 3
ASSISTANT_NAME = "Aria"


# ── Public contracts ──────────────────────────────────────────────────────────


@dataclass
class AssistantResponse:
    message: str
    phase: str
    conversation_id: int
    event: Literal[
        "question",
        "requirements_ready",
        "project_started",
        "review_resolved",
        "review_closed_limit_reached",
        "review_closed_abandoned",
        "executing",
        "project_completed",
    ]
    project_id: int | None = None
    requirements_draft: str | None = None


class ProjectAssistantError(Exception):
    pass


# ── Public API ────────────────────────────────────────────────────────────────


def get_or_create_conversation(db: Session, project_id: int) -> Conversation:
    """Return the active conversation for this project, or create one."""
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )

    if conversation is None:
        conversation = Conversation(
            project_id=project_id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_GATHERING,
            review_episode_attempts=0,
        )
        db.add(conversation)
        db.flush()

        _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, get_intro_message())
        db.commit()
        db.refresh(conversation)

    return conversation


def notify_review_started(
    db: Session,
    *,
    conversation_id: int,
    task_id: int,
) -> AssistantResponse:
    """Called by the execution layer when a task transitions to AWAITING_REVIEW."""
    conversation = _get_conversation_or_raise(db, conversation_id)
    task = db.get(Task, task_id)
    if task is None:
        raise ProjectAssistantError(f"Task {task_id} not found")

    conversation.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conversation.review_task_id = task_id
    conversation.review_episode_attempts = 0
    conversation.updated_at = datetime.now(timezone.utc)

    opening = _build_review_opening(task)
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, opening, task_id=task_id)
    db.commit()

    return AssistantResponse(
        message=opening,
        phase=CONVERSATION_PHASE_AWAITING_REVIEW,
        conversation_id=conversation_id,
        event="question",
    )


def process_user_message(
    db: Session,
    *,
    conversation_id: int,
    user_message: str,
) -> AssistantResponse:
    """Main entry point. Routes the user message to the correct handler based on phase."""
    conversation = _get_conversation_or_raise(db, conversation_id)

    if conversation.status != CONVERSATION_STATUS_ACTIVE:
        raise ProjectAssistantError(
            f"Conversation {conversation_id} is not active (status={conversation.status})"
        )

    _append_message(db, conversation.id, MESSAGE_ROLE_USER, user_message)

    if conversation.phase == CONVERSATION_PHASE_GATHERING:
        return _handle_gathering(db, conversation, user_message)

    if conversation.phase == CONVERSATION_PHASE_READY:
        return _handle_ready_to_start(db, conversation)

    if conversation.phase == CONVERSATION_PHASE_AWAITING_REVIEW:
        return _handle_review(db, conversation, user_message)

    # EXECUTING or COMPLETED — no input expected
    reply = "El proyecto está en ejecución. Te avisaré si necesito tu ayuda."
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()
    return AssistantResponse(
        message=reply,
        phase=conversation.phase,
        conversation_id=conversation_id,
        event="executing",
    )


def start_project_manually(
    db: Session,
    *,
    conversation_id: int,
    name: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
    manual_update: bool = True,
) -> AssistantResponse:
    """
    Called by the REST confirm-start endpoint when the user clicks the Start button.
    Updates project details and kicks off planning.
    """
    conversation = _get_conversation_or_raise(db, conversation_id)

    if conversation.phase not in (CONVERSATION_PHASE_GATHERING, CONVERSATION_PHASE_READY):
        raise ProjectAssistantError(
            f"No se puede iniciar el proyecto desde la fase '{conversation.phase}'. "
            "El proyecto debe estar en fase de recogida de requisitos o listo para empezar."
        )

    project = _get_project_or_raise(db, conversation.project_id)

    if name:
        project.name = name
    if description:
        project.description = description
    db.add(project)

    effective_description = (
        description
        or conversation.requirements_draft
        or project.description
        or ""
    )
    return _start_project(
        db, conversation, project, effective_description,
        source_path=source_path, manual_update=manual_update,
    )


def notify_project_completed(db: Session, project_id: int) -> AssistantResponse | None:
    """
    Called after each task status update. If all atomic tasks are terminal,
    sends Aria's completion message and transitions the conversation to 'completed'.
    """
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if conversation is None or conversation.phase != CONVERSATION_PHASE_EXECUTING:
        return None

    atomic_tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .all()
    )

    if not atomic_tasks:
        return None

    if not all(t.status in TERMINAL_TASK_STATUSES for t in atomic_tasks):
        return None

    completed_count = sum(1 for t in atomic_tasks if t.status == TASK_STATUS_COMPLETED)
    failed_count = sum(1 for t in atomic_tasks if t.status == TASK_STATUS_FAILED)

    conversation.phase = CONVERSATION_PHASE_COMPLETED
    conversation.updated_at = datetime.now(timezone.utc)

    if failed_count == 0:
        reply = (
            f"El proyecto ha finalizado correctamente. "
            f"Las {completed_count} tarea(s) han sido ejecutadas y validadas con éxito. "
            f"Puedes revisar los artefactos generados en el panel de tareas."
        )
    else:
        reply = (
            f"El proyecto ha finalizado. {completed_count} tarea(s) completadas, "
            f"{failed_count} tarea(s) fallidas. "
            f"Puedes revisar el historial de ejecución para ver los detalles."
        )

    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_COMPLETED,
        conversation_id=conversation.id,
        event="project_completed",
        project_id=project_id,
    )


# ── Gathering phase ───────────────────────────────────────────────────────────


def _handle_gathering(
    db: Session,
    conversation: Conversation,
    user_message: str,
) -> AssistantResponse:
    project = _get_project_or_raise(db, conversation.project_id)
    history = _load_history(db, conversation.id)

    result = evaluate_requirements(
        RequirementsEvaluatorInput(
            project_name=project.name or "Proyecto",
            history=history,
            current_draft=conversation.requirements_draft,
        )
    )

    if result.updated_draft:
        conversation.requirements_draft = result.updated_draft

    if result.status == "needs_more":
        reply = result.next_question or "¿Puedes contarme más sobre lo que necesitas?"
        _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
        conversation.updated_at = datetime.now(timezone.utc)
        db.commit()
        return AssistantResponse(
            message=reply,
            phase=CONVERSATION_PHASE_GATHERING,
            conversation_id=conversation.id,
            event="question",
            requirements_draft=result.updated_draft or None,
        )

    # Sufficient — transition to ready_to_start and ask for confirmation
    conversation.phase = CONVERSATION_PHASE_READY
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        "Creo que tengo suficiente información para comenzar. "
        "He actualizado la descripción del proyecto en el panel de la derecha. "
        "Revísala y ajústala si lo necesitas. "
        "Cuando estés listo, confírmame aquí o pulsa el botón «Iniciar» en la parte superior."
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_READY,
        conversation_id=conversation.id,
        event="requirements_ready",
        requirements_draft=result.updated_draft or None,
    )


def _handle_ready_to_start(
    db: Session,
    conversation: Conversation,
) -> AssistantResponse:
    """Any user message during ready_to_start confirms the start."""
    project = _get_project_or_raise(db, conversation.project_id)
    description = conversation.requirements_draft or project.description or ""
    return _start_project(db, conversation, project, description)


def _start_project(
    db: Session,
    conversation: Conversation,
    project: Project,
    description: str,
    source_path: str | None = None,
    manual_update: bool = True,
) -> AssistantResponse:
    """Kick off the planning service and transition the conversation to executing."""
    project.description = description
    db.add(project)

    start_service = ProjectStartService()

    if source_path:
        request = ProjectStartRequest(
            project_id=project.id,
            name=project.name,
            source_path=source_path,
            manual_update=manual_update,
        )
    else:
        # No external source — use existing analysis (None for first run → base planner)
        request = ProjectStartRequest(
            project_id=project.id,
            name=project.name,
            use_existing_source=True,
        )

    start_response = start_service.start(db, request)

    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        f"¡Todo listo! He creado un plan con {start_response.tasks_created} tarea(s). "
        f"El desarrollo comienza ahora. Te avisaré si surge algún problema que necesite tu atención."
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_EXECUTING,
        conversation_id=conversation.id,
        event="project_started",
        project_id=project.id,
    )


# ── Review phase ──────────────────────────────────────────────────────────────


def _handle_review(
    db: Session,
    conversation: Conversation,
    user_message: str,
) -> AssistantResponse:
    conversation.review_episode_attempts += 1
    conversation.updated_at = datetime.now(timezone.utc)

    task = _get_review_task_or_raise(db, conversation)
    project = _get_project_or_raise(db, conversation.project_id)
    episode_history = _load_episode_history(db, conversation)

    result = evaluate_review(
        ReviewEvaluatorInput(
            project_goal=project.description or project.name or "",
            blocked_task=BlockedTaskContext(
                task_id=task.id,
                title=task.title,
                description=task.description,
                validation_notes=None,
            ),
            episode_history=episode_history,
            attempts_used=conversation.review_episode_attempts,
        )
    )

    if result.status == "resolved":
        return _resolve_review(db, conversation, task, project, result.clarification_summary or user_message)

    if result.status == "abandoned":
        return _close_review_abandoned(db, conversation, task)

    if conversation.review_episode_attempts >= REVIEW_ATTEMPTS_LIMIT:
        return _close_review_limit(db, conversation, task)

    reply = result.next_question or "¿Puedes darme más detalles para poder continuar?"
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()
    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_AWAITING_REVIEW,
        conversation_id=conversation.id,
        event="question",
    )


def _resolve_review(
    db: Session,
    conversation: Conversation,
    task: Task,
    project: Project,
    clarification: str,
) -> AssistantResponse:
    resumption_result = resume_after_review(
        db,
        project_id=project.id,
        blocked_task_id=task.id,
        user_clarification=clarification,
        updated_project_goal=project.description,
    )

    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.review_task_id = None
    conversation.review_episode_attempts = 0
    conversation.updated_at = datetime.now(timezone.utc)

    reply = _build_resolution_message(resumption_result.scope, resumption_result.message)
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_EXECUTING,
        conversation_id=conversation.id,
        event="review_resolved",
    )


def _close_review_abandoned(
    db: Session,
    conversation: Conversation,
    task: Task,
) -> AssistantResponse:
    mark_task_failed(db, task.id, auto_commit=False)
    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.review_task_id = None
    conversation.review_episode_attempts = 0
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        "Entendido, cancelamos este punto. La tarea ha sido marcada como fallida. "
        "El resto del proyecto continuará si hay tareas independientes pendientes."
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_EXECUTING,
        conversation_id=conversation.id,
        event="review_closed_abandoned",
    )


def _close_review_limit(
    db: Session,
    conversation: Conversation,
    task: Task,
) -> AssistantResponse:
    mark_task_failed(db, task.id, auto_commit=False)
    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.review_task_id = None
    conversation.review_episode_attempts = 0
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        "Hemos llegado al máximo de intentos para resolver este punto sin llegar "
        "a una conclusión clara. La tarea ha sido marcada como fallida para que el "
        "proyecto pueda continuar. Puedes revisar el historial y retomar este punto manualmente."
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_EXECUTING,
        conversation_id=conversation.id,
        event="review_closed_limit_reached",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_conversation_or_raise(db: Session, conversation_id: int) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise ProjectAssistantError(f"Conversation {conversation_id} not found")
    return conversation


def _get_project_or_raise(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ProjectAssistantError(f"Project {project_id} not found")
    return project


def _get_review_task_or_raise(db: Session, conversation: Conversation) -> Task:
    if conversation.review_task_id is None:
        raise ProjectAssistantError(
            f"Conversation {conversation.id} has no review_task_id set"
        )
    task = db.get(Task, conversation.review_task_id)
    if task is None:
        raise ProjectAssistantError(f"Review task {conversation.review_task_id} not found")
    return task


def _append_message(
    db: Session,
    conversation_id: int,
    role: str,
    content: str,
    task_id: int | None = None,
) -> ConversationMessage:
    msg = ConversationMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        task_id=task_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.flush()
    return msg


def _load_history(db: Session, conversation_id: int) -> list[ConversationTurn]:
    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.id.asc())
        .all()
    )
    return [ConversationTurn(role=m.role, content=m.content) for m in messages]  # type: ignore[arg-type]


def _load_episode_history(
    db: Session,
    conversation: Conversation,
) -> list[ConversationTurn]:
    """Return only messages from the current review episode (after the episode opener)."""
    if conversation.review_task_id is None:
        return []

    messages = (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.task_id == conversation.review_task_id,
        )
        .order_by(ConversationMessage.id.asc())
        .all()
    )

    if messages:
        first_id = messages[0].id
        followup = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.conversation_id == conversation.id,
                ConversationMessage.id > first_id,
                ConversationMessage.task_id.is_(None),
            )
            .order_by(ConversationMessage.id.asc())
            .all()
        )
        all_msgs = [messages[0]] + followup
        all_msgs.sort(key=lambda m: m.id)
        return [ConversationTurn(role=m.role, content=m.content) for m in all_msgs]  # type: ignore[arg-type]

    return []


def _build_review_opening(task: Task) -> str:
    return (
        f"He detectado un problema con la tarea «{task.title}» que necesita tu ayuda para continuar. "
        f"El sistema no pudo determinar automáticamente cómo proceder. "
        f"¿Podrías indicarme cómo quieres resolver esta situación?"
    )


def _build_resolution_message(scope: str, backend_message: str) -> str:
    if scope == "narrow":
        return (
            "Perfecto, entendido. Voy a aplicar tu indicación directamente a la tarea "
            "y retomar la ejecución ahora mismo."
        )
    if scope == "moderate":
        return (
            "Muy bien. He actualizado las tareas afectadas con los nuevos criterios "
            "y reanudo el desarrollo. " + backend_message
        )
    return (
        "Entendido. Es un cambio importante, así que he replanificado el proyecto completo "
        "desde el estado actual del código. " + backend_message
    )
