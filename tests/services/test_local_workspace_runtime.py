from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.workspace_runtime import WorkspaceRuntimeError


def test_promote_workspace_to_source_applies_overlay_over_source(tmp_path: Path):
    runtime = LocalWorkspaceRuntime()

    project_id = 1
    execution_run_id = 10

    project_root = runtime.ensure_project_storage(project_id)
    source_dir = project_root / "domain_data" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    (source_dir / "old.txt").write_text("old", encoding="utf-8")
    (source_dir / "shared.txt").write_text("baseline", encoding="utf-8")

    prepared = runtime.prepare_workspace(
        project_id=project_id,
        execution_run_id=execution_run_id,
    )

    # El workspace ya no se hidrata con source; solo contiene el overlay de la run.
    assert not (prepared.workspace_dir / "old.txt").exists()

    (prepared.workspace_dir / "new.txt").write_text("new", encoding="utf-8")
    (prepared.workspace_dir / "shared.txt").write_text("candidate", encoding="utf-8")

    promoted = runtime.promote_workspace_to_source(
        project_id=project_id,
        execution_run_id=execution_run_id,
    )

    assert promoted == source_dir
    assert (source_dir / "new.txt").read_text(encoding="utf-8") == "new"
    assert (source_dir / "shared.txt").read_text(encoding="utf-8") == "candidate"

    # Sin semántica explícita de borrado, lo no tocado en el overlay permanece en source.
    assert (source_dir / "old.txt").read_text(encoding="utf-8") == "old"


def test_promote_workspace_to_source_restores_backup_on_swap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = LocalWorkspaceRuntime()

    project_id = 2
    execution_run_id = 20

    project_root = runtime.ensure_project_storage(project_id)
    source_dir = project_root / "domain_data" / "source"

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
    )
    (prepared.workspace_dir / "candidate.txt").write_text("candidate", encoding="utf-8")

    original_rename = Path.rename

    def _failing_rename(self: Path, target: Path):
        if self.name == "source.staging" and target.name == "source":
            raise OSError("simulated swap failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _failing_rename)

    with pytest.raises(
        WorkspaceRuntimeError, match="Failed to promote workspace overlay to source"
    ):
        runtime.promote_workspace_to_source(
            project_id=project_id,
            execution_run_id=execution_run_id,
        )

    assert (source_dir / "baseline.txt").read_text(encoding="utf-8") == "baseline"
    assert not (source_dir / "candidate.txt").exists()


def test_promote_workspace_to_source_restores_previous_source_if_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = LocalWorkspaceRuntime()

    project_id = 3
    execution_run_id = 30

    project_root = runtime.ensure_project_storage(project_id)
    source_dir = project_root / "domain_data" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "baseline.txt").write_text("baseline", encoding="utf-8")

    prepared = runtime.prepare_workspace(
        project_id=project_id,
        execution_run_id=execution_run_id,
    )
    (prepared.workspace_dir / "candidate.txt").write_text("candidate", encoding="utf-8")

    original_rename = Path.rename

    def _failing_rename(self: Path, target: Path):
        if self.name.endswith(".staging") and target.name == "source":
            raise OSError("simulated swap failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _failing_rename)

    with pytest.raises(
        WorkspaceRuntimeError, match="Failed to promote workspace overlay to source"
    ):
        runtime.promote_workspace_to_source(
            project_id=project_id,
            execution_run_id=execution_run_id,
        )

    assert (source_dir / "baseline.txt").read_text(encoding="utf-8") == "baseline"
    assert not (source_dir / "candidate.txt").exists()
