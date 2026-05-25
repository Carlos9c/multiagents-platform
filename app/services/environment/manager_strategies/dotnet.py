"""
.NET (dotnet CLI) dependency strategy.

`dotnet add package <Name> [--version <V>]` updates the project's .csproj
(or .fsproj) file atomically and performs a NuGet restore.  Because the project
root is a read-write Docker volume mount, the .csproj change propagates back to
the host immediately.
"""

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


class DotnetStrategy(BasePackageStrategy):
    """Adds .NET NuGet packages via `dotnet add package`.

    Package name: NuGet package id, e.g. "Newtonsoft.Json".
    Version:      NuGet version string, e.g. "13.0.3".
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        return frozenset({"dotnet"})

    def detect(self, project_root: Path) -> bool:
        return bool(_find_project_file(project_root))

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
        # Locate the .csproj / .fsproj on the host so we can report it as changed.
        project_file_rel: str | None = None
        if workspace_path:
            host_proj = _find_project_file(workspace_path)
            if host_proj is None:
                return InstallResult(
                    success=False,
                    installed_version=None,
                    version_adjusted=False,
                    output="",
                    error=(
                        "No .csproj or .fsproj file found. "
                        "Ensure the .NET project is initialised before adding packages."
                    ),
                )
            project_file_rel = str(host_proj.relative_to(workspace_path))

        # Build the dotnet add package command.
        # Use the relative project path so the command works from /project in the container.
        proj_arg = f'"{project_file_rel}"' if project_file_rel else ""
        version_flag = f"--version {request.version}" if request.version else ""
        cmd = f"dotnet add {proj_arg} package {request.name} {version_flag}".strip()

        exit_code, stdout, stderr = run_cmd(f"{cmd} 2>&1")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            if request.version and version_constraint != "exact_only":
                # Try without pinned version
                cmd_latest = f"dotnet add {proj_arg} package {request.name}".strip()
                exit_code2, stdout2, stderr2 = run_cmd(f"{cmd_latest} 2>&1")
                combined2 = f"{stdout2}\n{stderr2}".strip()
                if exit_code2 == 0:
                    installed = _extract_dotnet_version(combined2, request.name)
                    changed = [project_file_rel] if project_file_rel else []
                    return InstallResult(
                        success=True,
                        installed_version=installed or "latest",
                        version_adjusted=True,
                        output=combined2,
                        adjustment_reason=(
                            f"Version {request.version} not available; installed latest instead."
                        ),
                        manifest_files_changed=changed,
                    )
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=f"dotnet add package {request.name} failed: {stderr or stdout}",
            )

        installed = _extract_dotnet_version(combined, request.name) or request.version or "latest"
        changed = [project_file_rel] if project_file_rel else []
        return InstallResult(
            success=True,
            installed_version=installed,
            version_adjusted=False,
            output=combined,
            manifest_files_changed=changed,
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
    def requires_rebuild(self) -> bool:
        # dotnet add package performs a restore automatically; no extra rebuild needed.
        return False

    @property
    def strategy_name(self) -> str:
        return "dotnet"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_project_file(project_root: Path) -> Path | None:
    """Return the first .csproj or .fsproj found in the project root or one level deep."""
    for ext in ("*.csproj", "*.fsproj"):
        matches = list(project_root.glob(ext))
        if matches:
            return matches[0]
    # One level deep (e.g. src/MyApp/MyApp.csproj)
    for subdir in sorted(project_root.iterdir()):
        if subdir.is_dir() and not subdir.name.startswith("."):
            for ext in ("*.csproj", "*.fsproj"):
                matches = list(subdir.glob(ext))
                if matches:
                    return matches[0]
    return None


def _extract_dotnet_version(output: str, package_name: str) -> str | None:
    """Parse the installed version from `dotnet add package` output.

    dotnet prints lines like:
      info : PackageReference for package 'Newtonsoft.Json' version '13.0.3' added to file '...'.
    """
    pattern = (
        rf"package\s+['\"]?{re.escape(package_name)}['\"]?\s+version\s+['\"]?([\d\w.-]+)['\"]?"
    )
    m = re.search(pattern, output, re.IGNORECASE)
    if m:
        return m.group(1)
    return None
