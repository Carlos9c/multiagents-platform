from __future__ import annotations

from app.services.environment.base_driver import BaseEnvironmentDriver
from app.services.environment.contracts import RuntimeType


class DriverRegistry:
    """Maps runtime_type strings to their driver instances."""

    def __init__(self) -> None:
        self._drivers: dict[str, BaseEnvironmentDriver] = {}

    def register(self, runtime_type: RuntimeType, driver: BaseEnvironmentDriver) -> None:
        self._drivers[runtime_type] = driver

    def get_driver(self, runtime_type: str) -> BaseEnvironmentDriver:
        driver = self._drivers.get(runtime_type)
        if driver is None:
            supported = list(self._drivers.keys())
            raise ValueError(
                f"No driver registered for runtime_type={runtime_type!r}. "
                f"Supported types: {supported}"
            )
        return driver

    def supported_types(self) -> list[str]:
        return list(self._drivers.keys())


def build_default_registry() -> DriverRegistry:
    from app.services.environment.docker_driver import DockerDriver

    registry = DriverRegistry()
    registry.register("python_venv", DockerDriver())
    registry.register("node_npm", DockerDriver())
    registry.register("rust_cargo", DockerDriver())
    registry.register("java_maven", DockerDriver())
    return registry


_default_registry: DriverRegistry | None = None


def get_driver_registry() -> DriverRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry
