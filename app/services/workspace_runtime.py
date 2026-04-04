from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.schemas.workspace import WorkspaceChangeSet


class WorkspaceRuntimeError(Exception):
    """Base exception for workspace runtime operations."""


@dataclass(frozen=True)
class ExecutionWorkspacePaths:
    project_id: int
    execution_run_id: int
    execution_root: Path
    workspace_dir: Path
    run_dir: Path
    logs_dir: Path
    outputs_dir: Path


@dataclass(frozen=True)
class PreparedWorkspace:
    project_id: int
    execution_run_id: int
    workspace_dir: Path
    run_dir: Path
    source_dir: Path


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    cwd: Path
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class BaseWorkspaceRuntime(ABC):
    """
    Runtime abstraction for isolated execution workspaces.

    Storage model:
    - source_dir: canonical persisted project tree
    - workspace_dir: editable overlay for the current execution run
    - run_dir: ephemeral candidate tree used only for command execution / verification
    """

    @abstractmethod
    def ensure_project_storage(self, project_id: int) -> Path:
        raise NotImplementedError

    @abstractmethod
    def prepare_workspace(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> PreparedWorkspace:
        """
        Prepare an empty editable workspace for the execution run.

        Important:
        - This must NOT hydrate workspace_dir with the persisted source tree.
        - workspace_dir is an overlay/editing area, not the executable candidate tree.
        """
        raise NotImplementedError

    @abstractmethod
    def materialize_run_tree(
        self,
        project_id: int,
        execution_run_id: int,
        overlay_paths: list[str] | None = None,
    ) -> Path:
        """
        Create the ephemeral candidate run tree by:

        1. copying the canonical source tree into run_dir
        2. overlaying the current execution-run materialized files from workspace_dir

        overlay_paths:
        - when provided, only those relative paths from workspace_dir are overlaid
        - when omitted, the implementation may overlay the full workspace contents
          as a transitional fallback, but the preferred behavior is explicit overlay

        The returned path is the run_dir root.
        """
        raise NotImplementedError

    @abstractmethod
    def cleanup_run_tree(self, project_id: int, execution_run_id: int) -> None:
        """
        Remove the ephemeral run_dir used for verification.
        This must be safe to call whether or not run_dir exists.
        """
        raise NotImplementedError

    @abstractmethod
    def get_execution_workspace_paths(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> ExecutionWorkspacePaths:
        raise NotImplementedError

    @abstractmethod
    def read_file(self, workspace_dir: str | Path, relative_path: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def write_file(
        self,
        workspace_dir: str | Path,
        relative_path: str,
        content: str,
    ) -> Path:
        raise NotImplementedError

    @abstractmethod
    def create_file(
        self,
        workspace_dir: str | Path,
        relative_path: str,
        content: str,
    ) -> Path:
        raise NotImplementedError

    @abstractmethod
    def file_exists(self, workspace_dir: str | Path, relative_path: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_files(self, workspace_dir: str | Path) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def collect_changes(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> WorkspaceChangeSet:
        """
        Collect the overlay changes authored in workspace_dir relative to the canonical source_dir.

        This should describe what the current execution run changed, not the contents
        of the ephemeral run tree.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_diff(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def promote_workspace_to_source(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> Path:
        """
        Apply the execution-run overlay into the canonical source tree.

        This must promote the authored result of workspace_dir, not a previously
        materialized run_dir.
        """
        raise NotImplementedError

    @abstractmethod
    def cleanup_workspace(self, project_id: int, execution_run_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def run_command(
        self,
        workspace_dir: str | Path,
        command: list[str],
        timeout_seconds: int = 120,
        allowed_exit_codes: set[int] | None = None,
    ) -> CommandResult:
        """
        Execute a command in the provided directory.

        In the intended design, callers should pass the ephemeral run_dir here,
        not workspace_dir and never the canonical source_dir.
        """
        raise NotImplementedError
