from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.main import app
from app.services.analysis.base import CodebaseAnalysis, FileAnalysis

# ── fixtures ──────────────────────────────────────────────────────────────────


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


def make_analysis() -> CodebaseAnalysis:
    return CodebaseAnalysis(
        analyzed_at="2026-01-01T00:00:00+00:00",
        source_path="/project/src",
        total_files=2,
        project_summary="A test project with a REST API.",
        main_technologies=["Python", "FastAPI"],
        entry_points=["main.py"],
        files=[
            FileAnalysis(
                path="main.py",
                file_type="code",
                language="Python",
                summary="Application entry point.",
                key_elements=["app"],
                dependencies=["fastapi"],
            ),
            FileAnalysis(
                path="README.md",
                file_type="docs",
                language=None,
                summary="Project documentation.",
                key_elements=[],
                dependencies=[],
            ),
        ],
    )


# ── GET /projects/{id}/analysis ───────────────────────────────────────────────


def test_get_analysis_returns_404_when_project_not_found(client: TestClient):
    response = client.get("/projects/9999/analysis")
    assert response.status_code == 404
    assert "project" in response.json()["detail"].lower()


def test_get_analysis_returns_404_when_no_analysis_stored(client: TestClient, make_project):
    project = make_project()
    with patch("app.api.projects.CodebaseAnalysisService") as MockSvc:
        MockSvc.return_value.get_analysis.return_value = None
        response = client.get(f"/projects/{project.id}/analysis")

    assert response.status_code == 404
    assert "analysis" in response.json()["detail"].lower()


def test_get_analysis_returns_200_with_correct_shape(client: TestClient, make_project):
    project = make_project()
    analysis = make_analysis()

    with patch("app.api.projects.CodebaseAnalysisService") as MockSvc:
        MockSvc.return_value.get_analysis.return_value = analysis
        response = client.get(f"/projects/{project.id}/analysis")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_files"] == 2
    assert payload["project_summary"] == "A test project with a REST API."
    assert "FastAPI" in payload["main_technologies"]
    assert payload["entry_points"] == ["main.py"]
    assert len(payload["files"]) == 2


def test_get_analysis_files_have_expected_fields(client: TestClient, make_project):
    project = make_project()
    analysis = make_analysis()

    with patch("app.api.projects.CodebaseAnalysisService") as MockSvc:
        MockSvc.return_value.get_analysis.return_value = analysis
        response = client.get(f"/projects/{project.id}/analysis")

    files = response.json()["files"]
    main_py = next(f for f in files if f["path"] == "main.py")
    assert main_py["file_type"] == "code"
    assert main_py["language"] == "Python"
    assert "Application entry point" in main_py["summary"]

    readme = next(f for f in files if f["path"] == "README.md")
    assert readme["language"] is None
    assert readme["file_type"] == "docs"


def test_get_analysis_calls_service_with_correct_project_id(client: TestClient, make_project):
    project = make_project()

    with patch("app.api.projects.CodebaseAnalysisService") as MockSvc:
        MockSvc.return_value.get_analysis.return_value = make_analysis()
        client.get(f"/projects/{project.id}/analysis")
        MockSvc.return_value.get_analysis.assert_called_once_with(project.id)


# ── GET /projects/{id}/plan-history ──────────────────────────────────────────


def test_get_plan_history_returns_404_when_project_not_found(client: TestClient):
    response = client.get("/projects/9999/plan-history")
    assert response.status_code == 404
    assert "project" in response.json()["detail"].lower()


def test_get_plan_history_returns_empty_list_when_no_history(client: TestClient, make_project):
    project = make_project()

    with (
        patch("app.api.projects.ProjectStorageService") as _,
        patch("app.api.projects.PlanHistoryService") as MockHistory,
    ):
        MockHistory.return_value.get_history.return_value = []
        response = client.get(f"/projects/{project.id}/plan-history")

    assert response.status_code == 200
    assert response.json() == []


def test_get_plan_history_returns_cycles_in_order(client: TestClient, make_project):
    project = make_project()
    cycles = [
        {
            "planned_at": "2026-01-01T10:00:00+00:00",
            "objective": "Initial project setup.",
            "source_path": "/workspace/project",
        },
        {
            "planned_at": "2026-02-01T10:00:00+00:00",
            "objective": "Add authentication layer.",
            "source_path": "/workspace/project",
        },
    ]

    with (
        patch("app.api.projects.ProjectStorageService") as _,
        patch("app.api.projects.PlanHistoryService") as MockHistory,
    ):
        MockHistory.return_value.get_history.return_value = cycles
        response = client.get(f"/projects/{project.id}/plan-history")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert payload[0]["objective"] == "Initial project setup."
    assert payload[1]["objective"] == "Add authentication layer."
    assert payload[0]["source_path"] == "/workspace/project"


def test_get_plan_history_cycle_without_source_path(client: TestClient, make_project):
    project = make_project()
    cycles = [
        {
            "planned_at": "2026-01-01T10:00:00+00:00",
            "objective": "Empty workspace project.",
            "source_path": None,
        }
    ]

    with (
        patch("app.api.projects.ProjectStorageService") as _,
        patch("app.api.projects.PlanHistoryService") as MockHistory,
    ):
        MockHistory.return_value.get_history.return_value = cycles
        response = client.get(f"/projects/{project.id}/plan-history")

    assert response.status_code == 200
    assert response.json()[0]["source_path"] is None
