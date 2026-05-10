from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.environment.contracts import (
    EnvironmentCommandResult,
    EnvironmentSession,
    EnvironmentValidationError,
    PinnedDependency,
    RuntimeSpec,
)
from app.services.environment.session_store import EnvironmentSessionStore
from app.services.environment.validator import EnvironmentValidator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def spec() -> RuntimeSpec:
    return RuntimeSpec(
        runtime_type="python_venv",
        image="python:3.12-slim",
        dependencies=[PinnedDependency(name="xgboost", version="2.1.3")],
    )


@pytest.fixture()
def session(tmp_path) -> EnvironmentSession:
    return EnvironmentSession(
        project_id=1,
        container_id="abc",
        project_root=tmp_path,
        runtime_type="python_venv",
    )


@pytest.fixture()
def store(session: EnvironmentSession) -> EnvironmentSessionStore:
    s = EnvironmentSessionStore()
    s.set_session(1, session)
    return s


@pytest.fixture()
def mock_driver() -> MagicMock:
    driver = MagicMock()
    driver.smoke_test_command.return_value = "python --version"
    return driver


@pytest.fixture()
def registry(mock_driver: MagicMock) -> MagicMock:
    r = MagicMock()
    r.get_driver.return_value = mock_driver
    return r


# ---------------------------------------------------------------------------
# Smoke test passes immediately
# ---------------------------------------------------------------------------


def test_validate_passes_first_attempt(
    spec: RuntimeSpec,
    store: EnvironmentSessionStore,
    registry: MagicMock,
    mock_driver: MagicMock,
) -> None:
    mock_driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=0, stdout="ok", stderr=""
    )

    validator = EnvironmentValidator(registry=registry, session_store=store)
    result = validator.validate(project_id=1, spec=spec)

    assert result is spec
    mock_driver.install_packages.assert_not_called()


# ---------------------------------------------------------------------------
# Smoke test fails → repair → passes
# ---------------------------------------------------------------------------


def test_validate_repair_succeeds(
    spec: RuntimeSpec,
    store: EnvironmentSessionStore,
    registry: MagicMock,
    mock_driver: MagicMock,
) -> None:
    mock_driver.run_command.side_effect = [
        EnvironmentCommandResult(exit_code=1, stdout="", stderr="ModuleNotFoundError"),
        EnvironmentCommandResult(exit_code=0, stdout="ok", stderr=""),
    ]
    mock_driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=0, stdout="installed", stderr=""
    )

    repaired_dep = MagicMock()
    repaired_dep.name = "xgboost"
    repaired_dep.version = "2.0.3"
    repaired_dep.extras = []

    repair_output = MagicMock()
    repair_output.corrected_image = "python:3.12-slim"
    repair_output.corrected_dependencies = [repaired_dep]
    repair_output.repair_rationale = "Downgraded xgboost to 2.0.3 for compatibility."

    with patch(
        "app.services.environment.validator.call_environment_repair",
        return_value=repair_output,
    ):
        validator = EnvironmentValidator(registry=registry, session_store=store)
        result = validator.validate(project_id=1, spec=spec)

    assert result is not spec
    assert result.change_log[-1].change_type == "repair"
    mock_driver.install_packages.assert_called_once()


# ---------------------------------------------------------------------------
# Smoke test fails → repair → fails again → raises
# ---------------------------------------------------------------------------


def test_validate_repair_fails_raises(
    spec: RuntimeSpec,
    store: EnvironmentSessionStore,
    registry: MagicMock,
    mock_driver: MagicMock,
) -> None:
    mock_driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="error"
    )
    mock_driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=0, stdout="", stderr=""
    )

    repair_output = MagicMock()
    repair_output.corrected_image = "python:3.12-slim"
    repair_output.corrected_dependencies = []
    repair_output.repair_rationale = "Removed all deps."

    with patch(
        "app.services.environment.validator.call_environment_repair",
        return_value=repair_output,
    ):
        validator = EnvironmentValidator(registry=registry, session_store=store)
        with pytest.raises(EnvironmentValidationError) as exc_info:
            validator.validate(project_id=1, spec=spec)

    err = exc_info.value
    assert "repair attempt" in str(err).lower() or "manual review" in str(err).lower()
    assert err.smoke_test_output != ""
    assert err.repair_attempt_output is not None


# ---------------------------------------------------------------------------
# Repair install failure raises immediately
# ---------------------------------------------------------------------------


def test_validate_repair_install_failure_raises(
    spec: RuntimeSpec,
    store: EnvironmentSessionStore,
    registry: MagicMock,
    mock_driver: MagicMock,
) -> None:
    mock_driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="ModuleNotFoundError"
    )
    mock_driver.install_packages.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="pip install failed"
    )

    repair_output = MagicMock()
    repair_output.corrected_image = "python:3.12-slim"
    repair_output.corrected_dependencies = []
    repair_output.repair_rationale = "Removed broken dep."

    with patch(
        "app.services.environment.validator.call_environment_repair",
        return_value=repair_output,
    ):
        validator = EnvironmentValidator(registry=registry, session_store=store)
        with pytest.raises(EnvironmentValidationError) as exc_info:
            validator.validate(project_id=1, spec=spec)

    err = exc_info.value
    # Must raise before the second smoke test (install error is the blocker)
    assert err.repair_attempt_output is not None
    assert "pip install failed" in err.repair_attempt_output
    # run_command called only once (first smoke test; install failed before retry)
    assert mock_driver.run_command.call_count == 1


# ---------------------------------------------------------------------------
# No session raises immediately
# ---------------------------------------------------------------------------


def test_validate_no_session_raises(spec: RuntimeSpec, registry: MagicMock) -> None:
    empty_store = EnvironmentSessionStore()
    validator = EnvironmentValidator(registry=registry, session_store=empty_store)
    with pytest.raises(EnvironmentValidationError) as exc_info:
        validator.validate(project_id=99, spec=spec)
    assert exc_info.value.smoke_test_output == ""


# ---------------------------------------------------------------------------
# LLM repair call failure raises
# ---------------------------------------------------------------------------


def test_validate_llm_repair_error_raises(
    spec: RuntimeSpec,
    store: EnvironmentSessionStore,
    registry: MagicMock,
    mock_driver: MagicMock,
) -> None:
    mock_driver.run_command.return_value = EnvironmentCommandResult(
        exit_code=1, stdout="", stderr="error"
    )

    with patch(
        "app.services.environment.validator.call_environment_repair",
        side_effect=RuntimeError("LLM unavailable"),
    ):
        validator = EnvironmentValidator(registry=registry, session_store=store)
        with pytest.raises(EnvironmentValidationError) as exc_info:
            validator.validate(project_id=1, spec=spec)

    assert "LLM unavailable" in (exc_info.value.repair_attempt_output or "")
