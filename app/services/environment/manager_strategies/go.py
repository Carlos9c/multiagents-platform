"""Go module dependency strategy.

`go get <module>@<version>` updates go.mod and go.sum atomically inside the
container.  Because the project root is mounted as a read-write Docker volume,
those changes propagate back to the host filesystem immediately — no separate
file-write step is needed.

After `go get` the strategy runs `go mod tidy` to prune unused entries and
ensure go.sum is complete.
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


class GoStrategy(BasePackageStrategy):
    """Adds Go module dependencies via `go get`.

    Package name format: full module path, e.g. "github.com/gin-gonic/gin".
    Version format: semver with 'v' prefix, e.g. "v1.9.0" or "1.9.0" (normalised).
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        return frozenset({"go"})

    def detect(self, project_root: Path) -> bool:
        return (project_root / "go.mod").exists()

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
        if workspace_path and not (workspace_path / "go.mod").exists():
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output="",
                error=(
                    "go.mod not found. Run 'go mod init <module>' to initialise "
                    "the Go module before adding dependencies."
                ),
            )

        pkg_spec = _build_go_pkg_spec(request.name, request.version)

        # go get downloads and records the dependency in go.mod + go.sum
        exit_code, stdout, stderr = run_cmd(f"go get {pkg_spec} 2>&1")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            if request.version and version_constraint != "exact_only":
                # Try latest version
                exit_code2, stdout2, stderr2 = run_cmd(f"go get {request.name}@latest 2>&1")
                combined2 = f"{stdout2}\n{stderr2}".strip()
                if exit_code2 == 0:
                    tidy_code, _, _ = run_cmd("go mod tidy 2>&1")
                    return InstallResult(
                        success=True,
                        installed_version="latest",
                        version_adjusted=True,
                        output=combined2,
                        adjustment_reason=(
                            f"Requested version {request.version} failed; installed latest instead."
                        ),
                        manifest_files_changed=["go.mod", "go.sum"],
                    )
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=f"go get {pkg_spec} failed: {stderr or stdout}",
            )

        # Tidy after successful get (best-effort — non-fatal if it fails)
        run_cmd("go mod tidy 2>&1")

        installed_version = (
            _extract_go_version(stdout + stderr, request.name) or request.version or "latest"
        )
        return InstallResult(
            success=True,
            installed_version=installed_version,
            version_adjusted=False,
            output=combined,
            manifest_files_changed=["go.mod", "go.sum"],
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
        # go get downloads sources; the actual compile happens on `go build`/`go test`
        return False

    @property
    def strategy_name(self) -> str:
        return "go"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_go_pkg_spec(name: str, version: str | None) -> str:
    """Build the argument for `go get` from package name and optional version.

    Normalises version strings: adds 'v' prefix if missing ("1.9.0" → "v1.9.0").
    Returns plain name when no version is specified (downloads latest).
    """
    if not version:
        return f"{name}@latest"
    v = version if version.startswith("v") else f"v{version}"
    return f"{name}@{v}"


def _extract_go_version(output: str, module: str) -> str | None:
    """Parse the installed version from `go get` output.

    go get prints lines like:
      go: added github.com/gin-gonic/gin v1.9.0
      go: upgraded github.com/gin-gonic/gin v1.8.0 => v1.9.0
    """
    pattern = rf"(?:added|upgraded)\s+{re.escape(module)}\s+(?:\S+\s+=>\s+)?(v[\d\w.-]+)"
    m = re.search(pattern, output)
    if m:
        return m.group(1)
    return None
