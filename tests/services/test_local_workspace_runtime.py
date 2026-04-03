from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.workspace_runtime import WorkspaceRuntimeError


def test_promote_workspace_to_source_replaces_source_atomicallyish(tmp_path: Path):
    runtime = LocalWorkspaceRuntime()

    project_id = 1
    execution_run_id = 10
    domain_name = "code"

    domain_root = runtime.ensure_domain_storage(project_id, domain_name)
    source_dir = domain_root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "old.txt").write_text("old", encoding="utf-8")

    prepared = runtime.prepare_workspace(
        project_id=project_id,
        execution_run_id=execution_run_id,
        domain_name=domain_name,
    )

    old_in_workspace = prepared.workspace_dir / "old.txt"
    assert old_in_workspace.exists()
    old_in_workspace.unlink()

    (prepared.workspace_dir / "new.txt").write_text("new", encoding="utf-8")

    promoted = runtime.promote_workspace_to_source(
        project_id=project_id,
        execution_run_id=execution_run_id,
        domain_name=domain_name,
    )

    assert promoted == source_dir
    assert (source_dir / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (source_dir / "old.txt").exists()

def test_promote_workspace_to_source_restores_backup_on_swap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = LocalWorkspaceRuntime()

    project_id = 2
    execution_run_id = 20
    domain_name = "code"

    domain_root = runtime.ensure_domain_storage(project_id, domain_name)
    source_dir = domain_root / "source"

    # Limpieza explícita para evitar contaminación entre tests
    if source_dir.exists():
        shutil.rmtree(source_dir)

    run_paths = runtime.get_execution_workspace_paths(project_id, execution_run_id)
    if run_paths.execution_root.exists():
        shutil.rmtree(run_paths.execution_root)

    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "baseline.txt").write_text("baseline", encoding="utf-8")

    prepared = runtime.prepare_workspace(
        project_id=project_id,
        execution_run_id=execution_run_id,
        domain_name=domain_name,
    )
    (prepared.workspace_dir / "candidate.txt").write_text("candidate", encoding="utf-8")

    original_rename = Path.rename

    def _failing_rename(self: Path, target: Path):
        if self.name == "source.staging" and target.name == "source":
            raise OSError("simulated swap failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _failing_rename)

    with pytest.raises(WorkspaceRuntimeError, match="Failed to promote workspace"):
        runtime.promote_workspace_to_source(
            project_id=project_id,
            execution_run_id=execution_run_id,
            domain_name=domain_name,
        )

    assert (source_dir / "baseline.txt").read_text(encoding="utf-8") == "baseline"
    assert not (source_dir / "candidate.txt").exists()


def test_promote_workspace_to_source_restores_previous_source_if_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = LocalWorkspaceRuntime()

    project_id = 1
    execution_run_id = 10
    domain_name = "code"

    domain_root = runtime.ensure_domain_storage(project_id, domain_name)
    source_dir = domain_root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "baseline.txt").write_text("baseline", encoding="utf-8")

    prepared = runtime.prepare_workspace(
        project_id=project_id,
        execution_run_id=execution_run_id,
        domain_name=domain_name,
    )
    (prepared.workspace_dir / "candidate.txt").write_text("candidate", encoding="utf-8")

    original_rename = Path.rename

    def _failing_rename(self: Path, target: Path):
        if self.name.endswith(".staging") and target.name == "source":
            raise OSError("simulated swap failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _failing_rename)

    with pytest.raises(WorkspaceRuntimeError, match="Failed to promote workspace"):
        runtime.promote_workspace_to_source(
            project_id=project_id,
            execution_run_id=execution_run_id,
            domain_name=domain_name,
        )

    assert (source_dir / "baseline.txt").read_text(encoding="utf-8") == "baseline"
    assert not (source_dir / "candidate.txt").exists()