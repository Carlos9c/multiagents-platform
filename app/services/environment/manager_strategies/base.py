"""Abstract base class for package install strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.services.environment.contracts import EnvironmentSession, RuntimeSpec
from app.services.environment.manager_contracts import PackageRequest

# A callable that runs one shell command inside the environment and returns
# (exit_code, stdout, stderr).  Provided by EnvironmentManager at call time.
CmdFn = Callable[[str], tuple[int, str, str]]


@dataclass
class InstallResult:
    """Outcome of installing a single package."""

    success: bool
    installed_version: str | None
    version_adjusted: bool  # True when a different version was installed than requested
    output: str  # combined stdout + stderr for logging
    error: str | None = None
    adjustment_reason: str | None = None
    # Paths (relative to workspace root) of manifest files modified by this install.
    # Populated by compiled-ecosystem strategies that edit build files directly
    # (pom.xml, build.gradle, Cargo.toml, go.mod, .csproj, pubspec.yaml).
    # Empty for pip/npm where the runtime manages the manifest.
    manifest_files_changed: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.manifest_files_changed is None:
            self.manifest_files_changed = []


class BasePackageStrategy(ABC):
    """Strategy for installing packages into a running environment session.

    Concrete strategies implement the runtime-specific install commands and
    manifest update logic.  The EnvironmentManager selects the right strategy
    based on the active RuntimeSpec.runtime_type.
    """

    @property
    @abstractmethod
    def runtime_types(self) -> frozenset[str]:
        """Set of RuntimeType values this strategy handles."""

    @abstractmethod
    def detect(self, project_root: Path) -> bool:
        """Return True when this strategy is applicable to the project tree.

        Used as a fallback when no RuntimeSpec is available.
        """

    @abstractmethod
    def install_app_package(
        self,
        *,
        session: EnvironmentSession,
        spec: RuntimeSpec,
        request: PackageRequest,
        version_constraint: str,
        run_cmd: CmdFn,
        workspace_path: Path | None = None,
    ) -> InstallResult:
        """Install one application-level package (pip, npm, cargo, go get, etc.).

        version_constraint: one of "exact_only" | "preferred" | "any_compatible"

        workspace_path: absolute path to the project root on the host filesystem.
            Required for compiled ecosystems (JVM, Rust, Go, .NET) that must edit
            manifest files (pom.xml, Cargo.toml, go.mod, .csproj) before or after
            running the package manager command.  May be None for runtime-only
            ecosystems (Python, Node) that don't need direct file access.
        """

    def install_system_package(
        self,
        *,
        request: PackageRequest,
        run_cmd: CmdFn,
    ) -> InstallResult:
        """Install one OS-level package (apt-get).  Default implementation."""
        pkg = request.name
        if request.version:
            pkg = f"{request.name}={request.version}"
        exit_code, stdout, stderr = run_cmd(
            f"apt-get install -y --no-install-recommends {pkg} 2>&1 || true && "
            f"dpkg -l {request.name} | tail -1"
        )
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code == 0:
            return InstallResult(
                success=True,
                installed_version=request.version or "latest",
                version_adjusted=False,
                output=combined,
            )
        return InstallResult(
            success=False,
            installed_version=None,
            version_adjusted=False,
            output=combined,
            error=stderr or stdout,
        )

    @abstractmethod
    def update_manifest(
        self,
        *,
        spec: RuntimeSpec,
        name: str,
        installed_version: str,
    ) -> RuntimeSpec:
        """Return an updated RuntimeSpec with the installed package recorded."""

    @property
    def requires_rebuild(self) -> bool:
        """Return True for compiled ecosystems that need a rebuild after install."""
        return False

    @property
    def strategy_name(self) -> str:
        return self.__class__.__name__.lower().replace("strategy", "")
