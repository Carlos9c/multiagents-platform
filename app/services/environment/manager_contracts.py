"""
Contracts for the environment manager service.

These dataclasses are shared between:
- EnvironmentManager (core install service)
- EnvironmentManagerAgent (orchestrator subagent wrapper)
- _apply_environment_delta() in resumption_service (Aria conversational path)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class PackageRequest:
    """A request to install one package into the runtime environment."""

    name: str
    version: str | None = None  # None = any compatible version
    package_type: Literal["app", "system"] = "app"
    # app  = language-level package manager (pip, npm, maven, etc.)
    # system = OS-level package (apt-get)


@dataclass
class PackageInstallation:
    """Record of what was actually installed for one package request."""

    name: str
    requested_version: str | None
    installed_version: str
    version_adjusted: bool
    adjustment_reason: str | None = None


@dataclass
class EnvironmentManagerRequest:
    """Input to the EnvironmentManager.install() call."""

    packages: list[PackageRequest]
    version_constraint: Literal["exact_only", "preferred", "any_compatible"]
    project_id: int
    workspace_path: str
    context_description: str = ""


@dataclass
class EnvironmentManagerOutput:
    """Output from the EnvironmentManager.install() call."""

    status: Literal[
        "success",
        "partial_success",
        "needs_user_input",  # exact_only version conflict — escalate to user
        "failed",  # unrecoverable error
        "rolled_back",  # install partially succeeded then failed; rolled back
    ]
    runtime_strategy: str  # e.g. "python_pip", "node_npm", "generic"
    installed_packages: list[PackageInstallation]
    manifest_changes: list[str]  # human-readable description of manifest updates
    observations: list[str]  # informational messages for the orchestrator
    blocker_message: str | None  # set when status ∈ {needs_user_input, failed, rolled_back}
    dependency_conflicts: list[str]  # package names or ranges that caused conflicts
    requires_rebuild: bool  # True for JVM/Rust/Go after adding deps
    updated_runtime_spec_json: str | None = None  # serialized RuntimeSpec after successful install
    # Paths (relative to workspace root) of manifest files modified during this install.
    # The EnvironmentManagerAgent uses these to register entries in evidence.changed_files
    # so the orchestrator and validators know which repo files changed.
    manifest_files_changed: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.manifest_files_changed is None:
            self.manifest_files_changed = []
