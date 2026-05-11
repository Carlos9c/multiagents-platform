"""Tests for WebSocket conversation endpoints."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.conversation import Conversation, ConversationMessage
from app.models.task import PLANNING_LEVEL_ATOMIC, TASK_STATUS_AWAITING_REVIEW, Task


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
    from app.models.conversation import CONVERSATION_PHASE_EXECUTING, CONVERSATION_STATUS_ACTIVE

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
