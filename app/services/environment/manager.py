"""
Environment Manager — incremental package installation service.

Installs individual packages into a running environment session without
full teardown/rebuild.  Used by:

1. EnvironmentManagerAgent (orchestrator subagent path): triggered when
   error_diagnosis.fault_side == "environment" during a task execution run.

2. _apply_environment_delta() in resumption_service: triggered when
   ImpactAssessmentAgent returns environment_changes after a review episode.

Design contract:
- The manager describes what it did and returns structured output.
- It does NOT prescribe routing or orchestration decisions.
- On version conflicts it follows the version_constraint policy from the caller.
- On success it updates the RuntimeSpec in memory and returns updated JSON.
- The caller is responsible for persisting the updated RuntimeSpec to the DB.

Runtime detection order:
1. spec.runtime_type (definitive when a RuntimeSpec is available)
2. Strategy.detect(project_root) file-based heuristic
3. GenericStrategy fallback
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.environment.contracts import RuntimeSpec
from app.services.environment.manager_contracts import (
    EnvironmentManagerOutput,
    EnvironmentManagerRequest,
    PackageInstallation,
    PackageRequest,
)
from app.services.environment.manager_strategies.base import (
    BasePackageStrategy,
    CmdFn,
    InstallResult,
)
from app.services.environment.manager_strategies.dotnet import DotnetStrategy
from app.services.environment.manager_strategies.generic import GenericStrategy
from app.services.environment.manager_strategies.go import GoStrategy
from app.services.environment.manager_strategies.jvm import JvmStrategy
from app.services.environment.manager_strategies.node import NodeStrategy
from app.services.environment.manager_strategies.python import PythonStrategy
from app.services.environment.manager_strategies.rust import RustStrategy
from app.services.environment.session_store import get_session_store

logger = logging.getLogger(__name__)

# Ordered list of concrete strategies — tried in order for file-based detection.
# More specific strategies (Go, Rust, .NET, JVM) are placed before the generic
# fallback. Python and Node come last among the explicit strategies because their
# detection markers (requirements.txt, package.json) are often present alongside
# other ecosystems in fullstack projects.
_STRATEGIES: list[BasePackageStrategy] = [
    GoStrategy(),
    RustStrategy(),
    DotnetStrategy(),
    JvmStrategy(),
    PythonStrategy(),
    NodeStrategy(),
    GenericStrategy(),
]


def _select_strategy(
    spec: RuntimeSpec | None,
    project_root: Path,
) -> BasePackageStrategy:
    """Select the appropriate strategy for the current runtime."""
    if spec is not None:
        for strategy in _STRATEGIES:
            if spec.runtime_type in strategy.runtime_types:
                return strategy

    # Fallback: file-based detection (skips GenericStrategy initially)
    for strategy in _STRATEGIES[:-1]:
        if strategy.detect(project_root):
            return strategy

    return GenericStrategy()


def _build_run_cmd(
    *,
    project_id: int,
    project_root: Path,
) -> CmdFn:
    """Return a command-execution function for the active container session.

    Falls back to subprocess execution when no Docker session is active (e.g.
    during local/test runs without a Docker daemon).
    """
    session = get_session_store().get_session(project_id)

    if session is not None:
        # Use the running Docker container
        try:
            from app.services.environment.docker_driver import DockerDriver

            driver = DockerDriver()

            def docker_run(command: str) -> tuple[int, str, str]:
                result = driver.run_command(session, command, project_root)
                return result.exit_code, result.stdout, result.stderr

            return docker_run
        except Exception as exc:
            # Docker IS active (session exists) but the driver failed — do NOT fall
            # through to subprocess here; that would silently install on the host.
            logger.error(
                "env_manager_docker_driver_error project_id=%s error=%s "
                "— refusing to fall back to subprocess because a Docker session exists.",
                project_id,
                str(exc),
            )
            raise RuntimeError(
                f"Docker driver unavailable for project_id={project_id} even though a "
                f"session exists. Cannot install packages safely: {exc}"
            ) from exc

    # No active Docker session.
    # In production all tasks run inside a Docker container, so reaching this branch
    # usually means the environment was not started or the session was evicted.
    # We fall back to subprocess only to support local development and integration tests
    # that run without Docker.  Any install here modifies the HOST machine, not a
    # container — this is intentional for local/test contexts and dangerous in production.
    logger.warning(
        "env_manager_subprocess_fallback project_id=%s "
        "WARNING: no active Docker session — package install will run on the HOST machine. "
        "This is safe for local/test runs but dangerous in production.",
        project_id,
    )

    import subprocess

    def local_run(command: str) -> tuple[int, str, str]:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=120,
        )
        return result.returncode, result.stdout, result.stderr

    return local_run


class EnvironmentManager:
    """Installs packages incrementally into the active runtime environment.

    Each call to install() handles one EnvironmentManagerRequest: it selects
    the runtime strategy, installs packages one by one with version-conflict
    handling, updates the RuntimeSpec manifest, and returns structured output.
    """

    def install(
        self,
        *,
        request: EnvironmentManagerRequest,
        spec: RuntimeSpec | None = None,
    ) -> EnvironmentManagerOutput:
        """
        Install the requested packages.

        spec: the current RuntimeSpec for the project, if available.
              When provided it takes priority for strategy selection and is
              updated in place; the updated spec is serialized and returned
              in EnvironmentManagerOutput.updated_runtime_spec_json.
        """
        workspace_root = Path(request.workspace_path)
        strategy = _select_strategy(spec, workspace_root)
        run_cmd = _build_run_cmd(
            project_id=request.project_id,
            project_root=workspace_root,
        )

        logger.info(
            "env_manager_started project_id=%s strategy=%s packages=%s constraint=%s",
            request.project_id,
            strategy.strategy_name,
            [p.name for p in request.packages],
            request.version_constraint,
        )

        # Snapshot the current dependencies for potential rollback
        spec_snapshot_json: str | None = spec.model_dump_json() if spec else None

        installed_packages: list[PackageInstallation] = []
        manifest_changes: list[str] = []
        manifest_files_changed: list[str] = []
        observations: list[str] = []
        conflicts: list[str] = []
        current_spec = spec

        for pkg_request in request.packages:
            result = self._install_one(
                strategy=strategy,
                spec=current_spec,
                pkg_request=pkg_request,
                version_constraint=request.version_constraint,
                run_cmd=run_cmd,
                workspace_path=workspace_root,
            )

            # Accumulate manifest file changes regardless of success/failure
            manifest_files_changed.extend(result.manifest_files_changed)

            if result.success:
                installation = PackageInstallation(
                    name=pkg_request.name,
                    requested_version=pkg_request.version,
                    installed_version=result.installed_version or "unknown",
                    version_adjusted=result.version_adjusted,
                    adjustment_reason=result.adjustment_reason,
                )
                installed_packages.append(installation)

                if result.version_adjusted and result.adjustment_reason:
                    observations.append(f"{pkg_request.name}: {result.adjustment_reason}")

                # Update manifest in memory
                if current_spec is not None:
                    current_spec = strategy.update_manifest(
                        spec=current_spec,
                        name=pkg_request.name,
                        installed_version=installation.installed_version,
                    )
                    manifest_changes.append(
                        f"Added/updated {pkg_request.name}=={installation.installed_version}"
                    )

            else:
                # Check if this is an escalation case (exact_only conflict)
                if (
                    request.version_constraint == "exact_only"
                    and pkg_request.version is not None
                    and "exact_only" in (result.error or "")
                ):
                    conflicts.append(
                        f"{pkg_request.name}=={pkg_request.version}: " f"{result.error}"
                    )
                    # For exact_only: rollback all successful installs and surface to user
                    if spec_snapshot_json and current_spec is not None:
                        try:
                            rolled_back_spec = RuntimeSpec.model_validate_json(spec_snapshot_json)
                        except Exception:
                            rolled_back_spec = spec
                        return EnvironmentManagerOutput(
                            status="needs_user_input",
                            runtime_strategy=strategy.strategy_name,
                            installed_packages=installed_packages,
                            manifest_changes=manifest_changes,
                            observations=observations,
                            blocker_message=(
                                f"Cannot install {pkg_request.name}=={pkg_request.version} "
                                f"with exact_only constraint: {result.error}. "
                                "User decision required: update the version or change the constraint."
                            ),
                            dependency_conflicts=conflicts,
                            requires_rebuild=strategy.requires_rebuild,
                            updated_runtime_spec_json=(
                                rolled_back_spec.model_dump_json() if rolled_back_spec else None
                            ),
                            manifest_files_changed=manifest_files_changed,
                        )

                conflicts.append(f"{pkg_request.name}: {result.error}")
                observations.append(f"Failed to install {pkg_request.name}: {result.error}")

        # Determine overall status
        total = len(request.packages)
        succeeded = len(installed_packages)

        if succeeded == 0 and total > 0:
            # Nothing installed — rollback (already nothing to roll back)
            return EnvironmentManagerOutput(
                status="failed",
                runtime_strategy=strategy.strategy_name,
                installed_packages=[],
                manifest_changes=[],
                observations=observations,
                blocker_message=f"All {total} package(s) failed to install: {'; '.join(conflicts)}",
                dependency_conflicts=conflicts,
                requires_rebuild=False,
                updated_runtime_spec_json=spec_snapshot_json,
                manifest_files_changed=manifest_files_changed,
            )

        status = "success" if succeeded == total else "partial_success"
        updated_spec_json = current_spec.model_dump_json() if current_spec else None

        logger.info(
            "env_manager_completed project_id=%s status=%s installed=%d/%d",
            request.project_id,
            status,
            succeeded,
            total,
        )

        return EnvironmentManagerOutput(
            status=status,
            runtime_strategy=strategy.strategy_name,
            installed_packages=installed_packages,
            manifest_changes=manifest_changes,
            observations=observations,
            blocker_message=None,
            dependency_conflicts=conflicts,
            requires_rebuild=strategy.requires_rebuild,
            updated_runtime_spec_json=updated_spec_json,
            manifest_files_changed=manifest_files_changed,
        )

    def _install_one(
        self,
        *,
        strategy: BasePackageStrategy,
        spec: RuntimeSpec | None,
        pkg_request: PackageRequest,
        version_constraint: str,
        run_cmd: CmdFn,
        workspace_path: Path,
    ) -> InstallResult:
        """Route to the right install method (app vs system)."""
        if pkg_request.package_type == "system":
            return strategy.install_system_package(
                request=pkg_request,
                run_cmd=run_cmd,
            )

        if spec is not None:
            return strategy.install_app_package(
                session=None,  # type: ignore[arg-type]
                spec=spec,
                request=pkg_request,
                version_constraint=version_constraint,
                run_cmd=run_cmd,
                workspace_path=workspace_path,
            )

        # No spec — use a minimal stub
        from app.services.environment.contracts import RuntimeSpec as _RS

        stub_spec = _RS(runtime_type="python_venv", image="")
        return strategy.install_app_package(
            session=None,  # type: ignore[arg-type]
            spec=stub_spec,
            request=pkg_request,
            version_constraint=version_constraint,
            run_cmd=run_cmd,
            workspace_path=workspace_path,
        )
