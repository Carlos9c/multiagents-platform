from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

PROJECT_META_DIRNAME = "project_meta"
ARTIFACTS_DIRNAME = "artifacts"
EXECUTIONS_DIRNAME = "executions"
DOMAIN_DATA_DIRNAME = "domain_data"
SOURCE_DIRNAME = "source"


class ProjectStorageError(Exception):
    """Base exception for project storage management."""


@dataclass(frozen=True)
class ProjectStoragePaths:
    root: Path
    project_root: Path
    project_meta_dir: Path
    artifacts_dir: Path
    executions_dir: Path
    domain_data_dir: Path
    source_dir: Path


class ProjectStorageService:
    """
    Universal project storage manager.

    Filesystem model:
    - project_root/
      - project_meta/
      - artifacts/
      - executions/
      - domain_data/
        - source/

    Notes:
    - There is a single canonical persisted source tree per project:
      domain_data/source
    - Agents choose repository-relative paths inside the canonical source tree.
      They do not choose storage roots.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or settings.agents_projects_root).expanduser().resolve()

    def get_root(self) -> Path:
        return self.root

    def get_project_root(self, project_id: int) -> Path:
        return self.root / "projects" / str(project_id)

    def get_project_paths(self, project_id: int) -> ProjectStoragePaths:
        project_root = self.get_project_root(project_id)
        domain_data_dir = project_root / DOMAIN_DATA_DIRNAME
        source_dir = domain_data_dir / SOURCE_DIRNAME

        return ProjectStoragePaths(
            root=self.root,
            project_root=project_root,
            project_meta_dir=project_root / PROJECT_META_DIRNAME,
            artifacts_dir=project_root / ARTIFACTS_DIRNAME,
            executions_dir=project_root / EXECUTIONS_DIRNAME,
            domain_data_dir=domain_data_dir,
            source_dir=source_dir,
        )

    def ensure_project_storage(self, project_id: int) -> ProjectStoragePaths:
        paths = self.get_project_paths(project_id)

        paths.root.mkdir(parents=True, exist_ok=True)
        paths.project_root.mkdir(parents=True, exist_ok=True)
        paths.project_meta_dir.mkdir(parents=True, exist_ok=True)
        paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
        paths.executions_dir.mkdir(parents=True, exist_ok=True)
        paths.domain_data_dir.mkdir(parents=True, exist_ok=True)
        paths.source_dir.mkdir(parents=True, exist_ok=True)

        return paths

    def write_project_storage_manifest(
        self,
        project_id: int,
    ) -> Path:
        paths = self.ensure_project_storage(project_id)
        manifest_path = paths.project_meta_dir / "storage_manifest.json"

        payload = {
            "project_id": project_id,
            "root": str(paths.project_root),
            "storage_model": "single_canonical_source_tree",
            "paths": {
                "project_meta_dir": str(paths.project_meta_dir),
                "artifacts_dir": str(paths.artifacts_dir),
                "executions_dir": str(paths.executions_dir),
                "domain_data_dir": str(paths.domain_data_dir),
                "source_dir": str(paths.source_dir),
            },
        }

        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest_path
