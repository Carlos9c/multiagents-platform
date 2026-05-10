"""
Integration tests for the runtime environment system.

These tests start real Docker containers and verify the full bootstrap +
validate cycle end-to-end.  They are skipped automatically unless you run:

    poetry run pytest -m integration -v

Requirements:
- Docker Desktop (or daemon) must be running.
- The image ``python:3.12-slim`` must be pullable (or already present).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.environment.bootstrapper import EnvironmentBootstrapper
from app.services.environment.contracts import (
    EnvironmentCommandResult,
    PinnedDependency,
    RuntimeSpec,
)
from app.services.environment.docker_driver import DockerDriver
from app.services.environment.registry import DriverRegistry
from app.services.environment.session_store import EnvironmentSessionStore
from app.services.environment.validator import EnvironmentValidator

pytestmark = pytest.mark.integration

PYTHON_IMAGE = "python:3.12-slim"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker_client():
    """Return a live docker client or skip the whole module."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:
        pytest.skip(f"Docker daemon not available: {exc}")


@pytest.fixture()
def empty_spec() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="python_venv",
        image=PYTHON_IMAGE,
        dependencies=[],
    )


@pytest.fixture()
def spec_with_dep() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="python_venv",
        image=PYTHON_IMAGE,
        dependencies=[PinnedDependency(name="six", version="1.16.0")],
    )


@pytest.fixture()
def driver(docker_client) -> DockerDriver:
    return DockerDriver()


@pytest.fixture()
def registry(driver: DockerDriver) -> DriverRegistry:
    r = DriverRegistry()
    r.register("python_venv", driver)
    return r


@pytest.fixture()
def store() -> EnvironmentSessionStore:
    return EnvironmentSessionStore()


# ---------------------------------------------------------------------------
# DockerDriver unit-style integration
# ---------------------------------------------------------------------------


def test_docker_driver_start_and_stop(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE)
    session = driver.start_session(project_id=100, project_root=tmp_path, spec=spec)

    try:
        assert session.container_id
        assert session.project_id == 100
        assert session.runtime_type == "python_venv"
    finally:
        driver.stop_session(session)


def test_docker_driver_run_command_success(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE)
    session = driver.start_session(project_id=101, project_root=tmp_path, spec=spec)

    try:
        result: EnvironmentCommandResult = driver.run_command(session, "python --version", tmp_path)
        assert result.succeeded
        assert "Python" in result.stdout or "Python" in result.stderr
    finally:
        driver.stop_session(session)


def test_docker_driver_run_command_failure(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE)
    session = driver.start_session(project_id=102, project_root=tmp_path, spec=spec)

    try:
        result = driver.run_command(session, "python -c 'raise SystemExit(42)'", tmp_path)
        assert result.exit_code == 42
        assert not result.succeeded
    finally:
        driver.stop_session(session)


def test_docker_driver_empty_install_succeeds(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE, dependencies=[])
    session = driver.start_session(project_id=103, project_root=tmp_path, spec=spec)

    try:
        result = driver.install_packages(session, spec)
        assert result.succeeded
        assert "No packages" in result.stdout
    finally:
        driver.stop_session(session)


def test_docker_driver_install_real_package(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(
        runtime_type="python_venv",
        image=PYTHON_IMAGE,
        dependencies=[PinnedDependency(name="six", version="1.16.0")],
    )
    session = driver.start_session(project_id=104, project_root=tmp_path, spec=spec)

    try:
        result = driver.install_packages(session, spec)
        assert result.succeeded, f"Install failed: {result.stderr}"
    finally:
        driver.stop_session(session)


def test_docker_driver_generate_lock_file(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(
        runtime_type="python_venv",
        image=PYTHON_IMAGE,
        dependencies=[PinnedDependency(name="six", version="1.16.0")],
    )
    session = driver.start_session(project_id=105, project_root=tmp_path, spec=spec)

    try:
        driver.install_packages(session, spec)
        lock_content = driver.generate_lock_file(session, spec)
        assert isinstance(lock_content, str)
        assert "six" in lock_content
    finally:
        driver.stop_session(session)


def test_docker_driver_stop_already_gone(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE)
    session = driver.start_session(project_id=106, project_root=tmp_path, spec=spec)
    driver.stop_session(session)

    # Stopping again must not raise
    driver.stop_session(session)


def test_host_to_container_path_in_live_session(driver: DockerDriver, tmp_path: Path) -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE)
    session = driver.start_session(project_id=107, project_root=tmp_path, spec=spec)

    try:
        subdir = tmp_path / "executions" / "42" / "run"
        subdir.mkdir(parents=True)
        container_path = session.host_to_container_path(subdir)
        assert container_path == "/project/executions/42/run"
    finally:
        driver.stop_session(session)


# ---------------------------------------------------------------------------
# EnvironmentBootstrapper full cycle
# ---------------------------------------------------------------------------


def test_bootstrapper_bootstrap_and_teardown(
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    tmp_path: Path,
) -> None:
    from app.services.project_storage import ProjectStorageService

    storage = ProjectStorageService(root=tmp_path)
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE, dependencies=[])

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )

    bootstrapper.bootstrap(project_id=200, spec=spec)

    try:
        assert store.get_session(200) is not None
        paths = storage.get_project_paths(200)
        lock_file = paths.env_dir / "requirements.lock"
        assert lock_file.exists()
    finally:
        bootstrapper.teardown(project_id=200)

    assert store.get_session(200) is None


def test_bootstrapper_with_real_package(
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    tmp_path: Path,
) -> None:
    from app.services.project_storage import ProjectStorageService

    storage = ProjectStorageService(root=tmp_path)
    spec = RuntimeSpec(
        runtime_type="python_venv",
        image=PYTHON_IMAGE,
        dependencies=[PinnedDependency(name="six", version="1.16.0")],
    )

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )

    try:
        bootstrapper.bootstrap(project_id=201, spec=spec)
        lock_file = storage.get_project_paths(201).env_dir / "requirements.lock"
        content = lock_file.read_text()
        assert "six" in content
    finally:
        bootstrapper.teardown(project_id=201)


# ---------------------------------------------------------------------------
# EnvironmentValidator smoke-test cycle
# ---------------------------------------------------------------------------


def test_validator_passes_on_empty_spec(
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    tmp_path: Path,
) -> None:
    from app.services.project_storage import ProjectStorageService

    storage = ProjectStorageService(root=tmp_path)
    spec = RuntimeSpec(runtime_type="python_venv", image=PYTHON_IMAGE, dependencies=[])

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )

    try:
        bootstrapper.bootstrap(project_id=300, spec=spec)
        validator = EnvironmentValidator(registry=registry, session_store=store)
        returned_spec = validator.validate(project_id=300, spec=spec)
        assert returned_spec is spec
    finally:
        bootstrapper.teardown(project_id=300)


def test_validator_passes_after_installing_real_package(
    registry: DriverRegistry,
    store: EnvironmentSessionStore,
    tmp_path: Path,
) -> None:
    from app.services.project_storage import ProjectStorageService

    storage = ProjectStorageService(root=tmp_path)
    spec = RuntimeSpec(
        runtime_type="python_venv",
        image=PYTHON_IMAGE,
        dependencies=[PinnedDependency(name="six", version="1.16.0")],
    )

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )

    try:
        bootstrapper.bootstrap(project_id=301, spec=spec)
        validator = EnvironmentValidator(registry=registry, session_store=store)
        returned_spec = validator.validate(project_id=301, spec=spec)
        assert returned_spec is spec
    finally:
        bootstrapper.teardown(project_id=301)
