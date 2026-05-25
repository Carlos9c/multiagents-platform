"""Rust (cargo) dependency strategy.

`cargo add <crate>@<version>` is a built-in cargo subcommand since Rust 1.62
(the agente-rust:stable image ships Rust stable ≥ 1.78).  It updates Cargo.toml
in-place, and because the project root is a read-write Docker volume mount,
that change propagates back to the host immediately.

After `cargo add`, the strategy runs `cargo fetch` to download the crate
sources into the local registry cache without doing a full compilation.
The actual build is left to command_runner_agent so it can surface any
compilation errors that need code fixes.
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


class RustStrategy(BasePackageStrategy):
    """Adds Rust crate dependencies via `cargo add`.

    Package name format: crate name from crates.io, e.g. "serde".
    Version format: semver, e.g. "1.0" or "1.0.196".
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        return frozenset({"rust_cargo"})

    def detect(self, project_root: Path) -> bool:
        return (project_root / "Cargo.toml").exists()

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
        if workspace_path and not (workspace_path / "Cargo.toml").exists():
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output="",
                error=(
                    "Cargo.toml not found. Run 'cargo init' or 'cargo new' to "
                    "initialise the Rust project before adding dependencies."
                ),
            )

        pkg_spec = _build_cargo_spec(request.name, request.version)

        # cargo add updates Cargo.toml atomically
        exit_code, stdout, stderr = run_cmd(f"cargo add {pkg_spec} 2>&1")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            if request.version and version_constraint != "exact_only":
                # Try without version constraint
                exit_code2, stdout2, stderr2 = run_cmd(f"cargo add {request.name} 2>&1")
                combined2 = f"{stdout2}\n{stderr2}".strip()
                if exit_code2 == 0:
                    run_cmd("cargo fetch 2>&1")
                    installed = _extract_cargo_version(combined2, request.name)
                    return InstallResult(
                        success=True,
                        installed_version=installed or "latest",
                        version_adjusted=True,
                        output=combined2,
                        adjustment_reason=(
                            f"Requested version {request.version} not available; "
                            "installed latest compatible version instead."
                        ),
                        manifest_files_changed=["Cargo.toml", "Cargo.lock"],
                    )
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=f"cargo add {pkg_spec} failed: {stderr or stdout}",
            )

        # Pre-fetch crate sources so the next build doesn't need network access
        run_cmd("cargo fetch 2>&1")

        installed = _extract_cargo_version(combined, request.name) or request.version or "latest"
        return InstallResult(
            success=True,
            installed_version=installed,
            version_adjusted=False,
            output=combined,
            manifest_files_changed=["Cargo.toml", "Cargo.lock"],
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
        # cargo fetch downloads sources; full compilation is done by command_runner_agent
        return False

    @property
    def strategy_name(self) -> str:
        return "rust_cargo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_cargo_spec(name: str, version: str | None) -> str:
    """Build the argument for `cargo add` from crate name and optional version.

    Examples:
      ("serde", "1.0")       → "serde@1.0"
      ("serde", None)        → "serde"
      ("tokio", "1.0.0")     → "tokio@1.0.0"
    """
    if version:
        return f"{name}@{version}"
    return name


def _extract_cargo_version(output: str, crate_name: str) -> str | None:
    """Parse the resolved version from `cargo add` output.

    cargo add prints lines like:
      Updating crates.io index
          Adding serde v1.0.196 to dependencies
    """
    pattern = rf"Adding\s+{re.escape(crate_name)}\s+v([\d\w.-]+)"
    m = re.search(pattern, output, re.IGNORECASE)
    if m:
        return m.group(1)
    return None
