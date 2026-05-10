from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.environment.registry import DriverRegistry, build_default_registry


@pytest.fixture()
def registry() -> DriverRegistry:
    return DriverRegistry()


@pytest.fixture()
def mock_driver() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# register / get_driver
# ---------------------------------------------------------------------------


def test_register_and_get_driver(registry: DriverRegistry, mock_driver: MagicMock) -> None:
    registry.register("python_venv", mock_driver)
    assert registry.get_driver("python_venv") is mock_driver


def test_get_unregistered_driver_raises(registry: DriverRegistry) -> None:
    with pytest.raises(ValueError, match="No driver registered"):
        registry.get_driver("rust_cargo")


def test_get_unknown_type_error_message_lists_supported(
    registry: DriverRegistry, mock_driver: MagicMock
) -> None:
    registry.register("python_venv", mock_driver)
    with pytest.raises(ValueError, match="python_venv"):
        registry.get_driver("unknown_type")


# ---------------------------------------------------------------------------
# supported_types
# ---------------------------------------------------------------------------


def test_supported_types_empty(registry: DriverRegistry) -> None:
    assert registry.supported_types() == []


def test_supported_types_after_registration(
    registry: DriverRegistry, mock_driver: MagicMock
) -> None:
    registry.register("python_venv", mock_driver)
    registry.register("node_npm", mock_driver)
    types = registry.supported_types()
    assert set(types) == {"python_venv", "node_npm"}


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def test_default_registry_has_all_runtime_types() -> None:
    r = build_default_registry()
    for runtime_type in ("python_venv", "node_npm", "rust_cargo"):
        driver = r.get_driver(runtime_type)
        assert driver is not None


def test_default_registry_all_types_share_docker_driver() -> None:
    from app.services.environment.docker_driver import DockerDriver

    r = build_default_registry()
    for runtime_type in ("python_venv", "node_npm", "rust_cargo"):
        assert isinstance(r.get_driver(runtime_type), DockerDriver)
