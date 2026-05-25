"""Tests for ProjectAssistantService (Aria)."""

from unittest.mock import MagicMock, patch

import pytest

from app.models.conversation import (
    CONVERSATION_PHASE_AWAITING_REVIEW,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_READY,
    REVIEW_SUBPHASE_AWAITING_CONFIRMATION,
    REVIEW_SUBPHASE_GATHERING,
    ConversationMessage,
)
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_FAILED,
    Task,
)
from app.services.conversation.confirmation_evaluator import ConfirmationEvaluatorResult
from app.services.conversation.project_assistant import (
    ProjectAssistantError,
    get_or_create_conversation,
    notify_review_started,
    notify_workflow_error,
    process_user_message,
)
from app.services.conversation.requirements_evaluator import RequirementsEvaluatorResult
from app.services.conversation.resumption_service import ResumptionResult
from app.services.conversation.review_evaluator import ReviewEvaluatorResult

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_task(db, project_id, status=TASK_STATUS_AWAITING_REVIEW):
    task = Task(
        project_id=project_id,
        title="Configure DB",
        description="Set up connection",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=status,
        sequence_order=1,
    )
    db.add(task)
    db.flush()
    return task


def _needs_more(question="¿Qué tecnología prefieres?"):
    return RequirementsEvaluatorResult(
        status="needs_more",
        next_question=question,
        updated_draft="Draft so far...",
        reasoning="Missing tech stack",
    )


def _sufficient(draft="Final complete description of the project."):
    return RequirementsEvaluatorResult(
        status="sufficient",
        next_question=None,
        updated_draft=draft,
        reasoning="All covered",
    )


def _review_insufficient(question="¿Qué alternativa propones?"):
    return ReviewEvaluatorResult(
        status="insufficient",
        next_question=question,
        clarification_summary=None,
        action_summary=None,
        reasoning="Not enough info",
    )


def _review_ready_to_confirm(
    summary="Use PostgreSQL instead of SQLite",
    action="I will switch the database driver to PostgreSQL.",
):
    return ReviewEvaluatorResult(
        status="ready_to_confirm",
        next_question=None,
        clarification_summary=summary,
        action_summary=action,
        reasoning="Clear direction gathered",
    )


def _review_abandoned():
    return ReviewEvaluatorResult(
        status="abandoned",
        next_question=None,
        clarification_summary=None,
        action_summary=None,
        reasoning="User wants to stop",
    )


def _confirmation_confirmed():
    return ConfirmationEvaluatorResult(
        confirmed=True,
        follow_up=None,
        reasoning="User said yes",
    )


def _confirmation_rejected(follow_up="¿Qué cambio quieres hacer?"):
    return ConfirmationEvaluatorResult(
        confirmed=False,
        follow_up=follow_up,
        reasoning="User wants to adjust",
    )


# ── get_or_create_conversation ────────────────────────────────────────────────


def test_creates_conversation_with_intro_message(db_session, make_project):
    project = make_project()

    conv = get_or_create_conversation(db_session, project.id)

    assert conv.phase == CONVERSATION_PHASE_GATHERING
    assert conv.status == "active"
    messages = db_session.query(ConversationMessage).filter_by(conversation_id=conv.id).all()
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert "Aria" in messages[0].content


def test_returns_existing_conversation(db_session, make_project):
    project = make_project()
    conv1 = get_or_create_conversation(db_session, project.id)
    conv2 = get_or_create_conversation(db_session, project.id)
    assert conv1.id == conv2.id


# ── Gathering phase ───────────────────────────────────────────────────────────


def test_gathering_needs_more_returns_question(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)

    with patch(
        "app.services.conversation.project_assistant.evaluate_requirements",
        return_value=_needs_more("¿Qué base de datos usarás?"),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Quiero construir una API"
        )

    assert response.event == "question"
    assert response.phase == CONVERSATION_PHASE_GATHERING
    assert "base de datos" in response.message

    db_session.refresh(conv)
    assert conv.requirements_draft == "Draft so far..."


def test_gathering_sufficient_transitions_to_ready(db_session, make_project):
    """When requirements are sufficient, Aria asks for confirmation instead of starting directly."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)

    with patch(
        "app.services.conversation.project_assistant.evaluate_requirements",
        return_value=_sufficient(),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Quiero construir una API REST"
        )

    assert response.event == "requirements_ready"
    assert response.phase == CONVERSATION_PHASE_READY

    db_session.refresh(conv)
    assert conv.phase == CONVERSATION_PHASE_READY
    assert conv.requirements_draft == "Final complete description of the project."


def test_ready_to_start_message_launches_project(db_session, make_project):
    """Any message in ready_to_start phase triggers project start."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_READY
    conv.requirements_draft = "Final complete description of the project."
    db_session.commit()

    mock_start_response = MagicMock()
    mock_start_response.tasks_created = 5

    with patch("app.services.conversation.project_assistant.ProjectStartService") as mock_svc_class:
        mock_svc = MagicMock()
        mock_svc.start.return_value = mock_start_response
        mock_svc_class.return_value = mock_svc

        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="sí, adelante"
        )

    assert response.event == "project_started"
    assert response.phase == CONVERSATION_PHASE_EXECUTING
    assert "5" in response.message

    db_session.refresh(conv)
    assert conv.phase == CONVERSATION_PHASE_EXECUTING


# ── notify_review_started ─────────────────────────────────────────────────────


def test_notify_review_started_switches_phase(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_EXECUTING
    db_session.commit()

    task = _make_task(db_session, project.id)
    db_session.commit()

    response = notify_review_started(db_session, conversation_id=conv.id, task_id=task.id)

    db_session.refresh(conv)
    assert conv.phase == CONVERSATION_PHASE_AWAITING_REVIEW
    assert conv.review_task_id == task.id
    assert conv.review_episode_attempts == 0
    assert conv.review_subphase == REVIEW_SUBPHASE_GATHERING
    assert response.event == "question"
    assert task.title in response.message


# ── Review phase: insufficient (no limit) ────────────────────────────────────


def test_review_insufficient_asks_question_without_limit(db_session, make_project):
    """Aria keeps asking without failing the task, regardless of attempt count."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    conv.review_subphase = REVIEW_SUBPHASE_GATHERING
    conv.review_episode_attempts = 10  # well past the old limit of 3
    db_session.commit()

    with (
        patch(
            "app.services.conversation.project_assistant.evaluate_review",
            return_value=_review_insufficient(),
        ),
        patch(
            "app.services.conversation.project_assistant._get_task_validation_notes",
            return_value=None,
        ),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Todavía no lo sé"
        )

    # Should still be asking — NOT failing the task
    assert response.event == "question"
    assert response.phase == CONVERSATION_PHASE_AWAITING_REVIEW
    db_session.refresh(task)
    assert task.status == TASK_STATUS_AWAITING_REVIEW  # not failed


# ── Review phase: ready_to_confirm → confirmation → resolved ──────────────────


def test_review_ready_to_confirm_requests_confirmation(db_session, make_project):
    """When review evaluator is confident, Aria presents a plan and asks for confirmation."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    conv.review_subphase = REVIEW_SUBPHASE_GATHERING
    db_session.commit()

    with (
        patch(
            "app.services.conversation.project_assistant.evaluate_review",
            return_value=_review_ready_to_confirm(),
        ),
        patch(
            "app.services.conversation.project_assistant._get_task_validation_notes",
            return_value=None,
        ),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Usa PostgreSQL"
        )

    assert response.event == "review_confirmation_requested"
    assert response.phase == CONVERSATION_PHASE_AWAITING_REVIEW

    db_session.refresh(conv)
    assert conv.review_subphase == REVIEW_SUBPHASE_AWAITING_CONFIRMATION
    assert conv.pending_clarification_summary == "Use PostgreSQL instead of SQLite"


def test_review_confirmation_confirmed_resolves_review(db_session, make_project):
    """After the user confirms, the review is resolved and workflow resumes."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    conv.review_subphase = REVIEW_SUBPHASE_AWAITING_CONFIRMATION
    conv.pending_clarification_summary = "Use PostgreSQL instead of SQLite"
    db_session.commit()

    mock_resumption = ResumptionResult(scope="narrow", message="Task retried.", reasoning="ok")

    with (
        patch(
            "app.services.conversation.project_assistant.evaluate_confirmation",
            return_value=_confirmation_confirmed(),
        ),
        patch(
            "app.services.conversation.project_assistant.resume_after_review",
            return_value=mock_resumption,
        ),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Sí, adelante"
        )

    assert response.event == "review_resolved"
    assert response.phase == CONVERSATION_PHASE_EXECUTING
    db_session.refresh(conv)
    assert conv.review_episode_attempts == 0
    assert conv.review_task_id is None
    assert conv.review_subphase is None
    assert conv.pending_clarification_summary is None


def test_review_confirmation_rejected_returns_to_gathering(db_session, make_project):
    """If the user doesn't confirm, Aria resets to gathering and asks a follow-up."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    conv.review_subphase = REVIEW_SUBPHASE_AWAITING_CONFIRMATION
    conv.pending_clarification_summary = "Use PostgreSQL instead of SQLite"
    db_session.commit()

    with patch(
        "app.services.conversation.project_assistant.evaluate_confirmation",
        return_value=_confirmation_rejected("¿Prefieres MySQL en su lugar?"),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Mejor usa MySQL"
        )

    assert response.event == "question"
    assert response.phase == CONVERSATION_PHASE_AWAITING_REVIEW
    assert "MySQL" in response.message
    db_session.refresh(conv)
    assert conv.review_subphase == REVIEW_SUBPHASE_GATHERING
    assert conv.pending_clarification_summary is None


# ── Review phase: abandoned ───────────────────────────────────────────────────


def test_review_abandoned_fails_task(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    conv.review_subphase = REVIEW_SUBPHASE_GATHERING
    db_session.commit()

    with (
        patch(
            "app.services.conversation.project_assistant.evaluate_review",
            return_value=_review_abandoned(),
        ),
        patch(
            "app.services.conversation.project_assistant._get_task_validation_notes",
            return_value=None,
        ),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Abandona esto"
        )

    assert response.event == "review_closed_abandoned"
    db_session.refresh(task)
    assert task.status == TASK_STATUS_FAILED


# ── Executing phase: project Q&A ─────────────────────────────────────────────


def test_message_during_executing_routes_to_project_query_agent(db_session, make_project):
    """During EXECUTING, messages are routed to ProjectQueryAgent for live Q&A."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_EXECUTING
    db_session.commit()

    with patch(
        "app.services.conversation.project_assistant.answer_project_query",
        return_value="El proyecto va bien, se están ejecutando las tareas.",
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="¿Cómo va el proyecto?"
        )

    assert response.event == "project_query_answered"
    assert response.phase == CONVERSATION_PHASE_EXECUTING


# ── Error cases ───────────────────────────────────────────────────────────────


def test_raises_on_nonexistent_conversation(db_session):
    with pytest.raises(ProjectAssistantError, match="not found"):
        process_user_message(db_session, conversation_id=99999, user_message="hola")


# ── notify_workflow_error ─────────────────────────────────────────────────────


def test_notify_workflow_error_sets_project_level_review_state(db_session, make_project):
    """notify_workflow_error transitions to AWAITING_REVIEW with no task anchor."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_EXECUTING
    db_session.commit()

    failure_reason = "Docker image 'python:3.11' could not be pulled — network error."

    response = notify_workflow_error(
        db_session, conversation_id=conv.id, failure_reason=failure_reason
    )

    db_session.refresh(conv)
    assert conv.phase == CONVERSATION_PHASE_AWAITING_REVIEW
    assert conv.review_task_id is None
    assert conv.review_failure_context == failure_reason
    assert conv.review_episode_attempts == 0
    assert conv.review_subphase == REVIEW_SUBPHASE_GATHERING
    assert conv.pending_clarification_summary is None
    assert response.event == "question"
    assert response.phase == CONVERSATION_PHASE_AWAITING_REVIEW


def test_notify_workflow_error_opening_message_contains_failure_reason(db_session, make_project):
    """The opening message Aria sends must include what happened and recovery options."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_EXECUTING
    db_session.commit()

    failure_reason = "Environment bootstrap failed: Docker daemon not reachable."

    response = notify_workflow_error(
        db_session, conversation_id=conv.id, failure_reason=failure_reason
    )

    # Message must explicitly state what happened
    assert failure_reason in response.message
    # Message must offer concrete options
    assert "Opciones para continuar" in response.message


# ── Project-level review flow ─────────────────────────────────────────────────


def test_project_level_review_gathering_calls_evaluator_with_is_project_level_flag(
    db_session, make_project
):
    """When review_task_id is None, evaluate_review is called with is_project_level_error=True."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = None
    conv.review_failure_context = "Environment bootstrap failed."
    conv.review_subphase = REVIEW_SUBPHASE_GATHERING
    db_session.commit()

    captured: list = []

    def capture_evaluate_review(inp):
        captured.append(inp)
        return _review_insufficient()

    with patch(
        "app.services.conversation.project_assistant.evaluate_review",
        side_effect=capture_evaluate_review,
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="¿Cómo lo arreglo?"
        )

    assert response.phase == CONVERSATION_PHASE_AWAITING_REVIEW
    assert len(captured) == 1
    assert captured[0].is_project_level_error is True
    # The synthetic BlockedTaskContext must include the failure reason
    assert (
        "bootstrap" in captured[0].blocked_task.description.lower()
        or "bootstrap" in (captured[0].blocked_task.title or "").lower()
        or "bootstrap" in captured[0].blocked_task.description.lower()
    )


def test_project_level_review_resolution_transitions_to_executing(db_session, make_project):
    """When user confirms a project-level plan, conversation goes to EXECUTING with review_resolved."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = None
    conv.review_failure_context = "Empty execution plan: no batches generated."
    conv.review_subphase = REVIEW_SUBPHASE_AWAITING_CONFIRMATION
    conv.pending_clarification_summary = "Add explicit tasks for initialising the database schema."
    db_session.commit()

    with patch(
        "app.services.conversation.project_assistant.evaluate_confirmation",
        return_value=_confirmation_confirmed(),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Sí, procede"
        )

    assert response.event == "review_resolved"
    assert response.phase == CONVERSATION_PHASE_EXECUTING
    assert response.project_id == project.id

    db_session.refresh(conv)
    assert conv.review_task_id is None
    assert conv.review_failure_context is None
    assert conv.review_subphase is None
    assert conv.pending_clarification_summary is None

    # Clarification must have been embedded into the project description
    db_session.refresh(project)
    assert "Add explicit tasks" in (project.description or "")


def test_project_level_review_resolution_failure_context_persists_through_round_trip(
    db_session, make_project
):
    """review_failure_context must survive a gathering→confirmation→back_to_gathering round-trip."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = None
    original_failure = "Workflow iteration limit reached after 5 iterations."
    conv.review_failure_context = original_failure
    conv.review_subphase = REVIEW_SUBPHASE_AWAITING_CONFIRMATION
    conv.pending_clarification_summary = "Use simpler tasks."
    db_session.commit()

    # User rejects the plan → back to gathering; review_failure_context must survive
    with patch(
        "app.services.conversation.project_assistant.evaluate_confirmation",
        return_value=_confirmation_rejected("¿Qué criterios de aceptación usarás?"),
    ):
        process_user_message(
            db_session, conversation_id=conv.id, user_message="No, quiero cambiar el plan"
        )

    db_session.refresh(conv)
    assert conv.review_failure_context == original_failure
    assert conv.review_subphase == REVIEW_SUBPHASE_GATHERING


def test_project_level_review_abandoned_transitions_to_paused(db_session, make_project):
    """Abandoning a project-level review transitions to PAUSED (not EXECUTING)."""
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = None
    conv.review_failure_context = "Environment setup failed."
    conv.review_subphase = REVIEW_SUBPHASE_GATHERING
    db_session.commit()

    with patch(
        "app.services.conversation.project_assistant.evaluate_review",
        return_value=_review_abandoned(),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Olvídalo"
        )

    assert response.event == "review_closed_abandoned"
    # Must be PAUSED (not EXECUTING) — infrastructure issue likely still present
    assert response.phase == "paused"
    db_session.refresh(conv)
    assert conv.phase == "paused"
    assert conv.review_failure_context is None
