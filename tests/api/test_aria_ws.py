"""Integration tests for Aria WebSocket and REST conversation endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models.conversation import (
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.task import TASK_STATUS_AWAITING_REVIEW
from app.services.conversation.aria.contracts import AriaStep

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(db_session: Session):
    from app.api.deps import get_db

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _make_conversation(
    db: Session, project_id: int, phase: str = CONVERSATION_PHASE_GATHERING
) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=phase,
        review_episode_attempts=0,
        requirements_ready=False,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _aria_respond(message: str) -> AriaStep:
    return AriaStep(action="respond", response=message, reasoning="test")


# ── WebSocket tests ───────────────────────────────────────────────────────────


class TestWebSocketChat:
    def test_connect_creates_conversation_and_sends_state(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project(name="WS Test")

        with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
            data = ws.receive_json()

        assert data["type"] == "conversation_state"
        assert "phase" in data
        assert data["phase"] == CONVERSATION_PHASE_GATHERING

    def test_user_message_routed_through_aria(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_respond("What are you building?"),
        ):
            with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
                ws.receive_json()  # conversation_state
                ws.send_json({"type": "user_message", "content": "Hello Aria"})
                response = ws.receive_json()

        assert response["type"] == "assistant_message"
        assert response["content"] == "What are you building?"

    def test_ping_pong(self, client: TestClient, db_session: Session, make_project):
        project = make_project()

        with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
            ws.receive_json()  # conversation_state
            ws.send_json({"type": "ping"})
            response = ws.receive_json()

        assert response["type"] == "pong"

    def test_unknown_message_type_returns_error(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()

        with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
            ws.receive_json()  # conversation_state
            ws.send_json({"type": "unknown_type"})
            response = ws.receive_json()

        assert response["type"] == "error"

    def test_empty_message_ignored(self, client: TestClient, db_session: Session, make_project):
        project = make_project()

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_respond("Hi"),
        ) as mock_llm:
            with client.websocket_connect(f"/ws/projects/{project.id}/chat") as ws:
                ws.receive_json()  # conversation_state
                ws.send_json({"type": "user_message", "content": "   "})

        mock_llm.assert_not_called()


# ── REST: notify-review ───────────────────────────────────────────────────────


class TestNotifyReview:
    def test_transitions_to_reviewing(
        self, client: TestClient, db_session: Session, make_project, make_task
    ):
        project = make_project()
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)
        task = make_task(
            project_id=project.id, title="Setup Auth", status=TASK_STATUS_AWAITING_REVIEW
        )

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_respond("La tarea «Setup Auth» necesita tu atención."),
        ):
            response = client.post(
                f"/projects/{project.id}/conversations/notify-review",
                json={"task_id": task.id, "conversation_id": conv.id},
            )

        assert response.status_code == 200
        db_session.refresh(conv)
        assert conv.phase == CONVERSATION_PHASE_REVIEWING
        assert conv.review_context is not None

    def test_returns_404_for_missing_task(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        response = client.post(
            f"/projects/{project.id}/conversations/notify-review",
            json={"task_id": 99999, "conversation_id": conv.id},
        )

        assert response.status_code == 404


# ── REST: active conversation state ──────────────────────────────────────────


class TestGetActiveConversation:
    def test_returns_conversation_state(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        response = client.get(f"/projects/{project.id}/conversations/active")

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == conv.id
        assert data["phase"] == CONVERSATION_PHASE_GATHERING
        assert "requirements_ready" in data

    def test_returns_404_when_no_conversation(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()

        response = client.get(f"/projects/{project.id}/conversations/active")

        assert response.status_code == 404


# ── REST: pause/resume ────────────────────────────────────────────────────────


class TestPauseResume:
    def test_pause_returns_pause_requested(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()

        with patch("app.api.ws.router.request_workflow_stop"):
            response = client.post(f"/projects/{project.id}/conversations/pause")

        assert response.status_code == 200
        assert response.json()["status"] == "pause_requested"

    def test_resume_returns_no_pending_when_no_tasks(
        self, client: TestClient, db_session: Session, make_project
    ):
        project = make_project()
        _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        with patch("app.api.ws.router.clear_workflow_stop"):
            response = client.post(f"/projects/{project.id}/conversations/resume-workflow")

        assert response.status_code == 200
        # No pending tasks → should return no_pending_tasks or not_resumable
        assert response.json()["status"] in (
            "no_pending_tasks",
            "not_resumable",
            "no_active_conversation",
        )
