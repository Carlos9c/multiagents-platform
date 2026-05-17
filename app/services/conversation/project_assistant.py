"""Project Assistant Service — Aria.

State machine that manages the full conversation lifecycle:
  GATHERING_REQUIREMENTS → READY_TO_START → EXECUTING ⇄ AWAITING_REVIEW → EXECUTING → …
                                                      ↕ PAUSED

Phases:
- gathering_requirements: Aria asks questions to build the requirements draft
- ready_to_start: Aria has enough info; waits for user confirmation (chat or button)
- executing: Plan is running; Aria answers project questions but cannot take action
- awaiting_review: A task or project-level error is blocked; Aria gathers clarification,
  proposes a plan, waits for user confirmation, then resumes
- paused: Execution paused by user; Aria answers questions and awaits resume
- completed: All tasks finished

Review episodes have two kinds:
- Task-level: review_task_id is set — a specific task failed and needs clarification.
  Resolved via resume_after_review() which modifies/retries the task.
- Project-level: review_task_id is None — an infrastructure/configuration error stopped
  the whole workflow (env bootstrap, empty plan, iteration limit, unexpected exception).
  review_failure_context holds the original failure reason.
  Resolved by embedding user clarification into project description and re-queuing
  the workflow.
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
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_PHASE_READY,
    CONVERSATION_STATUS_ACTIVE,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    REVIEW_SUBPHASE_AWAITING_CONFIRMATION,
    REVIEW_SUBPHASE_GATHERING,
    Conversation,
    ConversationMessage,
)
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.schemas.project_start import ProjectStartRequest
from app.services.conversation.confirmation_evaluator import (
    ConfirmationEvaluatorInput,
    evaluate_confirmation,
)
from app.services.conversation.project_query_agent import (
    ProjectQueryError,
    answer_project_query,
)
from app.services.conversation.requirements_evaluator import (
    ConversationTurn,
    RequirementsEvaluatorInput,
    evaluate_requirements,
    get_intro_message,
)
from app.services.conversation.resumption_service import ResumptionError, resume_after_review
from app.services.conversation.review_evaluator import (
    BlockedTaskContext,
    ReviewEvaluatorInput,
    evaluate_review,
)
from app.services.project_start_service import ProjectStartService
from app.services.tasks import mark_task_failed

logger = logging.getLogger(__name__)

ASSISTANT_NAME = "Aria"

_PROJECT_CLARIFICATION_HEADER = "\n\n--- INDICACIÓN POST-REVISIÓN ---\n"


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
        "review_confirmation_requested",
        "review_closed_abandoned",
        "executing",
        "project_completed",
        "project_query_answered",
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
    """Called by the execution layer when a specific task transitions to AWAITING_REVIEW."""
    conversation = _get_conversation_or_raise(db, conversation_id)
    task = db.get(Task, task_id)
    if task is None:
        raise ProjectAssistantError(f"Task {task_id} not found")

    conversation.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conversation.review_task_id = task_id
    conversation.review_failure_context = None
    conversation.review_episode_attempts = 0
    conversation.review_subphase = REVIEW_SUBPHASE_GATHERING
    conversation.pending_clarification_summary = None
    conversation.updated_at = datetime.now(timezone.utc)

    validation_notes = _get_task_validation_notes(db, task_id)
    opening = _build_review_opening(task, validation_notes)
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, opening, task_id=task_id)
    db.commit()

    return AssistantResponse(
        message=opening,
        phase=CONVERSATION_PHASE_AWAITING_REVIEW,
        conversation_id=conversation_id,
        event="question",
    )


def notify_workflow_error(
    db: Session,
    *,
    conversation_id: int,
    failure_reason: str,
) -> AssistantResponse:
    """
    Called when the workflow stops due to a project-level failure with no specific blocked
    task — e.g. environment bootstrap failure, empty execution plan, iteration limit, or
    an unexpected exception. Transitions the conversation to AWAITING_REVIEW so the user
    can provide direction and the workflow can be re-queued under new assumptions.
    """
    conversation = _get_conversation_or_raise(db, conversation_id)

    conversation.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conversation.review_task_id = None
    conversation.review_failure_context = failure_reason
    conversation.review_episode_attempts = 0
    conversation.review_subphase = REVIEW_SUBPHASE_GATHERING
    conversation.pending_clarification_summary = None
    conversation.updated_at = datetime.now(timezone.utc)

    opening = _build_workflow_error_opening(failure_reason)
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, opening)
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
    # Re-fetch from DB: the WS session is long-lived so the identity map may hold a
    # stale phase that was changed by a background workflow thread in a separate session.
    db.refresh(conversation)

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

    if conversation.phase == CONVERSATION_PHASE_EXECUTING:
        return _handle_project_query(db, conversation, user_message)

    # PAUSED or COMPLETED — answer project questions
    return _handle_project_query(db, conversation, user_message)


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
    """Route to the correct review sub-handler based on the current sub-phase."""
    subphase = conversation.review_subphase or REVIEW_SUBPHASE_GATHERING

    if subphase == REVIEW_SUBPHASE_AWAITING_CONFIRMATION:
        return _handle_review_confirmation(db, conversation, user_message)

    return _handle_review_gathering(db, conversation, user_message)


def _handle_review_gathering(
    db: Session,
    conversation: Conversation,
    user_message: str,
) -> AssistantResponse:
    """Evaluate the user's clarification. Continue asking until Aria has enough context."""
    conversation.review_episode_attempts += 1
    conversation.updated_at = datetime.now(timezone.utc)

    project = _get_project_or_raise(db, conversation.project_id)
    episode_history = _load_episode_history(db, conversation)
    project_task_summary = _build_task_summary_for_review(
        db, conversation.project_id, blocked_task_id=conversation.review_task_id
    )

    is_project_level = conversation.review_task_id is None

    if is_project_level:
        blocked_ctx = _build_project_level_block_context(conversation)
    else:
        task = _get_review_task_or_raise(db, conversation)
        validation_notes = _get_task_validation_notes(db, task.id)
        blocked_ctx = BlockedTaskContext(
            task_id=task.id,
            title=task.title,
            description=task.description,
            validation_notes=validation_notes,
        )

    result = evaluate_review(
        ReviewEvaluatorInput(
            project_goal=project.description or project.name or "",
            blocked_task=blocked_ctx,
            episode_history=episode_history,
            project_task_summary=project_task_summary,
            is_project_level_error=is_project_level,
        )
    )

    if result.status == "abandoned":
        if is_project_level:
            return _close_project_level_review_abandoned(db, conversation, project)
        task = _get_review_task_or_raise(db, conversation)
        return _close_review_abandoned(db, conversation, task)

    if result.status == "ready_to_confirm":
        if is_project_level:
            return _request_review_confirmation(
                db, conversation, task=None, project=project,
                clarification_summary=result.clarification_summary or user_message,
                action_summary=result.action_summary or "Actualizar la configuración del proyecto y reanudar.",
            )
        task = _get_review_task_or_raise(db, conversation)
        return _request_review_confirmation(
            db, conversation, task=task, project=project,
            clarification_summary=result.clarification_summary or user_message,
            action_summary=result.action_summary or "Proceder con los cambios necesarios.",
        )

    # insufficient — ask next_question (no attempt limit)
    reply = result.next_question or "¿Puedes darme más detalles para poder continuar?"
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()
    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_AWAITING_REVIEW,
        conversation_id=conversation.id,
        event="question",
    )


def _request_review_confirmation(
    db: Session,
    conversation: Conversation,
    task: Task | None,
    project: Project,
    *,
    clarification_summary: str,
    action_summary: str,
) -> AssistantResponse:
    """Store the proposed plan and ask the user to confirm before proceeding."""
    conversation.review_subphase = REVIEW_SUBPHASE_AWAITING_CONFIRMATION
    conversation.pending_clarification_summary = clarification_summary
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        f"Creo que ya tengo suficiente contexto para resolver esto. Aquí está lo que haré:\n\n"
        f"{action_summary}\n\n"
        f"¿Confirmamos y procedemos?"
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_AWAITING_REVIEW,
        conversation_id=conversation.id,
        event="review_confirmation_requested",
    )


def _handle_review_confirmation(
    db: Session,
    conversation: Conversation,
    user_message: str,
) -> AssistantResponse:
    """Check if the user confirmed the proposed plan, or wants to keep discussing."""
    action_summary = conversation.pending_clarification_summary or "(plan details not available)"

    result = evaluate_confirmation(
        ConfirmationEvaluatorInput(
            action_summary=action_summary,
            user_response=user_message,
        )
    )

    if not result.confirmed:
        # User wants to modify/clarify — go back to gathering
        conversation.review_subphase = REVIEW_SUBPHASE_GATHERING
        conversation.pending_clarification_summary = None
        conversation.updated_at = datetime.now(timezone.utc)

        reply = result.follow_up or "Entendido, cuéntame más para que pueda ajustar el plan."
        _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
        db.commit()
        return AssistantResponse(
            message=reply,
            phase=CONVERSATION_PHASE_AWAITING_REVIEW,
            conversation_id=conversation.id,
            event="question",
        )

    clarification = conversation.pending_clarification_summary or user_message
    project = _get_project_or_raise(db, conversation.project_id)

    if conversation.review_task_id is None:
        return _resolve_project_level_review(db, conversation, project, clarification)

    task = _get_review_task_or_raise(db, conversation)
    return _resolve_review(db, conversation, task, project, clarification)


def _resolve_review(
    db: Session,
    conversation: Conversation,
    task: Task,
    project: Project,
    clarification: str,
) -> AssistantResponse:
    try:
        resumption_result = resume_after_review(
            db,
            project_id=project.id,
            blocked_task_id=task.id,
            user_clarification=clarification,
            updated_project_goal=project.description,
        )
    except ResumptionError as exc:
        raise ProjectAssistantError(str(exc)) from exc

    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.review_task_id = None
    conversation.review_failure_context = None
    conversation.review_episode_attempts = 0
    conversation.review_subphase = None
    conversation.pending_clarification_summary = None
    conversation.updated_at = datetime.now(timezone.utc)

    reply = _build_resolution_message(resumption_result.scope, resumption_result.message)
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_EXECUTING,
        conversation_id=conversation.id,
        event="review_resolved",
        project_id=project.id,
    )


def _resolve_project_level_review(
    db: Session,
    conversation: Conversation,
    project: Project,
    clarification: str,
) -> AssistantResponse:
    """
    Resume after a project-level (no task) review episode.
    Embeds user clarification into the project description so all downstream LLM agents
    see the updated requirements/constraints on the next workflow run.
    Returns event="review_resolved" so the router re-queues _run_workflow_background.

    Environment delta (if any) is handled inside resume_after_review for task-level reviews.
    For project-level reviews there is no blocked task, so we only update the description
    and re-queue; environment changes are deferred to the next execution cycle.
    """
    existing = project.description or ""
    project.description = existing + _PROJECT_CLARIFICATION_HEADER + clarification.strip()
    db.add(project)

    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.review_task_id = None
    conversation.review_failure_context = None
    conversation.review_episode_attempts = 0
    conversation.review_subphase = None
    conversation.pending_clarification_summary = None
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        "Entendido. He actualizado la configuración del proyecto con tus indicaciones "
        "y voy a reanudar el flujo de trabajo ahora mismo."
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_EXECUTING,
        conversation_id=conversation.id,
        event="review_resolved",
        project_id=project.id,
    )


def _close_review_abandoned(
    db: Session,
    conversation: Conversation,
    task: Task,
) -> AssistantResponse:
    mark_task_failed(db, task.id, auto_commit=False)
    conversation.phase = CONVERSATION_PHASE_EXECUTING
    conversation.review_task_id = None
    conversation.review_failure_context = None
    conversation.review_episode_attempts = 0
    conversation.review_subphase = None
    conversation.pending_clarification_summary = None
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


def _close_project_level_review_abandoned(
    db: Session,
    conversation: Conversation,
    project: Project,
) -> AssistantResponse:
    """
    User abandoned a project-level review episode. No task to mark failed.
    Transition to PAUSED because the underlying infrastructure/configuration problem
    that caused the error likely persists — restarting immediately would just fail again.
    The user can resume manually once they have resolved the external issue.
    """
    conversation.phase = CONVERSATION_PHASE_PAUSED
    conversation.review_task_id = None
    conversation.review_failure_context = None
    conversation.review_episode_attempts = 0
    conversation.review_subphase = None
    conversation.pending_clarification_summary = None
    conversation.updated_at = datetime.now(timezone.utc)

    reply = (
        "Entendido. El problema ha quedado registrado y el proyecto está pausado. "
        "Cuando hayas resuelto el problema externo (configuración del entorno, "
        "dependencias, etc.), puedes reanudar el proyecto desde el botón «Reanudar» "
        "o describirme los cambios necesarios y lo intentamos de nuevo."
    )
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=CONVERSATION_PHASE_PAUSED,
        conversation_id=conversation.id,
        event="review_closed_abandoned",
        project_id=project.id,
    )


# ── Project query (EXECUTING / PAUSED / COMPLETED) ────────────────────────────


def _handle_project_query(
    db: Session,
    conversation: Conversation,
    user_message: str,
) -> AssistantResponse:
    """Answer a question about the project state using the ProjectQueryAgent."""
    project = _get_project_or_raise(db, conversation.project_id)

    try:
        reply = answer_project_query(
            db,
            project_id=project.id,
            project_name=project.name or "Proyecto",
            project_goal=project.description or project.name or "",
            user_question=user_message,
            conversation_phase=conversation.phase,
        )
    except ProjectQueryError as exc:
        logger.warning("project_query_failed conversation_id=%s error=%s", conversation.id, exc)
        reply = "Lo siento, no pude procesar tu pregunta en este momento. ¿Puedes intentarlo de nuevo?"

    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, reply)
    db.commit()

    return AssistantResponse(
        message=reply,
        phase=conversation.phase,
        conversation_id=conversation.id,
        event="project_query_answered",
        project_id=project.id,
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


def _get_task_validation_notes(db: Session, task_id: int) -> str | None:
    """Build a validation_notes string from the latest ExecutionRun for this task."""
    last_run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    if last_run is None:
        return None
    parts = [
        last_run.validation_notes,
        last_run.blockers_found,
        last_run.error_message,
    ]
    combined = " | ".join(p for p in parts if p)
    return combined or None


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
    """Return messages from the current review episode for the evaluator's context."""
    if conversation.review_task_id is None:
        # Project-level review: no task anchor. Return full history so the evaluator
        # sees the opening error message and all subsequent user replies.
        return _load_history(db, conversation.id)

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


def _build_task_summary_for_review(
    db: Session, project_id: int, *, blocked_task_id: int | None = None
) -> str | None:
    """Build a concise task-progress summary for the ReviewEvaluator's context.

    The blocked task is excluded so the LLM doesn't see it both here and in
    the separate blocked_task context passed to the evaluator.
    """
    tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .all()
    )
    if not tasks:
        return None

    completed = [
        t for t in tasks if t.status == TASK_STATUS_COMPLETED and t.id != blocked_task_id
    ]
    failed = [t for t in tasks if t.status == TASK_STATUS_FAILED and t.id != blocked_task_id]
    pending = [
        t for t in tasks if t.status not in TERMINAL_TASK_STATUSES and t.id != blocked_task_id
    ]

    parts: list[str] = []
    if completed:
        titles = ", ".join(f"«{t.title}»" for t in completed)
        parts.append(f"Completed ({len(completed)}): {titles}")
    if failed:
        titles = ", ".join(f"«{t.title}»" for t in failed)
        parts.append(f"Failed ({len(failed)}): {titles}")
    if pending:
        titles = ", ".join(f"«{t.title}»" for t in pending)
        parts.append(f"Pending ({len(pending)}): {titles}")

    return "\n".join(parts) if parts else None


def _build_review_opening(task: Task, validation_notes: str | None) -> str:
    base = (
        f"He detectado un problema con la tarea «{task.title}» que necesita tu ayuda para continuar."
    )
    if validation_notes:
        base += f"\n\nMotivo del bloqueo: {validation_notes}"
    base += "\n\n¿Podrías indicarme cómo quieres resolver esta situación?"
    return base


def _build_workflow_error_opening(failure_reason: str) -> str:
    """
    Build the opening message Aria sends when the workflow stops due to a project-level
    error (no specific task to blame). The message must tell the user exactly what
    happened and what concrete options exist to continue.
    """
    # Classify the failure type from the reason string so we can offer targeted options.
    reason_lower = failure_reason.lower()

    if any(kw in reason_lower for kw in ("environment", "docker", "bootstrap", "entorno", "imagen")):
        options = (
            "• Cambia la imagen base o las dependencias del entorno (descríbeme qué stack técnico necesita tu proyecto)\n"
            "• Ajusta los requisitos técnicos del proyecto para usar una configuración diferente\n"
            "• Si el problema es de conectividad temporal, puedo volver a intentarlo con las mismas opciones"
        )
    elif any(kw in reason_lower for kw in ("empty", "vacío", "no batches", "no tasks", "sin tareas", "empty_execution_plan")):
        options = (
            "• Describe más concretamente qué queda por hacer en el proyecto\n"
            "• Indica si hay tareas específicas que deben ejecutarse aunque ya estén marcadas como completadas\n"
            "• Actualiza el objetivo del proyecto para reflejar el trabajo pendiente real"
        )
    elif any(kw in reason_lower for kw in ("iteration", "iteraci", "limit", "límite", "exhausted", "agotado")):
        options = (
            "• Indica criterios de aceptación más concretos para determinar cuándo una tarea está completa\n"
            "• Describe qué resultados son aceptables aunque no sean perfectos\n"
            "• Simplifica o divide el trabajo en objetivos más pequeños y manejables"
        )
    else:
        options = (
            "• Describe qué cambios quieres hacer en el proyecto o en su configuración\n"
            "• Si es un problema técnico del sistema, puedes intentar reanudar con la configuración actual\n"
            "• Proporciona instrucciones adicionales para que el agente pueda continuar"
        )

    return (
        f"El flujo de trabajo se ha detenido y necesito tu ayuda para continuar.\n\n"
        f"**Qué ha ocurrido:** {failure_reason}\n\n"
        f"**Opciones para continuar:**\n{options}\n\n"
        f"¿Cómo quieres proceder?"
    )


def _build_project_level_block_context(conversation: Conversation) -> BlockedTaskContext:
    """
    Build a synthetic BlockedTaskContext for project-level review episodes so the
    ReviewEvaluator LLM receives structured context about the failure. The failure
    reason is retrieved from review_failure_context (persists across gathering/confirmation
    round-trips, unlike pending_clarification_summary which gets overwritten).
    """
    failure_reason = conversation.review_failure_context or "Error desconocido en el flujo de trabajo."

    reason_lower = failure_reason.lower()
    if any(kw in reason_lower for kw in ("environment", "docker", "bootstrap", "entorno", "imagen")):
        title = "Error de configuración del entorno de ejecución"
    elif any(kw in reason_lower for kw in ("empty", "vacío", "no batches", "sin tareas", "empty_execution_plan")):
        title = "Plan de ejecución vacío — sin tareas ejecutables"
    elif any(kw in reason_lower for kw in ("iteration", "iteraci", "limit", "límite", "exhausted")):
        title = "Límite de iteraciones del flujo de trabajo alcanzado"
    else:
        title = "Error en el flujo de trabajo del proyecto"

    return BlockedTaskContext(
        task_id=0,
        title=title,
        description=failure_reason,
        validation_notes=None,
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
