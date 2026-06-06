from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.execution_engine.contracts import FileDocumentationEntry
from app.services.workspace_manifest_service import (
    FileManifestEntry,
    WorkspaceManifestService,
)


@pytest.fixture()
def service() -> WorkspaceManifestService:
    return WorkspaceManifestService()


@pytest.fixture()
def meta_dir(tmp_path: Path) -> Path:
    d = tmp_path / "project_meta"
    d.mkdir()
    return d


def _make_entry(
    path: str,
    documentation: str,
    change_summary: str = "initial",
    agent: str = "code_change_agent",
    operation: str = "create",
) -> FileDocumentationEntry:
    return FileDocumentationEntry(
        path=path,
        documentation=documentation,
        change_summary=change_summary,
        agent=agent,
        operation=operation,
    )


class TestReadManifest:
    def test_returns_empty_dict_when_file_does_not_exist(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        result = service.read_manifest(meta_dir)
        assert result == {}

    def test_returns_empty_dict_on_corrupt_json(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        (meta_dir / "workspace_manifest.json").write_text("not json", encoding="utf-8")
        result = service.read_manifest(meta_dir)
        assert result == {}

    def test_reads_existing_manifest(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        payload = {
            "app/service.py": {
                "path": "app/service.py",
                "description": "Core service module",
                "agent": "code_change_agent",
                "task_id": 1,
                "last_modified": "2026-06-02T10:00:00+00:00",
                "historical_changes": [],
            }
        }
        (meta_dir / "workspace_manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        result = service.read_manifest(meta_dir)
        assert "app/service.py" in result
        assert isinstance(result["app/service.py"], FileManifestEntry)
        assert result["app/service.py"].description == "Core service module"


class TestUpsertEntries:
    def test_creates_new_entries(self, service: WorkspaceManifestService, meta_dir: Path) -> None:
        entries = [
            _make_entry("app/models.py", "SQLAlchemy data models for the project"),
            _make_entry("app/routes.py", "FastAPI route handlers", agent="code_change_agent"),
        ]
        service.upsert_entries(meta_dir, entries, task_id=10, run_id=100)

        manifest = service.read_manifest(meta_dir)
        assert "app/models.py" in manifest
        assert "app/routes.py" in manifest
        assert manifest["app/models.py"].description == "SQLAlchemy data models for the project"
        assert manifest["app/models.py"].task_id == 10
        assert len(manifest["app/models.py"].historical_changes) == 1
        assert manifest["app/models.py"].historical_changes[0].run_id == 100
        assert manifest["app/models.py"].historical_changes[0].operation == "create"

    def test_upsert_updates_description_and_appends_history(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        # First write
        service.upsert_entries(
            meta_dir,
            [_make_entry("app/service.py", "Initial service implementation", operation="create")],
            task_id=1,
            run_id=10,
        )
        # Second write (modification)
        service.upsert_entries(
            meta_dir,
            [
                _make_entry(
                    "app/service.py",
                    "Service module with refund support added",
                    change_summary="Added refund endpoint",
                    operation="modify",
                )
            ],
            task_id=2,
            run_id=20,
        )

        manifest = service.read_manifest(meta_dir)
        entry = manifest["app/service.py"]

        # Description should be updated to latest
        assert entry.description == "Service module with refund support added"
        assert entry.task_id == 2

        # History should have both changes
        assert len(entry.historical_changes) == 2
        assert entry.historical_changes[0].run_id == 10
        assert entry.historical_changes[0].operation == "create"
        assert entry.historical_changes[1].run_id == 20
        assert entry.historical_changes[1].operation == "modify"
        assert entry.historical_changes[1].summary == "Added refund endpoint"

    def test_skips_entries_with_empty_documentation(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        entries = [
            _make_entry("app/service.py", ""),  # empty — should be skipped
            _make_entry("app/models.py", "Data models"),
        ]
        service.upsert_entries(meta_dir, entries, task_id=1, run_id=1)

        manifest = service.read_manifest(meta_dir)
        assert "app/service.py" not in manifest
        assert "app/models.py" in manifest

    def test_no_op_on_empty_entries_list(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        service.upsert_entries(meta_dir, [], task_id=1, run_id=1)
        assert not (meta_dir / "workspace_manifest.json").exists()

    def test_creates_meta_dir_if_missing(
        self, service: WorkspaceManifestService, tmp_path: Path
    ) -> None:
        meta_dir = tmp_path / "nonexistent" / "project_meta"
        entries = [_make_entry("app/service.py", "Service module")]
        service.upsert_entries(meta_dir, entries, task_id=1, run_id=1)
        assert (meta_dir / "workspace_manifest.json").exists()


class TestGetPathDescriptions:
    def test_returns_empty_dict_when_no_manifest(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        result = service.get_path_descriptions(meta_dir)
        assert result == {}

    def test_returns_path_to_description_map(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        entries = [
            _make_entry("app/service.py", "Core service implementation"),
            _make_entry("app/models.py", "SQLAlchemy data models"),
        ]
        service.upsert_entries(meta_dir, entries, task_id=1, run_id=1)

        result = service.get_path_descriptions(meta_dir)
        assert result == {
            "app/service.py": "Core service implementation",
            "app/models.py": "SQLAlchemy data models",
        }

    def test_does_not_include_historical_changes(
        self, service: WorkspaceManifestService, meta_dir: Path
    ) -> None:
        entries = [_make_entry("app/service.py", "Service module")]
        service.upsert_entries(meta_dir, entries, task_id=1, run_id=1)

        result = service.get_path_descriptions(meta_dir)
        # Values are strings only, no nested dicts
        for value in result.values():
            assert isinstance(value, str)
