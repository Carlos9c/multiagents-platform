from __future__ import annotations

import filecmp
import shutil
import subprocess
from pathlib import Path

from app.schemas.code_execution import WorkspaceChangeSet
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.workspace_runtime import (
    BaseWorkspaceRuntime,
    CommandResult,
    ExecutionWorkspacePaths,
    PreparedWorkspace,
    WorkspaceRuntimeError,
)


class LocalWorkspaceRuntime(BaseWorkspaceRuntime):
    """
    Local filesystem implementation of the workspace runtime.

    It supports:
    - universal project storage
    - domain storage
    - isolated workspaces per execution_run_id
    - safe file reads/writes inside workspace
    - diff/change collection against the domain source
    - optional local command execution inside the workspace
    """

    def __init__(self, storage_service: ProjectStorageService | None = None):
        self.storage_service = storage_service or ProjectStorageService()

    def ensure_project_storage(self, project_id: int) -> Path:
        return self.storage_service.ensure_project_storage(project_id).project_root

    def ensure_domain_storage(self, project_id: int, domain_name: str) -> Path:
        return self.storage_service.ensure_domain_storage(project_id, domain_name).domain_root

    def get_execution_workspace_paths(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> ExecutionWorkspacePaths:
        project_paths = self.storage_service.ensure_project_storage(project_id)
        execution_root = project_paths.executions_dir / str(execution_run_id)

        return ExecutionWorkspacePaths(
            project_id=project_id,
            execution_run_id=execution_run_id,
            execution_root=execution_root,
            workspace_dir=execution_root / "workspace",
            logs_dir=execution_root / "logs",
            outputs_dir=execution_root / "outputs",
        )

    def prepare_workspace(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> PreparedWorkspace:
        self.storage_service.ensure_project_storage(project_id)
        domain_paths = self.storage_service.ensure_domain_storage(project_id, domain_name)
        run_paths = self.get_execution_workspace_paths(project_id, execution_run_id)

        run_paths.execution_root.mkdir(parents=True, exist_ok=True)
        run_paths.logs_dir.mkdir(parents=True, exist_ok=True)
        run_paths.outputs_dir.mkdir(parents=True, exist_ok=True)

        if run_paths.workspace_dir.exists():
            shutil.rmtree(run_paths.workspace_dir)

        run_paths.workspace_dir.mkdir(parents=True, exist_ok=True)

        if domain_paths.source_dir and domain_paths.source_dir.exists():
            self._copy_tree_contents(domain_paths.source_dir, run_paths.workspace_dir)

        return PreparedWorkspace(
            project_id=project_id,
            execution_run_id=execution_run_id,
            domain_name=domain_name,
            workspace_dir=run_paths.workspace_dir,
            source_dir=domain_paths.source_dir,
        )

    def read_file(self, workspace_dir: str | Path, relative_path: str) -> str:
        resolved_path = self._resolve_workspace_path(workspace_dir, relative_path)
        if not resolved_path.exists():
            raise WorkspaceRuntimeError(f"File '{relative_path}' does not exist in workspace.")
        return resolved_path.read_text(encoding="utf-8")

    def write_file(
        self,
        workspace_dir: str | Path,
        relative_path: str,
        content: str,
    ) -> Path:
        resolved_path = self._resolve_workspace_path(workspace_dir, relative_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(content, encoding="utf-8")
        return resolved_path

    def create_file(
        self,
        workspace_dir: str | Path,
        relative_path: str,
        content: str,
    ) -> Path:
        resolved_path = self._resolve_workspace_path(workspace_dir, relative_path)
        if resolved_path.exists():
            raise WorkspaceRuntimeError(
                f"Cannot create file '{relative_path}' because it already exists."
            )
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(content, encoding="utf-8")
        return resolved_path

    def file_exists(self, workspace_dir: str | Path, relative_path: str) -> bool:
        resolved_path = self._resolve_workspace_path(workspace_dir, relative_path)
        return resolved_path.exists()

    def list_files(self, workspace_dir: str | Path) -> list[str]:
        workspace = Path(workspace_dir).resolve()
        if not workspace.exists():
            return []

        files: list[str] = []
        for path in workspace.rglob("*"):
            if path.is_file():
                files.append(path.relative_to(workspace).as_posix())
        return sorted(files)

    def collect_changes(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> WorkspaceChangeSet:
        prepared = self._get_prepared_workspace(project_id, execution_run_id, domain_name)

        workspace_files = set(self.list_files(prepared.workspace_dir))
        source_files = (
            set(self.list_files(prepared.source_dir))
            if prepared.source_dir and prepared.source_dir.exists()
            else set()
        )

        created_files = sorted(workspace_files - source_files)
        deleted_files = sorted(source_files - workspace_files)

        modified_files: list[str] = []
        for relative_path in sorted(workspace_files & source_files):
            workspace_file = prepared.workspace_dir / relative_path
            source_file = prepared.source_dir / relative_path  # type: ignore[arg-type]

            same = filecmp.cmp(workspace_file, source_file, shallow=False)
            if not same:
                modified_files.append(relative_path)

        impacted_areas = sorted(
            {
                Path(path).parts[0] if Path(path).parts else path
                for path in created_files + modified_files + deleted_files
            }
        )

        diff_summary = self.generate_diff(project_id, execution_run_id, domain_name)

        return WorkspaceChangeSet(
            created_files=created_files,
            modified_files=modified_files,
            deleted_files=deleted_files,
            renamed_files=[],
            diff_summary=diff_summary or None,
            impacted_areas=impacted_areas,
        )

    def generate_diff(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> str:
        prepared = self._get_prepared_workspace(project_id, execution_run_id, domain_name)

        if prepared.source_dir is None or not prepared.source_dir.exists():
            return "No source baseline exists yet for this domain."

        source_path = prepared.source_dir
        workspace_path = prepared.workspace_dir

        command = [
            "git",
            "diff",
            "--no-index",
            "--",
            str(source_path),
            str(workspace_path),
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except FileNotFoundError:
            return "git is not available on the local system to generate a diff."
        except subprocess.TimeoutExpired:
            return "Diff generation timed out."

        # git diff --no-index semantics:
        # 0 => no differences
        # 1 => differences found
        # >1 => actual failure
        if result.returncode == 0:
            return result.stdout.strip()

        if result.returncode == 1:
            return result.stdout.strip()

        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()

        if stderr:
            return (
                f"Diff generation failed with exit code {result.returncode}: {stderr}"
            )

        if stdout:
            return (
                f"Diff generation failed with exit code {result.returncode}: {stdout}"
            )

        return f"Diff generation failed with exit code {result.returncode}."

    def promote_workspace_to_source(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> Path:
        prepared = self._get_prepared_workspace(project_id, execution_run_id, domain_name)

        if prepared.source_dir is None:
            raise WorkspaceRuntimeError(
                f"Domain '{domain_name}' does not define a source directory to promote into."
            )

        if prepared.source_dir.exists():
            shutil.rmtree(prepared.source_dir)

        prepared.source_dir.mkdir(parents=True, exist_ok=True)
        self._copy_tree_contents(prepared.workspace_dir, prepared.source_dir)

        return prepared.source_dir

    def cleanup_workspace(self, project_id: int, execution_run_id: int) -> None:
        run_paths = self.get_execution_workspace_paths(project_id, execution_run_id)
        if run_paths.execution_root.exists():
            shutil.rmtree(run_paths.execution_root)

    def run_command(
        self,
        workspace_dir: str | Path,
        command: list[str],
        timeout_seconds: int = 120,
        allowed_exit_codes: set[int] | None = None,
    ) -> CommandResult:
        allowed_exit_codes = allowed_exit_codes or {0}
        workspace = Path(workspace_dir).resolve()

        if not workspace.exists():
            raise WorkspaceRuntimeError(
                f"Workspace '{workspace}' does not exist for command execution."
            )

        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorkspaceRuntimeError(
                f"Command executable was not found: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceRuntimeError(
                f"Command timed out after {timeout_seconds} seconds: {' '.join(command)}"
            ) from exc

        command_result = CommandResult(
            command=command,
            exit_code=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

        if result.returncode not in allowed_exit_codes:
            raise WorkspaceRuntimeError(
                "Command execution failed. "
                f"exit_code={result.returncode}, command={' '.join(command)}, "
                f"stderr={(result.stderr or '').strip()}"
            )

        return command_result

    def _get_prepared_workspace(
        self,
        project_id: int,
        execution_run_id: int,
        domain_name: str,
    ) -> PreparedWorkspace:
        domain_paths = self.storage_service.ensure_domain_storage(project_id, domain_name)
        run_paths = self.get_execution_workspace_paths(project_id, execution_run_id)

        if not run_paths.workspace_dir.exists():
            raise WorkspaceRuntimeError(
                f"Workspace for project={project_id}, run={execution_run_id} does not exist."
            )

        return PreparedWorkspace(
            project_id=project_id,
            execution_run_id=execution_run_id,
            domain_name=domain_name,
            workspace_dir=run_paths.workspace_dir,
            source_dir=domain_paths.source_dir,
        )

    def _resolve_workspace_path(
        self,
        workspace_dir: str | Path,
        relative_path: str,
    ) -> Path:
        workspace = Path(workspace_dir).resolve()

        if not workspace.exists():
            raise WorkspaceRuntimeError(f"Workspace '{workspace}' does not exist.")

        normalized_relative = Path(relative_path)
        if normalized_relative.is_absolute():
            raise WorkspaceRuntimeError(
                f"Absolute paths are not allowed inside workspace operations: '{relative_path}'."
            )

        resolved = (workspace / normalized_relative).resolve()

        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise WorkspaceRuntimeError(
                f"Path escapes workspace boundary: '{relative_path}'."
            ) from exc

        return resolved

    @staticmethod
    def _copy_tree_contents(source_dir: Path, destination_dir: Path) -> None:
        destination_dir.mkdir(parents=True, exist_ok=True)

        for item in source_dir.iterdir():
            destination_item = destination_dir / item.name
            if item.is_dir():
                shutil.copytree(item, destination_item, dirs_exist_ok=True)
            else:
                destination_item.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination_item)