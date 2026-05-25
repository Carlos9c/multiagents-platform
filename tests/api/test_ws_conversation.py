"""Tests for WebSocket conversation endpoints."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.conversation import (
    CONVERSATION_PHASE_AWAITING_REVIEW,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    Task,
)
from app.schemas.workflow import ProjectWorkflowResult


@pytest.fixture()
def client(db_session):
    from app.api.deps import get_db

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_project(db):
    from app.models.project import Project

    project = Project(name="Test Project", description="A test project")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_conversation(db, project_id):
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_EXECUTING,
        review_episode_attempts=0,
    )
    db.add(conv)
    db.flush()
    msg = ConversationMessage(
        conversation_id=conv.id,
        role="assistant",
        content="Hola! Soy Aria.",
    )
    db.add(msg)
    db.commit()
    db.refresh(conv)
    return conv


def _make_awaiting_task(db, project_id):
    task = Task(
        project_id=project_id,
        title="Configure DB",
        description="Set up connection",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_AWAITING_REVIEW,
        sequence_order=1,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ── REST: active conversation ─────────────────────────────────────────────────


def test_get_active_conversation_not_found(client, db_session):
    project = _make_project(db_session)
    response = client.get(f"/projects/{project.id}/conversations/active")
    assert response.status_code == 404


def test_get_active_conversation_returns_state(client, db_session):
    project = _make_project(db_session)
    conv = _make_conversation(db_session, project.id)

    response = client.get(f"/projects/{project.id}/conversations/active")

    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == conv.id
    assert data["phase"] == "executing"
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "assistant"


# ── REST: notify-review ───────────────────────────────────────────────────────


def test_notify_review_updates_conversation_phase(client, db_session):
    project = _make_project(db_session)
    conv = _make_conversation(db_session, project.id)
    task = _make_awaiting_task(db_session, project.id)

    with patch("app.api.ws.router.manager") as mock_manager:
        mock_manager.has_connections.return_value = False

        response = client.post(
            f"/projects/{project.id}/conversations/notify-review",
            json={"task_id": task.id, "conversation_id": conv.id},
        )

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert data["broadcast_sent"] is False

    db_session.refresh(conv)
    assert conv.phase == "awaiting_review"
    assert conv.review_task_id == task.id


def test_notify_review_nonexistent_conversation_returns_400(client, db_session):
    project = _make_project(db_session)
    task = _make_awaiting_task(db_session, project.id)

    response = client.post(
        f"/projects/{project.id}/conversations/notify-review",
        json={"task_id": task.id, "conversation_id": 99999},
    )
    assert response.status_code == 400


# ── REST: notify-task-event ───────────────────────────────────────────────────


def test_notify_task_event_returns_broadcast_status(client, db_session):
    project = _make_project(db_session)

    with patch("app.api.ws.router.manager") as mock_manager:
        mock_manager.has_connections.return_value = True

        response = client.post(
            f"/projects/{project.id}/conversations/notify-task-event",
            json={"task_id": 1, "task_title": "Setup DB", "status": "completed"},
        )

    assert response.status_code == 200
    assert response.json()["broadcast_sent"] is True


# ── WebSocket: basic chat flow ────────────────────────────────────────────────


def test_websocket_sends_conversation_state_on_connect(client, db_session):
    project = _make_project(db_session)

    with patch(
        "app.api.ws.router.get_or_create_conversation",
        wraps=lambda db, pid: _make_conversation(db, pid),
    ):
        with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
            data = ws.receive_json()

    assert data["type"] == "conversation_state"
    assert "phase" in data
    assert "messages" in data


def test_websocket_responds_to_ping(client, db_session):
    project = _make_project(db_session)

    with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
        ws.receive_json()  # consume conversation_state
        ws.send_json({"type": "ping"})
        data = ws.receive_json()

    assert data["type"] == "pong"


def test_websocket_routes_user_message(client, db_session):
    from app.services.conversation.project_assistant import AssistantResponse

    project = _make_project(db_session)
    mock_response = AssistantResponse(
        message="¿Qué tecnología prefieres?",
        phase="gathering_requirements",
        conversation_id=1,
        event="question",
    )

    with patch(
        "app.api.ws.router.process_user_message",
        return_value=mock_response,
    ):
        with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
            ws.receive_json()  # consume conversation_state
            ws.send_json({"type": "user_message", "content": "Quiero una API"})
            data = ws.receive_json()

    assert data["type"] == "assistant_message"
    assert data["content"] == "¿Qué tecnología prefieres?"
    assert data["event"] == "question"


# ---------------------------------------------------------------------------
# _run_workflow_background — Fix 2: notify Aria on silent manual_review failure
# ---------------------------------------------------------------------------


def _make_manual_review_result(project_id: int = 1) -> "ProjectWorkflowResult":
    return ProjectWorkflowResult(
        project_id=project_id,
        status="awaiting_manual_review",
        planning_completed=True,
        refinement_completed=True,
        atomic_generation_completed=True,
        execution_plan_generated=True,
        manual_review_required=True,
        final_stage_closed=False,
    )


def _make_db_mock(*, awaiting_task=None, failed_task=None, conversation=None):
    """Build a MagicMock DB session that routes query chains to the right stubs."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.order_by.return_value.first.side_effect = [
        awaiting_task,  # first call: AWAITING_REVIEW task search
        failed_task,  # second call: FAILED/PARTIAL task search
    ]
    mock_db.query.return_value.filter.return_value.first.return_value = conversation
    return mock_db


def test_run_workflow_background_notifies_review_when_manual_review_and_failed_task():
    """When workflow returns manual_review_required=True and the failing task ended as FAILED
    (not AWAITING_REVIEW), the bridge must call notify_review_started and broadcast."""
    mock_conv = MagicMock(id=10)
    mock_failed = MagicMock(id=100)
    mock_db = _make_db_mock(awaiting_task=None, failed_task=mock_failed, conversation=mock_conv)

    workflow_result = _make_manual_review_result(project_id=84)

    from app.services.conversation.project_assistant import AssistantResponse

    mock_review_response = AssistantResponse(
        message="Hay un problema con la tarea.",
        phase=CONVERSATION_PHASE_AWAITING_REVIEW,
        conversation_id=mock_conv.id,
        event="question",
    )

    with (
        patch("app.api.ws.router.SessionLocal", return_value=mock_db),
        patch("app.api.ws.router.run_project_workflow", return_value=workflow_result),
        patch(
            "app.api.ws.router.notify_review_started", return_value=mock_review_response
        ) as mock_notify,
        patch("app.api.ws.router.manager") as mock_manager,
    ):
        from app.api.ws.router import _run_workflow_background

        _run_workflow_background(84)

    mock_notify.assert_called_once_with(
        mock_db, conversation_id=mock_conv.id, task_id=mock_failed.id
    )
    mock_manager.broadcast_sync.assert_called_once()


def test_run_workflow_background_does_not_call_review_when_manual_review_false():
    """When manual_review_required=False the fallback bridge must not fire."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    workflow_result = ProjectWorkflowResult(
        project_id=1,
        status="paused",
        planning_completed=True,
        refinement_completed=True,
        atomic_generation_completed=True,
        execution_plan_generated=True,
        manual_review_required=False,
        final_stage_closed=False,
    )

    with (
        patch("app.api.ws.router.SessionLocal", return_value=mock_db),
        patch("app.api.ws.router.run_project_workflow", return_value=workflow_result),
        patch("app.api.ws.router.notify_review_started") as mock_notify,
        patch("app.api.ws.router.notify_project_completed", return_value=None),
        patch("app.api.ws.router.manager"),
    ):
        from app.api.ws.router import _run_workflow_background

        _run_workflow_background(1)

    mock_notify.assert_not_called()


def test_run_workflow_background_calls_notify_workflow_error_when_no_failed_task():
    """When manual_review_required=True but no task exists (env failure, empty plan, etc.),
    notify_workflow_error must be called so Aria can inform the user."""
    mock_conv = MagicMock(id=20)
    mock_db = _make_db_mock(awaiting_task=None, failed_task=None, conversation=mock_conv)

    workflow_result = ProjectWorkflowResult(
        project_id=1,
        status="awaiting_manual_review",
        planning_completed=True,
        refinement_completed=True,
        atomic_generation_completed=True,
        execution_plan_generated=False,
        manual_review_required=True,
        final_stage_closed=False,
        notes="Environment bootstrap failed: image not found.",
    )

    with (
        patch("app.api.ws.router.SessionLocal", return_value=mock_db),
        patch("app.api.ws.router.run_project_workflow", return_value=workflow_result),
        patch("app.api.ws.router.notify_review_started") as mock_task_notify,
        patch("app.api.ws.router.notify_workflow_error") as mock_error_notify,
        patch("app.api.ws.router.manager"),
    ):
        from app.api.ws.router import _run_workflow_background

        _run_workflow_background(1)

    mock_task_notify.assert_not_called()
    mock_error_notify.assert_called_once()
    call_kwargs = mock_error_notify.call_args.kwargs
    assert (
        "bootstrap" in call_kwargs["failure_reason"].lower()
        or "environment" in call_kwargs["failure_reason"].lower()
    )


def test_run_workflow_background_notifies_aria_on_workflow_service_error():
    """ProjectWorkflowServiceError must trigger notify_workflow_error so Aria can tell
    the user what happened instead of leaving the conversation stuck in EXECUTING."""
    mock_conv = MagicMock(id=30)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_conv

    from app.services.project_workflow_service import ProjectWorkflowServiceError

    with (
        patch("app.api.ws.router.SessionLocal", return_value=mock_db),
        patch(
            "app.api.ws.router.run_project_workflow",
            side_effect=ProjectWorkflowServiceError("Docker daemon not reachable"),
        ),
        patch("app.api.ws.router.notify_workflow_error") as mock_error_notify,
        patch("app.api.ws.router.manager"),
    ):
        from app.api.ws.router import _run_workflow_background

        _run_workflow_background(1)

    mock_error_notify.assert_called_once()
    assert "Docker daemon not reachable" in mock_error_notify.call_args.kwargs["failure_reason"]


def test_run_workflow_background_notifies_aria_on_unexpected_exception():
    """Any unhandled exception must trigger notify_workflow_error so Aria can inform the user."""
    mock_conv = MagicMock(id=31)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_conv

    with (
        patch("app.api.ws.router.SessionLocal", return_value=mock_db),
        patch(
            "app.api.ws.router.run_project_workflow",
            side_effect=RuntimeError("Unexpected internal error"),
        ),
        patch("app.api.ws.router.notify_workflow_error") as mock_error_notify,
        patch("app.api.ws.router.manager"),
    ):
        from app.api.ws.router import _run_workflow_background

        _run_workflow_background(1)

    mock_error_notify.assert_called_once()
    assert "Unexpected internal error" in mock_error_notify.call_args.kwargs["failure_reason"]
