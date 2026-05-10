from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.services.environment.contracts import (
    EnvironmentCommandResult,
    EnvironmentSession,
    RuntimeSpec,
)


class BaseEnvironmentDriver(ABC):
    """Abstract interface for runtime environment drivers.

    Each concrete driver handles one runtime_type (python_venv, node_npm, etc.)
    and manages the lifecycle of an isolated execution container/environment.
    """

    @abstractmethod
    def start_session(
        self,
        project_id: int,
        project_root: Path,
        spec: RuntimeSpec,
    ) -> EnvironmentSession:
        """Start an isolated environment session for the project.

        Returns a session object that must be passed to subsequent driver calls.
        The caller is responsible for calling stop_session() when done.
        """

    @abstractmethod
    def run_command(
        self,
        session: EnvironmentSession,
        command: str,
        cwd: Path,
        timeout_seconds: int = 120,
    ) -> EnvironmentCommandResult:
        """Run a command inside the session at the given host-side working directory."""

    @abstractmethod
    def install_packages(
        self,
        session: EnvironmentSession,
        spec: RuntimeSpec,
    ) -> EnvironmentCommandResult:
        """Install all packages declared in the spec."""

    @abstractmethod
    def generate_lock_file(
        self,
        session: EnvironmentSession,
        spec: RuntimeSpec,
    ) -> str:
        """Return the content of a lock file reflecting what is currently installed."""

    @abstractmethod
    def smoke_test_command(self, spec: RuntimeSpec) -> str:
        """Return the command string that verifies the environment is healthy."""

    @abstractmethod
    def stop_session(self, session: EnvironmentSession) -> None:
        """Stop and clean up the session, releasing all resources."""
