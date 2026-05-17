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


# ---------------------------------------------------------------------------
# bootstrap() — android_gradle gradle wrapper seeding
# ---------------------------------------------------------------------------


@pytest.fixture()
def android_spec() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="android_gradle",
        image="mingc/android-build-box:1.26.0",
        dependencies=[],
    )


@pytest.fixture()
def android_session(tmp_path: Path) -> EnvironmentSession:
    return EnvironmentSession(
        project_id=2,
        container_id="ctr-android",
        project_root=tmp_path,
        runtime_type="android_gradle",
    )


@pytest.fixture()
def android_driver(android_session: EnvironmentSession) -> MagicMock:
    driver = MagicMock()
    driver.start_session.return_value = android_session
    driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=0, stdout="", stderr=""
    )
    driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=0, stdout="Gradle wrapper seeded.", stderr=""
    )
    driver.generate_lock_file.return_value = "java -version: ok\n"
    return driver


def test_android_bootstrap_writes_wrapper_scripts_when_gradlew_missing(
    android_spec: RuntimeSpec,
    android_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    """When gradlew is absent, bootstrap writes the text wrapper files on the host
    and then extracts the JAR via run_command (no dependency on container Gradle)."""
    registry = DriverRegistry()
    registry.register("android_gradle", android_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=2, spec=android_spec)

    paths = storage.get_project_paths(2)

    # Text files must be written directly on the host
    gradlew_path = paths.source_dir / "gradlew"
    assert gradlew_path.exists()
    assert "GradleWrapperMain" in gradlew_path.read_text()
    # chmod is a no-op on Windows; just verify the file is readable/writable
    import sys
    if sys.platform != "win32":
        assert oct(gradlew_path.stat().st_mode & 0o777) == oct(0o755)

    props_path = paths.source_dir / "gradle" / "wrapper" / "gradle-wrapper.properties"
    assert props_path.exists()
    assert "gradle-8.7-bin.zip" in props_path.read_text()

    # JAR extraction still runs via container command
    android_driver.run_command.assert_called_once()
    cmd = android_driver.run_command.call_args.args[1]
    assert "gradle-wrapper.jar" in cmd
    assert "curl" in cmd


def test_android_bootstrap_seeds_jar_only_when_gradlew_present(
    android_spec: RuntimeSpec,
    android_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    """When gradlew exists but JAR is missing, bootstrap falls straight to JAR extraction."""
    paths = storage.ensure_project_storage(2)
    gradlew_path = paths.source_dir / "gradlew"
    gradlew_path.write_text("#!/bin/sh\nexec gradle \"$@\"\n", encoding="utf-8")

    registry = DriverRegistry()
    registry.register("android_gradle", android_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=2, spec=android_spec)

    android_driver.run_command.assert_called_once()
    call_args = android_driver.run_command.call_args
    command_arg = call_args.args[1]
    assert "gradle/wrapper/gradle-wrapper.jar" in command_arg
    assert "curl" in command_arg
    assert "unzip" in command_arg


def test_android_bootstrap_raises_when_gradle_wrapper_fails(
    android_spec: RuntimeSpec,
    android_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    android_driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="curl: (6) Could not resolve host"
    )
    registry = DriverRegistry()
    registry.register("android_gradle", android_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    with pytest.raises(EnvironmentBootstrapError) as exc_info:
        bootstrapper.bootstrap(project_id=2, spec=android_spec)

    assert "gradle wrapper" in str(exc_info.value).lower()


def test_android_bootstrap_cleans_up_when_gradle_wrapper_fails(
    android_spec: RuntimeSpec,
    android_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    android_driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="error"
    )
    registry = DriverRegistry()
    registry.register("android_gradle", android_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    with pytest.raises(EnvironmentBootstrapError):
        bootstrapper.bootstrap(project_id=2, spec=android_spec)

    assert store.get_session(2) is None
    android_driver.stop_session.assert_called_once()


def test_android_bootstrap_skips_seeding_when_wrapper_already_complete(
    android_spec: RuntimeSpec,
    android_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
    tmp_path: Path,
) -> None:
    """Seeding is skipped only when both gradlew and the JAR are already present."""
    paths = storage.ensure_project_storage(2)
    # Pre-seed gradlew script
    gradlew_path = paths.source_dir / "gradlew"
    gradlew_path.write_text("#!/bin/sh\nexec gradle \"$@\"\n", encoding="utf-8")
    # Pre-seed JAR (must be >10KB to pass the size check)
    jar_path = paths.source_dir / "gradle" / "wrapper" / "gradle-wrapper.jar"
    jar_path.parent.mkdir(parents=True, exist_ok=True)
    jar_path.write_bytes(b"PK" + b"\x00" * 20_000)  # fake 20KB ZIP-like file

    registry = DriverRegistry()
    registry.register("android_gradle", android_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=2, spec=android_spec)

    # run_command should NOT be called because both files already exist
    android_driver.run_command.assert_not_called()


def test_non_android_bootstrap_does_not_seed_gradle_wrapper(
    spec: RuntimeSpec,
    mock_driver: MagicMock,
    store: EnvironmentSessionStore,
    storage,
) -> None:
    registry = DriverRegistry()
    registry.register("python_venv", mock_driver)

    bootstrapper = EnvironmentBootstrapper(
        registry=registry, session_store=store, storage_service=storage
    )
    bootstrapper.bootstrap(project_id=1, spec=spec)

    mock_driver.run_command.assert_not_called()
