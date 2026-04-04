from __future__ import annotations

import filecmp
import shutil
import subprocess
from pathlib import Path

from app.schemas.workspace import WorkspaceChangeSet
from app.services.project_storage import ProjectStorageService
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

    Storage model:
    - source_dir: canonical persisted project tree
    - workspace_dir: editable overlay for the current execution run
    - run_dir: ephemeral candidate tree used only for command execution / verification

    Important semantics:
    - workspace_dir is NOT hydrated with source contents
    - run_dir is materialized on demand from source + workspace overlay
    - run_dir must be treated as ephemeral and may be removed after verification
    - promotion applies the workspace overlay onto a staging copy of source_dir

    Current limitation:
    - overlay deletions are not yet modeled explicitly, so deleted_files is always empty
      under the new overlay-only workspace model unless deletion intent is introduced separately
    """

    def __init__(self, storage_service: ProjectStorageService | None = None):
        self.storage_service = storage_service or ProjectStorageService()

    def ensure_project_storage(self, project_id: int) -> Path:
        return self.storage_service.ensure_project_storage(project_id).project_root

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
            run_dir=execution_root / "run",
            logs_dir=execution_root / "logs",
            outputs_dir=execution_root / "outputs",
        )

    def prepare_workspace(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> PreparedWorkspace:
        project_paths = self.storage_service.ensure_project_storage(project_id)
        run_paths = self.get_execution_workspace_paths(project_id, execution_run_id)

        run_paths.execution_root.mkdir(parents=True, exist_ok=True)
        run_paths.logs_dir.mkdir(parents=True, exist_ok=True)
        run_paths.outputs_dir.mkdir(parents=True, exist_ok=True)

        if run_paths.workspace_dir.exists():
            shutil.rmtree(run_paths.workspace_dir)
        run_paths.workspace_dir.mkdir(parents=True, exist_ok=True)

        if run_paths.run_dir.exists():
            shutil.rmtree(run_paths.run_dir)

        return PreparedWorkspace(
            project_id=project_id,
            execution_run_id=execution_run_id,
            workspace_dir=run_paths.workspace_dir,
            run_dir=run_paths.run_dir,
            source_dir=project_paths.source_dir,
        )

    def materialize_run_tree(
        self,
        project_id: int,
        execution_run_id: int,
        overlay_paths: list[str] | None = None,
    ) -> Path:
        prepared = self._get_prepared_workspace(project_id, execution_run_id)
        run_dir = prepared.run_dir.resolve()

        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)

        if prepared.source_dir.exists():
            self._copy_tree_contents(prepared.source_dir.resolve(), run_dir)

        self._apply_workspace_overlay_to_destination(
            workspace_dir=prepared.workspace_dir.resolve(),
            destination_dir=run_dir,
            overlay_paths=overlay_paths,
        )

        return run_dir

    def cleanup_run_tree(self, project_id: int, execution_run_id: int) -> None:
        run_paths = self.get_execution_workspace_paths(project_id, execution_run_id)
        if run_paths.run_dir.exists():
            shutil.rmtree(run_paths.run_dir)

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
                f"Cannot create file '{relative_path}' because it already exists in workspace."
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
    ) -> WorkspaceChangeSet:
        """
        Collect overlay changes authored in workspace_dir relative to source_dir.

        Under the overlay-only workspace model:
        - created_files: files present in workspace but absent in source
        - modified_files: files present in both with different content
        - deleted_files: not inferable from absence alone, because workspace is not a hydrated clone

        If true deletion support is required later, it should be represented explicitly in the run state
        or by a dedicated deletion manifest/tool.
        """
        prepared = self._get_prepared_workspace(project_id, execution_run_id)

        workspace_files = set(self.list_files(prepared.workspace_dir))
        source_files = (
            set(self.list_files(prepared.source_dir)) if prepared.source_dir.exists() else set()
        )

        created_files = sorted(workspace_files - source_files)

        modified_files: list[str] = []
        for relative_path in sorted(workspace_files & source_files):
            workspace_file = prepared.workspace_dir / relative_path
            source_file = prepared.source_dir / relative_path

            same = filecmp.cmp(workspace_file, source_file, shallow=False)
            if not same:
                modified_files.append(relative_path)

        deleted_files: list[str] = []

        impacted_areas = sorted(
            {
                Path(path).parts[0] if Path(path).parts else path
                for path in created_files + modified_files + deleted_files
            }
        )

        diff_summary = self.generate_diff(project_id, execution_run_id)

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
    ) -> str:
        """
        Generate diff between canonical source and the materialized candidate run tree.

        This reflects the actual candidate state (source + overlay), not the raw overlay directory.
        The ephemeral run tree is always cleaned up after diff generation.
        """
        prepared = self._get_prepared_workspace(project_id, execution_run_id)

        if not prepared.source_dir.exists():
            workspace_files = self.list_files(prepared.workspace_dir)
            if not workspace_files:
                return "No source baseline exists yet and workspace overlay is empty."
            return "No source baseline exists yet for this project source tree."

        run_dir = self.materialize_run_tree(
            project_id=project_id,
            execution_run_id=execution_run_id,
        )

        source_path = prepared.source_dir.resolve()

        command = [
            "git",
            "diff",
            "--no-index",
            "--",
            str(source_path),
            str(run_dir),
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
        finally:
            self.cleanup_run_tree(project_id, execution_run_id)

        if result.returncode == 0:
            return "No changes detected."
        if result.returncode == 1:
            return result.stdout or "Changes detected, but diff output is empty."

        stderr = result.stderr.strip()
        return f"Diff generation failed with exit code {result.returncode}: {stderr}"

    def promote_workspace_to_source(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> Path:
        """
        Promote by applying the workspace overlay onto a staging copy of source_dir.

        This is a false-promote to staging followed by an atomic-ish directory swap:
        - stage = copy(source)
        - overlay(workspace -> stage)
        - swap stage into source
        """
        prepared = self._get_prepared_workspace(project_id, execution_run_id)
        project_paths = self.storage_service.ensure_project_storage(project_id)

        workspace_dir = prepared.workspace_dir.resolve()
        source_dir = project_paths.source_dir.resolve()
        parent_dir = source_dir.parent

        if not workspace_dir.exists() or not workspace_dir.is_dir():
            raise WorkspaceRuntimeError(
                f"Workspace for execution_run_id={execution_run_id} does not exist."
            )

        parent_dir.mkdir(parents=True, exist_ok=True)

        staging_dir = parent_dir / f"{source_dir.name}.staging"
        backup_dir = parent_dir / f"{source_dir.name}.backup"

        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)

        try:
            staging_dir.mkdir(parents=True, exist_ok=False)

            if source_dir.exists():
                self._copy_tree_contents(source_dir, staging_dir)

            self._apply_workspace_overlay_to_destination(
                workspace_dir=workspace_dir,
                destination_dir=staging_dir,
                overlay_paths=None,
            )

            if source_dir.exists():
                source_dir.rename(backup_dir)

            staging_dir.rename(source_dir)

            if backup_dir.exists():
                shutil.rmtree(backup_dir)

            return source_dir

        except Exception as exc:
            try:
                if source_dir.exists() and backup_dir.exists():
                    shutil.rmtree(source_dir)
                    backup_dir.rename(source_dir)
                elif not source_dir.exists() and backup_dir.exists():
                    backup_dir.rename(source_dir)
            except Exception as rollback_exc:
                raise WorkspaceRuntimeError(
                    "Failed to promote workspace overlay to source and rollback also failed. "
                    f"Original error: {exc}. Rollback error: {rollback_exc}"
                ) from exc

            if staging_dir.exists():
                shutil.rmtree(staging_dir)

            raise WorkspaceRuntimeError(
                f"Failed to promote workspace overlay to source safely: {exc}"
            ) from exc

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
        if not command:
            raise WorkspaceRuntimeError("Command cannot be empty.")

        workspace = Path(workspace_dir).resolve()
        if not workspace.exists():
            raise WorkspaceRuntimeError("Workspace directory does not exist.")

        try:
            completed = subprocess.run(
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
            raise WorkspaceRuntimeError(f"Command executable not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                command=command,
                cwd=workspace,
                exit_code=124,
                stdout=stdout,
                stderr=stderr or f"Command timed out after {timeout_seconds} seconds.",
                timed_out=True,
            )

        valid_exit_codes = allowed_exit_codes or {0}
        if completed.returncode not in valid_exit_codes:
            raise WorkspaceRuntimeError(
                "Command failed with exit code "
                f"{completed.returncode}: {' '.join(command)}\n"
                f"CWD:\n{workspace}\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )

        return CommandResult(
            command=command,
            cwd=workspace,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
        )

    def _get_prepared_workspace(
        self,
        project_id: int,
        execution_run_id: int,
    ) -> PreparedWorkspace:
        run_paths = self.get_execution_workspace_paths(project_id, execution_run_id)
        project_paths = self.storage_service.ensure_project_storage(project_id)

        if not run_paths.workspace_dir.exists():
            raise WorkspaceRuntimeError(
                f"Workspace for execution_run_id={execution_run_id} does not exist."
            )

        return PreparedWorkspace(
            project_id=project_id,
            execution_run_id=execution_run_id,
            workspace_dir=run_paths.workspace_dir,
            run_dir=run_paths.run_dir,
            source_dir=project_paths.source_dir,
        )

    def _resolve_workspace_path(self, workspace_dir: str | Path, relative_path: str) -> Path:
        workspace = Path(workspace_dir).resolve()
        candidate = (workspace / relative_path).resolve()

        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise WorkspaceRuntimeError(
                f"Path '{relative_path}' escapes the workspace boundary."
            ) from exc

        return candidate

    def _copy_tree_contents(self, source_dir: Path, destination_dir: Path) -> None:
        if not source_dir.exists():
            return

        for source_path in source_dir.rglob("*"):
            relative_path = source_path.relative_to(source_dir)
            destination_path = destination_dir / relative_path

            if source_path.is_dir():
                destination_path.mkdir(parents=True, exist_ok=True)
                continue

            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)

    def _apply_workspace_overlay_to_destination(
        self,
        *,
        workspace_dir: Path,
        destination_dir: Path,
        overlay_paths: list[str] | None,
    ) -> None:
        if not workspace_dir.exists():
            return

        if overlay_paths is None:
            overlay_candidates = self.list_files(workspace_dir)
        else:
            overlay_candidates = sorted(dict.fromkeys(path for path in overlay_paths if path))

        for relative_path in overlay_candidates:
            source_path = self._resolve_workspace_path(workspace_dir, relative_path)
            if not source_path.exists():
                continue
            if not source_path.is_file():
                continue

            destination_path = self._resolve_workspace_path(destination_dir, relative_path)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
