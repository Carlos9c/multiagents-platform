"""Node.js (npm / yarn) package strategy."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.environment.contracts import EnvironmentSession, PinnedDependency, RuntimeSpec
from app.services.environment.manager_contracts import PackageRequest
from app.services.environment.manager_strategies.base import (
    BasePackageStrategy,
    CmdFn,
    InstallResult,
)


class NodeStrategy(BasePackageStrategy):
    """Installs Node packages via npm.

    Yarn is supported via detect() but npm is used as the install tool
    since the Docker images bootstrapped by the existing system always
    have npm available.
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        return frozenset({"node_npm", "react_native"})

    def detect(self, project_root: Path) -> bool:
        return (project_root / "package.json").exists()

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
        if request.version:
            pkg_spec = f"{request.name}@{request.version}"
            result = self._npm_install(pkg_spec, run_cmd)
            if result.success:
                return result

            if version_constraint == "exact_only":
                return InstallResult(
                    success=False,
                    installed_version=None,
                    version_adjusted=False,
                    output=result.output,
                    error=(
                        f"Cannot install {pkg_spec}: {result.error}. "
                        "version_constraint='exact_only' — escalating to user."
                    ),
                )

            # preferred / any_compatible: try without version pin
            fallback = self._npm_install(request.name, run_cmd)
            if fallback.success:
                return InstallResult(
                    success=True,
                    installed_version=fallback.installed_version,
                    version_adjusted=True,
                    output=fallback.output,
                    adjustment_reason=(
                        f"Requested {request.version} failed; installed latest compatible version instead."
                    ),
                )
            return fallback

        return self._npm_install(request.name, run_cmd)

    def _npm_install(self, pkg_spec: str, run_cmd: CmdFn) -> InstallResult:
        exit_code, stdout, stderr = run_cmd(f"npm install --save {pkg_spec}")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=stderr or stdout,
            )
        pkg_name = pkg_spec.split("@")[0]
        installed = _parse_npm_installed_version(stdout or stderr, pkg_name)
        return InstallResult(
            success=True,
            installed_version=installed or "latest",
            version_adjusted=False,
            output=combined,
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
        return "node_npm"


def _parse_npm_installed_version(output: str, pkg_name: str) -> str | None:
    """Parse installed version from npm output."""
    # "added N package (foo@1.2.3)"
    m = re.search(
        rf"{re.escape(pkg_name)}@([^\s,)]+)",
        output,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return None
