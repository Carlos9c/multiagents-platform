"""Package manager strategy implementations for the environment manager."""

from __future__ import annotations

from app.services.environment.manager_strategies.base import BasePackageStrategy, InstallResult
from app.services.environment.manager_strategies.generic import GenericStrategy
from app.services.environment.manager_strategies.jvm import JvmStrategy
from app.services.environment.manager_strategies.node import NodeStrategy
from app.services.environment.manager_strategies.python import PythonStrategy

__all__ = [
    "BasePackageStrategy",
    "InstallResult",
    "PythonStrategy",
    "NodeStrategy",
    "JvmStrategy",
    "GenericStrategy",
]
