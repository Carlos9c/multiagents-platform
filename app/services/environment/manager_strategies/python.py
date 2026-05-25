"""Python (pip / poetry) package strategy."""

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


class PythonStrategy(BasePackageStrategy):
    """Installs Python packages via pip.

    Works for both pip-only and poetry-managed projects, since the Docker
    container always has pip available and the bootstrapper already installs
    requirements via pip.
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        return frozenset({"python_venv", "fullstack_py_node"})

    def detect(self, project_root: Path) -> bool:
        """Detectable by the presence of Python manifest files."""
        markers = [
            "requirements.txt",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "Pipfile",
        ]
        return any((project_root / m).exists() for m in markers)

    # ------------------------------------------------------------------
    # Core install logic
    # ------------------------------------------------------------------

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
        # --- Attempt 1: exact version (always tried first when specified) ---
        if request.version:
            pkg_spec = f"{request.name}=={request.version}"
            result = self._pip_install(pkg_spec, run_cmd)
            if result.success:
                return result

            # exact_only: do not try alternatives — surface conflict to user
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
            fallback = self._pip_install(request.name, run_cmd)
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
            return fallback  # both failed

        # --- No version requested: install latest ---
        return self._pip_install(request.name, run_cmd)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pip_install(self, pkg_spec: str, run_cmd: CmdFn) -> InstallResult:
        """Run pip install and parse the installed version."""
        exit_code, stdout, stderr = run_cmd(f"pip install --no-cache-dir {pkg_spec}")
        combined = f"{stdout}\n{stderr}".strip()

        if exit_code != 0:
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=stderr or stdout,
            )

        # Parse installed version from pip output, e.g.:
        # "Successfully installed requests-2.31.0"
        installed = _parse_pip_installed_version(stdout or stderr, pkg_spec)
        return InstallResult(
            success=True,
            installed_version=installed or "unknown",
            version_adjusted=False,
            output=combined,
        )

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def update_manifest(
        self,
        *,
        spec: RuntimeSpec,
        name: str,
        installed_version: str,
    ) -> RuntimeSpec:
        """Add or update a PinnedDependency entry in the spec."""
        deps = list(spec.dependencies)
        for i, dep in enumerate(deps):
            if dep.name.lower() == name.lower():
                deps[i] = PinnedDependency(name=dep.name, version=installed_version)
                return spec.model_copy(update={"dependencies": deps})
        deps.append(PinnedDependency(name=name, version=installed_version))
        return spec.model_copy(update={"dependencies": deps})

    @property
    def strategy_name(self) -> str:
        return "python_pip"


def _parse_pip_installed_version(output: str, pkg_spec: str) -> str | None:
    """Parse 'Successfully installed foo-1.2.3' from pip output."""
    # "Successfully installed <pkg>-<version> ..."
    pkg_name = pkg_spec.split("==")[0].split(">=")[0].split("~=")[0].strip()
    pattern = re.compile(
        rf"Successfully installed.*?{re.escape(pkg_name)}-([^\s]+)",
        re.IGNORECASE,
    )
    m = pattern.search(output)
    if m:
        return m.group(1)
    # fallback: parse from "Requirement already satisfied: foo==1.2 ..."
    sat = re.search(
        rf"Requirement already satisfied: {re.escape(pkg_name)}==([^\s,;]+)",
        output,
        re.IGNORECASE,
    )
    if sat:
        return sat.group(1)
    return None
