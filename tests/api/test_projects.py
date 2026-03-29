from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.main import app
from app.models.project import Project


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


def test_create_project_persists_enable_technical_refinement(
    client: TestClient,
    db_session: Session,
):
    response = client.post(
        "/projects",
        json={
            "name": "Proyecto con refinement",
            "description": "Proyecto de prueba",
            "enable_technical_refinement": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["name"] == "Proyecto con refinement"
    assert payload["description"] == "Proyecto de prueba"
    assert payload["enable_technical_refinement"] is True
    assert payload["plan_version"] == 1
    assert "id" in payload

    project = db_session.get(Project, payload["id"])
    assert project is not None
    assert project.name == "Proyecto con refinement"
    assert project.description == "Proyecto de prueba"
    assert project.enable_technical_refinement is True
    assert project.plan_version == 1


def test_create_project_defaults_enable_technical_refinement_to_false(
    client: TestClient,
    db_session: Session,
):
    response = client.post(
        "/projects",
        json={
            "name": "Proyecto sin refinement",
            "description": "Proyecto de prueba",
        },
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["name"] == "Proyecto sin refinement"
    assert payload["description"] == "Proyecto de prueba"
    assert payload["enable_technical_refinement"] is False
    assert payload["plan_version"] == 1
    assert "id" in payload

    project = db_session.get(Project, payload["id"])
    assert project is not None
    assert project.enable_technical_refinement is False
    assert project.plan_version == 1


def test_get_project_returns_enable_technical_refinement_field(
    client: TestClient,
    make_project,
    db_session: Session,
):
    project = make_project(
        name="Proyecto consultable",
        description="Proyecto de prueba",
    )
    project.enable_technical_refinement = True
    project.plan_version = 3
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    payload = response.json()

    assert payload["id"] == project.id
    assert payload["name"] == "Proyecto consultable"
    assert payload["description"] == "Proyecto de prueba"
    assert payload["enable_technical_refinement"] is True
    assert payload["plan_version"] == 3


def test_list_projects_includes_enable_technical_refinement_field(
    client: TestClient,
    make_project,
    db_session: Session,
):
    project_a = make_project(
        name="Proyecto A",
        description="Desc A",
    )
    project_a.enable_technical_refinement = False
    project_a.plan_version = 1

    project_b = make_project(
        name="Proyecto B",
        description="Desc B",
    )
    project_b.enable_technical_refinement = True
    project_b.plan_version = 4

    db_session.add(project_a)
    db_session.add(project_b)
    db_session.commit()
    db_session.refresh(project_a)
    db_session.refresh(project_b)

    response = client.get("/projects")

    assert response.status_code == 200
    payload = response.json()

    assert len(payload) >= 2

    by_id = {item["id"]: item for item in payload}

    assert by_id[project_a.id]["enable_technical_refinement"] is False
    assert by_id[project_b.id]["enable_technical_refinement"] is True
    assert by_id[project_a.id]["plan_version"] == 1
    assert by_id[project_b.id]["plan_version"] == 4
