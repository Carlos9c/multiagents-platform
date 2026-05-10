from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.environment.bootstrapper import EnvironmentBootstrapper
from app.services.environment.contracts import (
    EnvironmentBootstrapError,
    EnvironmentCommandResult,
    EnvironmentSession,
    PinnedDependency,
    RuntimeSpec,
)
from app.services.environment.registry import DriverRegistry
from app.services.environment.session_store import EnvironmentSessionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="python_venv",
        image="python:3.12-slim",
        dependencies=[PinnedDependency(name="numpy", version="1.26.0")],
    )


@pytest.fixture()
def mock_session(tmp_path: Path) -> EnvironmentSession:
    return EnvironmentSession(
        project_id=1,
        container_id="ctr-abc",
        project_root=tmp_path,
        runtime_type="python_venv",
    )


@pytest.fixture()
def mock_driver(mock_session: EnvironmentSession) -> MagicMock:
    driver = MagicMock()
    driver.start_session.return_value = mock_session
    driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=0, stdout="Successfully installed numpy-1.26.0", stderr=""
    )
    driver.generate_lock_file.return_value = "numpy==1.26.0\n"
    return driver


@pytest.fixture()
def registry(mock_driver: MagicMock) -> DriverRegistry:
    r = DriverRegistry()
    r.register("python_venv", mock_driver)
    return r


@pytest.fixture()
def store() -> EnvironmentSessionStore:
    return EnvironmentSessionStore()


@pytest.fixture()
def storage(tmp_path: Path):
    from app.services.project_storage import ProjectStorageService

    return ProjectStorageService(root=tmp_path)


# ---------------------------------------------------------------------------
# bootstrap() — happy path
# ---------------------------------------------------------------------------


def test_bootstrap_returns_session(
    spec: RuntimeSpec,
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    session = bootstrapper.bootstrap(project_id=1, spec=spec)

    assert session.project_id == 1
    assert session.container_id == "ctr-abc"


def test_bootstrap_stores_session_in_store(
    spec: RuntimeSpec,
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=1, spec=spec)

    assert store.get_session(1) is not None
    assert store.get_session(1).container_id == "ctr-abc"


def test_bootstrap_writes_lock_file(
    spec: RuntimeSpec,
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=1, spec=spec)

    paths = storage.get_project_paths(1)
    lock_file = paths.env_dir / "requirements.lock"
    assert lock_file.exists()
    assert "numpy==1.26.0" in lock_file.read_text()


# ---------------------------------------------------------------------------
# bootstrap() — install failure
# ---------------------------------------------------------------------------


def test_bootstrap_raises_on_install_failure(
    spec: RuntimeSpec,
    mock_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    mock_driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="Could not find a version that satisfies"
    )
    registry = DriverRegistry()
    registry.register("python_venv", mock_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    with pytest.raises(EnvironmentBootstrapError) as exc_info:
        bootstrapper.bootstrap(project_id=1, spec=spec)

    assert "installation failed" in str(exc_info.value).lower()


def test_bootstrap_cleans_up_on_install_failure(
    spec: RuntimeSpec,
    mock_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    mock_driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="error"
    )
    registry = DriverRegistry()
    registry.register("python_venv", mock_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    with pytest.raises(EnvironmentBootstrapError):
        bootstrapper.bootstrap(project_id=1, spec=spec)

    # Session must not remain in store after failure
    assert store.get_session(1) is None
    # Container must be stopped
    mock_driver.stop_session.assert_called_once()


# ---------------------------------------------------------------------------
# teardown()
# ---------------------------------------------------------------------------


def test_teardown_stops_container_and_removes_session(
    spec: RuntimeSpec,
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    storage,
    mock_driver: MagicMock,
) -> None:
    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=1, spec=spec)
    assert store.get_session(1) is not None

    bootstrapper.teardown(project_id=1)

    assert store.get_session(1) is None
    mock_driver.stop_session.assert_called_once()


def test_teardown_is_noop_when_no_session(
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    storage,
    mock_driver: MagicMock,
) -> None:
    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    # Should not raise
    bootstrapper.teardown(project_id=99)

    mock_driver.stop_session.assert_not_called()
