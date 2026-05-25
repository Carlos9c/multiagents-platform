"""Generic fallback strategy — tries pip install as a best-effort approach."""

from __future__ import annotations

from pathlib import Path

from app.services.environment.contracts import EnvironmentSession, PinnedDependency, RuntimeSpec
from app.services.environment.manager_contracts import PackageRequest
from app.services.environment.manager_strategies.base import (
    BasePackageStrategy,
    CmdFn,
    InstallResult,
)


class GenericStrategy(BasePackageStrategy):
    """Best-effort fallback: tries pip install.

    Used when the runtime type is not recognized or when no other strategy
    detects a match.  The install is attempted but the caller should treat
    results with lower confidence.
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        # No explicit runtime types — used only as a fallback via detect()
        return frozenset()

    def detect(self, project_root: Path) -> bool:  # noqa: ARG002
        # Always matches as a last resort
        return True

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
        pkg_spec = request.name
        if request.version:
            pkg_spec = f"{request.name}=={request.version}"

        exit_code, stdout, stderr = run_cmd(f"pip install --no-cache-dir {pkg_spec} 2>&1")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code == 0:
            return InstallResult(
                success=True,
                installed_version=request.version or "latest",
                version_adjusted=False,
                output=combined,
                adjustment_reason=(
                    "Installed via generic fallback strategy (runtime type not recognized)."
                ),
            )

        # Fallback: try without version
        if request.version and version_constraint != "exact_only":
            exit_code2, stdout2, stderr2 = run_cmd(
                f"pip install --no-cache-dir {request.name} 2>&1"
            )
            combined2 = f"{stdout2}\n{stderr2}".strip()
            if exit_code2 == 0:
                return InstallResult(
                    success=True,
                    installed_version="latest",
                    version_adjusted=True,
                    output=combined2,
                    adjustment_reason="Requested version failed; installed latest via generic fallback.",
                )

        return InstallResult(
            success=False,
            installed_version=None,
            version_adjusted=False,
            output=combined,
            error=stderr or stdout,
        )

    def update_manifest(
        self,
        *,
        spec: RuntimeSpec,
        name: str,
        installed_version: str,
    ) -> RuntimeSpec:
        deps = list(spec.dependencies)
        for i, dep in enumerate(deps):
            if dep.name.lower() == name.lower():
                deps[i] = PinnedDependency(name=dep.name, version=installed_version)
                return spec.model_copy(update={"dependencies": deps})
        deps.append(PinnedDependency(name=name, version=installed_version))
        return spec.model_copy(update={"dependencies": deps})

    @property
    def strategy_name(self) -> str:
        return "generic"
