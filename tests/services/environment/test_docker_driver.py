from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.environment.contracts import (
    EnvironmentSession,
    PinnedDependency,
    RuntimeSpec,
)
from app.services.environment.contracts import EnvironmentBootstrapError
from app.services.environment.docker_driver import (
    DockerDriver,
    _build_install_command,
    _build_lock_command,
    _build_smoke_test_command,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec_python() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="python_venv",
        image="python:3.12-slim",
        dependencies=[
            PinnedDependency(name="xgboost", version="2.1.3"),
            PinnedDependency(name="scikit-learn", version="1.4.2"),
        ],
    )


@pytest.fixture()
def spec_node() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="node_npm",
        image="node:20-slim",
        dependencies=[
            PinnedDependency(name="express", version="4.18.2"),
        ],
    )


@pytest.fixture()
def spec_empty() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="python_venv",
        image="python:3.12-slim",
        dependencies=[],
    )


@pytest.fixture()
def spec_java() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="java_maven",
        image="public.ecr.aws/docker/library/maven:3.9-eclipse-temurin-21",
        dependencies=[],
    )


@pytest.fixture()
def mock_docker_client():
    with patch("app.services.environment.docker_driver.docker") as mock_docker_module:
        mock_client = MagicMock()
        mock_docker_module.from_env.return_value = mock_client
        mock_docker_module.errors.NotFound = Exception
        mock_docker_module.errors.ImageNotFound = Exception
        yield mock_client


@pytest.fixture()
def session(tmp_path: Path) -> EnvironmentSession:
    return EnvironmentSession(
        project_id=1,
        container_id="abc123",
        project_root=tmp_path,
        runtime_type="python_venv",
        container_mount_point="/project",
    )


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def test_build_install_command_python(spec_python: RuntimeSpec) -> None:
    cmd = _build_install_command(spec_python)
    assert cmd.startswith("pip install --no-cache-dir")
    assert "xgboost==2.1.3" in cmd
    assert "scikit-learn==1.4.2" in cmd


def test_build_install_command_with_extras() -> None:
    spec = RuntimeSpec(
        runtime_type="python_venv",
        image="python:3.12-slim",
        dependencies=[PinnedDependency(name="xgboost", version="2.1.3", extras=["sklearn"])],
    )
    cmd = _build_install_command(spec)
    assert "xgboost[sklearn]==2.1.3" in cmd


def test_build_install_command_node(spec_node: RuntimeSpec) -> None:
    cmd = _build_install_command(spec_node)
    assert cmd.startswith("npm install")
    assert "express@4.18.2" in cmd


def test_build_install_command_empty(spec_empty: RuntimeSpec) -> None:
    cmd = _build_install_command(spec_empty)
    assert cmd == ""


def test_build_lock_command_python() -> None:
    spec = RuntimeSpec(runtime_type="python_venv", image="python:3.12-slim")
    assert _build_lock_command(spec) == "pip freeze"


def test_build_lock_command_node() -> None:
    spec = RuntimeSpec(runtime_type="node_npm", image="node:20-slim")
    assert _build_lock_command(spec) == "npm list --json --depth=0"


def test_build_smoke_test_command_python(spec_python: RuntimeSpec) -> None:
    cmd = _build_smoke_test_command(spec_python)
    assert "from importlib.metadata import version" in cmd
    assert "xgboost" in cmd
    assert "scikit-learn" in cmd


def test_build_smoke_test_command_python_empty(spec_empty: RuntimeSpec) -> None:
    cmd = _build_smoke_test_command(spec_empty)
    assert cmd == "python --version"


def test_build_smoke_test_command_node(spec_node: RuntimeSpec) -> None:
    cmd = _build_smoke_test_command(spec_node)
    assert "node" in cmd
    assert "express" in cmd


def test_build_install_command_java(spec_java: RuntimeSpec) -> None:
    cmd = _build_install_command(spec_java)
    assert cmd == ""


def test_build_lock_command_java(spec_java: RuntimeSpec) -> None:
    assert _build_lock_command(spec_java) == "mvn --version"


def test_build_smoke_test_command_java(spec_java: RuntimeSpec) -> None:
    cmd = _build_smoke_test_command(spec_java)
    assert "mvn" in cmd
    assert "java" in cmd


# ---------------------------------------------------------------------------
# EnvironmentSession.host_to_container_path
# ---------------------------------------------------------------------------


def test_host_to_container_path(tmp_path: Path, session: EnvironmentSession) -> None:
    subdir = tmp_path / "executions" / "42" / "run"
    subdir.mkdir(parents=True)
    container_path = session.host_to_container_path(subdir)
    assert container_path == "/project/executions/42/run"


# ---------------------------------------------------------------------------
# DockerDriver.start_session
# ---------------------------------------------------------------------------


def test_start_session(
    mock_docker_client: MagicMock, tmp_path: Path, spec_python: RuntimeSpec
) -> None:
    mock_container = MagicMock()
    mock_container.id = "container-abc"
    mock_docker_client.containers.run.return_value = mock_container
    mock_docker_client.images.get.return_value = MagicMock()

    driver = DockerDriver()
    driver._client = mock_docker_client

    result = driver.start_session(project_id=1, project_root=tmp_path, spec=spec_python)

    assert result.container_id == "container-abc"
    assert result.project_id == 1
    assert result.runtime_type == "python_venv"
    mock_docker_client.containers.run.assert_called_once()
    call_kwargs = mock_docker_client.containers.run.call_args
    assert call_kwargs.kwargs["image"] == "python:3.12-slim"
    assert call_kwargs.kwargs["detach"] is True


def test_start_session_pulls_missing_image(
    mock_docker_client: MagicMock,
    tmp_path: Path,
    spec_python: RuntimeSpec,
) -> None:
    # First images.get raises (not local), second succeeds (after pull)
    mock_docker_client.images.get.side_effect = [
        Exception("ImageNotFound"),
        MagicMock(),
    ]
    mock_docker_client.api.pull.return_value = []  # successful pull, no error events
    mock_container = MagicMock()
    mock_container.id = "xyz"
    mock_docker_client.containers.run.return_value = mock_container

    driver = DockerDriver()
    driver._client = mock_docker_client

    driver.start_session(project_id=2, project_root=tmp_path, spec=spec_python)
    mock_docker_client.api.pull.assert_called_once_with(
        "python:3.12-slim", stream=True, decode=True
    )


def test_ensure_image_pull_event_error_raises_bootstrap_error(
    mock_docker_client: MagicMock,
) -> None:
    mock_docker_client.images.get.side_effect = Exception("ImageNotFound")
    mock_docker_client.api.pull.return_value = [{"error": "pull access denied"}]

    driver = DockerDriver()
    driver._client = mock_docker_client

    with pytest.raises(EnvironmentBootstrapError, match="pull access denied"):
        driver._ensure_image(mock_docker_client, "some:image")


def test_ensure_image_not_available_after_pull_raises_bootstrap_error(
    mock_docker_client: MagicMock,
) -> None:
    mock_docker_client.images.get.side_effect = Exception("ImageNotFound")
    mock_docker_client.api.pull.return_value = []  # no error events in stream

    driver = DockerDriver()
    driver._client = mock_docker_client

    with pytest.raises(EnvironmentBootstrapError, match="not available after pull"):
        driver._ensure_image(mock_docker_client, "some:image")


# ---------------------------------------------------------------------------
# DockerDriver.run_command
# ---------------------------------------------------------------------------


def test_run_command_success(
    mock_docker_client: MagicMock,
    tmp_path: Path,
    session: EnvironmentSession,
) -> None:
    mock_container = MagicMock()
    mock_container.exec_run.return_value = (0, (b"hello\n", b""))
    mock_docker_client.containers.get.return_value = mock_container

    driver = DockerDriver()
    driver._client = mock_docker_client

    result = driver.run_command(session, "echo hello", tmp_path)

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.succeeded


def test_run_command_failure(
    mock_docker_client: MagicMock,
    tmp_path: Path,
    session: EnvironmentSession,
) -> None:
    mock_container = MagicMock()
    mock_container.exec_run.return_value = (1, (b"", b"error msg"))
    mock_docker_client.containers.get.return_value = mock_container

    driver = DockerDriver()
    driver._client = mock_docker_client

    result = driver.run_command(session, "false", tmp_path)
    assert result.exit_code == 1
    assert not result.succeeded


# ---------------------------------------------------------------------------
# DockerDriver.install_packages
# ---------------------------------------------------------------------------


def test_install_packages_empty_spec_returns_success(
    mock_docker_client: MagicMock,
    spec_empty: RuntimeSpec,
    session: EnvironmentSession,
) -> None:
    driver = DockerDriver()
    driver._client = mock_docker_client

    result = driver.install_packages(session, spec_empty)
    assert result.succeeded
    assert "No packages" in result.stdout
    mock_docker_client.containers.get.assert_not_called()


def test_install_packages_calls_exec(
    mock_docker_client: MagicMock,
    spec_python: RuntimeSpec,
    session: EnvironmentSession,
) -> None:
    mock_container = MagicMock()
    mock_container.exec_run.return_value = (0, (b"Successfully installed\n", b""))
    mock_docker_client.containers.get.return_value = mock_container

    driver = DockerDriver()
    driver._client = mock_docker_client

    result = driver.install_packages(session, spec_python)
    assert result.succeeded


# ---------------------------------------------------------------------------
# DockerDriver.stop_session
# ---------------------------------------------------------------------------


def test_stop_session(
    mock_docker_client: MagicMock,
    session: EnvironmentSession,
) -> None:
    mock_container = MagicMock()
    mock_docker_client.containers.get.return_value = mock_container

    driver = DockerDriver()
    driver._client = mock_docker_client

    driver.stop_session(session)
    mock_container.stop.assert_called_once()
    mock_container.remove.assert_called_once()


def test_stop_session_container_already_removed(
    mock_docker_client: MagicMock,
    session: EnvironmentSession,
) -> None:
    mock_docker_client.containers.get.side_effect = Exception("NotFound")

    driver = DockerDriver()
    driver._client = mock_docker_client

    driver.stop_session(session)
