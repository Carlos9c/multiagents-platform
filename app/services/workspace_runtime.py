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
    logs_dir: Path
    outputs_dir: Path


@dataclass(frozen=True)
class PreparedWorkspace:
    project_id: int
    execution_run_id: int
    domain_name: str
    workspace_dir: Path
    source_dir: Path | None


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str


class BaseWorkspaceRuntime(ABC):
    """
    Runtime abstraction for isolated execution workspaces.
    """

    @abstractmethod
    def ensure_project_storage(self, project_id: int) -> Path:
        raise NotImplementedError

    @abstractmethod
    def ensure_domain_storage(self, project_id: int, domain_name: str) -> Path:
        raise NotImplementedError

    @abstractmethod
    def prepare_workspace(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> PreparedWorkspace:
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
        domain_name: str,
    ) -> WorkspaceChangeSet:
        raise NotImplementedError

    @abstractmethod
    def generate_diff(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def promote_workspace_to_source(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> Path:
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
        raise NotImplementedError