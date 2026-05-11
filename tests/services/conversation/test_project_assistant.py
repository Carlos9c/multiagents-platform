"""Tests for ProjectAssistantService (Aria)."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_AWAITING_REVIEW,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_READY,
    Conversation,
    ConversationMessage,
)
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.conversation.project_assistant import (
    REVIEW_ATTEMPTS_LIMIT,
    ProjectAssistantError,
    get_or_create_conversation,
    notify_review_started,
    process_user_message,
)
from app.services.conversation.requirements_evaluator import RequirementsEvaluatorResult
from app.services.conversation.review_evaluator import ReviewEvaluatorResult
from app.services.conversation.resumption_service import ResumptionResult


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
        reasoning="Not enough info",
    )


def _review_resolved(summary="Use PostgreSQL instead of SQLite"):
    return ReviewEvaluatorResult(
        status="resolved",
        next_question=None,
        clarification_summary=summary,
        reasoning="Clear direction",
    )


def _review_abandoned():
    return ReviewEvaluatorResult(
        status="abandoned",
        next_question=None,
        clarification_summary=None,
        reasoning="User wants to stop",
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

    with patch(
        "app.services.conversation.project_assistant.ProjectStartService"
    ) as mock_svc_class:
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
    # Simulate execution phase
    conv.phase = CONVERSATION_PHASE_EXECUTING
    db_session.commit()

    task = _make_task(db_session, project.id)
    db_session.commit()

    response = notify_review_started(
        db_session, conversation_id=conv.id, task_id=task.id
    )

    db_session.refresh(conv)
    assert conv.phase == CONVERSATION_PHASE_AWAITING_REVIEW
    assert conv.review_task_id == task.id
    assert conv.review_episode_attempts == 0
    assert response.event == "question"
    assert task.title in response.message


# ── Review phase: resolved ────────────────────────────────────────────────────


def test_review_resolved_calls_resumption_and_switches_to_executing(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    db_session.commit()

    mock_resumption = ResumptionResult(
        scope="narrow", message="Task retried.", reasoning="ok"
    )

    with patch(
        "app.services.conversation.project_assistant.evaluate_review",
        return_value=_review_resolved(),
    ), patch(
        "app.services.conversation.project_assistant.resume_after_review",
        return_value=mock_resumption,
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Usa PostgreSQL"
        )

    assert response.event == "review_resolved"
    assert response.phase == CONVERSATION_PHASE_EXECUTING
    db_session.refresh(conv)
    assert conv.review_episode_attempts == 0
    assert conv.review_task_id is None


# ── Review phase: insufficient → limit ───────────────────────────────────────


def test_review_limit_reached_fails_task(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    conv.review_episode_attempts = REVIEW_ATTEMPTS_LIMIT - 1
    db_session.commit()

    with patch(
        "app.services.conversation.project_assistant.evaluate_review",
        return_value=_review_insufficient(),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Todavía no lo sé"
        )

    assert response.event == "review_closed_limit_reached"
    db_session.refresh(task)
    assert task.status == TASK_STATUS_FAILED
    db_session.refresh(conv)
    assert conv.review_episode_attempts == 0


# ── Review phase: abandoned ───────────────────────────────────────────────────


def test_review_abandoned_fails_task(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    task = _make_task(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_AWAITING_REVIEW
    conv.review_task_id = task.id
    db_session.commit()

    with patch(
        "app.services.conversation.project_assistant.evaluate_review",
        return_value=_review_abandoned(),
    ):
        response = process_user_message(
            db_session, conversation_id=conv.id, user_message="Abandona esto"
        )

    assert response.event == "review_closed_abandoned"
    db_session.refresh(task)
    assert task.status == TASK_STATUS_FAILED


# ── Executing phase ───────────────────────────────────────────────────────────


def test_message_during_executing_returns_ack(db_session, make_project):
    project = make_project()
    conv = get_or_create_conversation(db_session, project.id)
    conv.phase = CONVERSATION_PHASE_EXECUTING
    db_session.commit()

    response = process_user_message(
        db_session, conversation_id=conv.id, user_message="¿Cómo va el proyecto?"
    )

    assert response.event == "executing"
    assert response.phase == CONVERSATION_PHASE_EXECUTING


# ── Error cases ───────────────────────────────────────────────────────────────


def test_raises_on_nonexistent_conversation(db_session):
    with pytest.raises(ProjectAssistantError, match="not found"):
        process_user_message(db_session, conversation_id=99999, user_message="hola")
