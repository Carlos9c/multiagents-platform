from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.services.environment.contracts import EnvironmentSession
from app.services.environment.session_store import EnvironmentSessionStore


@pytest.fixture()
def store() -> EnvironmentSessionStore:
    return EnvironmentSessionStore()


@pytest.fixture()
def session(tmp_path: Path) -> EnvironmentSession:
    return EnvironmentSession(
        project_id=1,
        container_id="ctr-abc",
        project_root=tmp_path,
        runtime_type="python_venv",
    )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


def test_set_and_get_session(store: EnvironmentSessionStore, session: EnvironmentSession) -> None:
    store.set_session(1, session)
    result = store.get_session(1)
    assert result is session


def test_get_missing_session_returns_none(store: EnvironmentSessionStore) -> None:
    assert store.get_session(999) is None


def test_remove_existing_session(
    store: EnvironmentSessionStore, session: EnvironmentSession
) -> None:
    store.set_session(1, session)
    store.remove_session(1)
    assert store.get_session(1) is None


def test_remove_missing_session_is_noop(store: EnvironmentSessionStore) -> None:
    # Should not raise
    store.remove_session(999)


def test_get_all_sessions(store: EnvironmentSessionStore, tmp_path: Path) -> None:
    s1 = EnvironmentSession(
        project_id=1, container_id="c1", project_root=tmp_path, runtime_type="python_venv"
    )
    s2 = EnvironmentSession(
        project_id=2, container_id="c2", project_root=tmp_path, runtime_type="node_npm"
    )
    store.set_session(1, s1)
    store.set_session(2, s2)

    all_sessions = store.get_all_sessions()
    assert len(all_sessions) == 2
    assert all_sessions[1] is s1
    assert all_sessions[2] is s2


def test_overwrite_existing_session(store: EnvironmentSessionStore, tmp_path: Path) -> None:
    s1 = EnvironmentSession(
        project_id=1, container_id="old", project_root=tmp_path, runtime_type="python_venv"
    )
    s2 = EnvironmentSession(
        project_id=1, container_id="new", project_root=tmp_path, runtime_type="python_venv"
    )
    store.set_session(1, s1)
    store.set_session(1, s2)

    assert store.get_session(1).container_id == "new"


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    store = EnvironmentSessionStore()
    errors: list[Exception] = []

    def writer(project_id: int) -> None:
        try:
            s = EnvironmentSession(
                project_id=project_id,
                container_id=f"ctr-{project_id}",
                project_root=tmp_path,
                runtime_type="python_venv",
            )
            for _ in range(50):
                store.set_session(project_id, s)
                store.get_session(project_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
