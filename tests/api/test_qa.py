"""Tests for QA API endpoints — Phase 10."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.main import app
from app.models.project import Project
from app.models.qa_finding import QAFinding as QAFindingModel
from app.models.qa_session import (
    QA_SESSION_STATUS_COMPLETED,
    QA_SESSION_STATUS_RUNNING,
)
from app.models.qa_session import (
    QASession as QASessionModel,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _make_project(
    db: Session,
    project_id: int = 1,
    product_type: str = "rest_api",
) -> Project:
    project = Project(
        id=project_id,
        name="Test Project",
        description="A test project goal",
        product_type=product_type,
    )
    db.add(project)
    db.flush()
    return project


def _make_qa_session(
    db: Session,
    project_id: int = 1,
    status: str = QA_SESSION_STATUS_COMPLETED,
    verdict: str = "passed",
) -> QASessionModel:
    session = QASessionModel(
        project_id=project_id,
        triggered_by="user",
        status=status,
        product_type="rest_api",
        verdict=verdict,
        agents_called=["functional_qa_agent"],
        findings_summary={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        duration_seconds=4.5,
    )
    db.add(session)
    db.flush()
    return session


def _make_qa_finding(
    db: Session,
    qa_session_id: int,
    finding_id: str = "f-001",
    severity: str = "high",
) -> QAFindingModel:
    finding = QAFindingModel(
        qa_session_id=qa_session_id,
        finding_id=finding_id,
        severity=severity,
        category="functional",
        title=f"Finding {finding_id}",
        description="A test finding",
        auto_remediable=False,
    )
    db.add(finding)
    db.flush()
    return finding


# ── POST /qa/{project_id}/run ─────────────────────────────────────────────────


class TestRunQAEndpoint:
    def test_returns_201_with_qa_session_id(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        db_session.commit()

        with (
            patch("app.api.qa.create_qa_session") as mock_create,
            patch("app.api.qa._submit_qa_background"),
        ):
            mock_session = MagicMock()
            mock_session.id = 42
            mock_create.return_value = mock_session

            response = client.post("/qa/1/run")

        assert response.status_code == 200
        payload = response.json()
        assert payload["qa_session_id"] == 42
        assert payload["status"] == QA_SESSION_STATUS_RUNNING
        assert payload["project_id"] == 1

    def test_returns_404_when_project_not_found(
        self,
        client: TestClient,
        db_session: Session,
    ):
        response = client.post("/qa/999/run")
        assert response.status_code == 404

    def test_background_task_submitted(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        db_session.commit()

        submitted_args: list = []

        def _fake_submit(project_id: int, qa_session_id: int) -> None:
            submitted_args.append((project_id, qa_session_id))

        with (
            patch("app.api.qa.create_qa_session") as mock_create,
            patch("app.api.qa._submit_qa_background", side_effect=_fake_submit),
        ):
            mock_session = MagicMock()
            mock_session.id = 7
            mock_create.return_value = mock_session

            client.post("/qa/1/run")

        assert len(submitted_args) == 1
        assert submitted_args[0] == (1, 7)


# ── POST /projects/{project_id}/conversations/notify-qa-completed ─────────────


class TestNotifyQACompletedEndpoint:
    def _make_aria_response(self) -> MagicMock:
        resp = MagicMock()
        resp.message = "QA completed with no issues."
        resp.event = "qa_completed_no_issues"
        resp.phase = "completed"
        resp.conversation_id = 1
        resp.project_id = 1
        return resp

    def test_returns_200_with_message(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        db_session.commit()

        mock_resp = self._make_aria_response()

        with (
            patch(
                "app.api.qa.process_with_pre_transitions",
                return_value=mock_resp,
            ),
            patch("app.api.qa.manager"),
            patch("app.api.qa.assistant_message", return_value={}),
        ):
            response = client.post(
                "/projects/1/conversations/notify-qa-completed",
                json={
                    "conversation_id": 1,
                    "verdict": "passed",
                    "has_findings": False,
                    "critical_count": 0,
                    "high_count": 0,
                    "findings_summary": {},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["message"] == "QA completed with no issues."

    def test_routes_findings_to_aria(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        db_session.commit()

        mock_resp = MagicMock()
        mock_resp.message = "Found 2 issues."
        mock_resp.event = "qa_completed_with_findings"
        mock_resp.phase = "completed"
        mock_resp.conversation_id = 1
        mock_resp.project_id = 1

        captured: list = []

        def _capture(db, conv_id, aria_input):
            captured.append(aria_input.system_event.data)
            return mock_resp

        with (
            patch("app.api.qa.process_with_pre_transitions", side_effect=_capture),
            patch("app.api.qa.manager"),
            patch("app.api.qa.assistant_message", return_value={}),
        ):
            client.post(
                "/projects/1/conversations/notify-qa-completed",
                json={
                    "conversation_id": 1,
                    "verdict": "failed",
                    "has_findings": True,
                    "critical_count": 1,
                    "high_count": 1,
                    "findings_summary": {"critical": 1, "high": 1},
                },
            )

        assert len(captured) == 1
        data = captured[0]
        assert data["verdict"] == "failed"
        assert data["has_findings"] is True
        assert data["critical_count"] == 1

    def test_returns_400_when_aria_raises(
        self,
        client: TestClient,
        db_session: Session,
    ):
        from app.services.conversation.aria import AriaOrchestratorError

        _make_project(db_session)
        db_session.commit()

        with patch(
            "app.api.qa.process_with_pre_transitions",
            side_effect=AriaOrchestratorError("invalid state"),
        ):
            response = client.post(
                "/projects/1/conversations/notify-qa-completed",
                json={
                    "conversation_id": 1,
                    "verdict": "passed",
                    "has_findings": False,
                },
            )

        assert response.status_code == 400


# ── GET /qa/{project_id}/sessions ─────────────────────────────────────────────


class TestListQASessionsEndpoint:
    def test_returns_empty_list_when_no_sessions(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        db_session.commit()

        response = client.get("/qa/1/sessions")
        assert response.status_code == 200
        assert response.json() == []

    def test_returns_sessions_latest_first(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        s1 = _make_qa_session(db_session, project_id=1, verdict="passed")
        s2 = _make_qa_session(db_session, project_id=1, verdict="failed")
        db_session.commit()

        response = client.get("/qa/1/sessions")
        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) == 2
        # Latest first
        assert sessions[0]["id"] == s2.id
        assert sessions[1]["id"] == s1.id

    def test_does_not_include_findings_in_list(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        s = _make_qa_session(db_session, project_id=1)
        _make_qa_finding(db_session, qa_session_id=s.id)
        db_session.commit()

        response = client.get("/qa/1/sessions")
        assert response.status_code == 200
        session = response.json()[0]
        # List endpoint returns empty findings (not loaded)
        assert session["findings"] == []

    def test_respects_limit_parameter(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        for _ in range(5):
            _make_qa_session(db_session, project_id=1)
        db_session.commit()

        response = client.get("/qa/1/sessions?limit=3")
        assert response.status_code == 200
        assert len(response.json()) == 3


# ── GET /qa/{project_id}/sessions/{qa_session_id} ─────────────────────────────


class TestGetQASessionEndpoint:
    def test_returns_session_with_findings(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        s = _make_qa_session(db_session, verdict="failed")
        _make_qa_finding(db_session, qa_session_id=s.id, finding_id="f-001", severity="high")
        _make_qa_finding(db_session, qa_session_id=s.id, finding_id="f-002", severity="medium")
        db_session.commit()

        response = client.get(f"/qa/1/sessions/{s.id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["verdict"] == "failed"
        assert len(payload["findings"]) == 2
        assert payload["findings"][0]["finding_id"] == "f-001"

    def test_returns_404_for_missing_session(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        db_session.commit()

        response = client.get("/qa/1/sessions/9999")
        assert response.status_code == 404

    def test_returns_404_when_session_belongs_to_different_project(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session, project_id=1)
        _make_project(db_session, project_id=2)
        s = _make_qa_session(db_session, project_id=2)
        db_session.commit()

        # Session belongs to project 2 but request targets project 1
        response = client.get(f"/qa/1/sessions/{s.id}")
        assert response.status_code == 404

    def test_session_without_findings(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        s = _make_qa_session(db_session, verdict="passed")
        db_session.commit()

        response = client.get(f"/qa/1/sessions/{s.id}")
        assert response.status_code == 200
        assert response.json()["findings"] == []

    def test_returns_all_session_fields(
        self,
        client: TestClient,
        db_session: Session,
    ):
        _make_project(db_session)
        s = _make_qa_session(db_session, verdict="passed")
        db_session.commit()

        response = client.get(f"/qa/1/sessions/{s.id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == s.id
        assert payload["project_id"] == 1
        assert payload["status"] == QA_SESSION_STATUS_COMPLETED
        assert payload["product_type"] == "rest_api"
        assert payload["duration_seconds"] == 4.5
        assert isinstance(payload["agents_called"], list)
