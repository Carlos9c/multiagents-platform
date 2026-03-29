from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings


PROJECT_META_DIRNAME = "project_meta"
ARTIFACTS_DIRNAME = "artifacts"
EXECUTIONS_DIRNAME = "executions"
DOMAIN_DATA_DIRNAME = "domain_data"

CODE_DOMAIN = "code"
CODE_SOURCE_DIRNAME = "source"
CODE_PROMOTED_DIRNAME = "promoted"


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


@dataclass(frozen=True)
class DomainStoragePaths:
    project_id: int
    domain_name: str
    domain_root: Path
    source_dir: Path | None
    promoted_dir: Path | None


class ProjectStorageService:
    """
    Universal project storage manager.

    It creates a stable filesystem layout for any project and optionally
    prepares domain-specific storage when a domain needs it.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or settings.agents_projects_root).expanduser().resolve()

    def get_root(self) -> Path:
        return self.root

    def get_project_root(self, project_id: int) -> Path:
        return self.root / "projects" / str(project_id)

    def get_project_paths(self, project_id: int) -> ProjectStoragePaths:
        project_root = self.get_project_root(project_id)
        return ProjectStoragePaths(
            root=self.root,
            project_root=project_root,
            project_meta_dir=project_root / PROJECT_META_DIRNAME,
            artifacts_dir=project_root / ARTIFACTS_DIRNAME,
            executions_dir=project_root / EXECUTIONS_DIRNAME,
            domain_data_dir=project_root / DOMAIN_DATA_DIRNAME,
        )

    def ensure_project_storage(self, project_id: int) -> ProjectStoragePaths:
        paths = self.get_project_paths(project_id)

        paths.root.mkdir(parents=True, exist_ok=True)
        paths.project_root.mkdir(parents=True, exist_ok=True)
        paths.project_meta_dir.mkdir(parents=True, exist_ok=True)
        paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
        paths.executions_dir.mkdir(parents=True, exist_ok=True)
        paths.domain_data_dir.mkdir(parents=True, exist_ok=True)

        return paths

    def get_domain_paths(self, project_id: int, domain_name: str) -> DomainStoragePaths:
        paths = self.get_project_paths(project_id)
        domain_root = paths.domain_data_dir / domain_name

        source_dir: Path | None = None
        promoted_dir: Path | None = None

        if domain_name == CODE_DOMAIN:
            source_dir = domain_root / CODE_SOURCE_DIRNAME
            promoted_dir = domain_root / CODE_PROMOTED_DIRNAME

        return DomainStoragePaths(
            project_id=project_id,
            domain_name=domain_name,
            domain_root=domain_root,
            source_dir=source_dir,
            promoted_dir=promoted_dir,
        )

    def ensure_domain_storage(self, project_id: int, domain_name: str) -> DomainStoragePaths:
        self.ensure_project_storage(project_id)
        paths = self.get_domain_paths(project_id, domain_name)

        paths.domain_root.mkdir(parents=True, exist_ok=True)

        if paths.source_dir is not None:
            paths.source_dir.mkdir(parents=True, exist_ok=True)

        if paths.promoted_dir is not None:
            paths.promoted_dir.mkdir(parents=True, exist_ok=True)

        return paths

    def write_project_storage_manifest(
        self,
        project_id: int,
        enabled_domains: list[str],
    ) -> Path:
        paths = self.ensure_project_storage(project_id)
        manifest_path = paths.project_meta_dir / "storage_manifest.json"

        payload = {
            "project_id": project_id,
            "root": str(paths.project_root),
            "enabled_domains": enabled_domains,
            "domains": {},
        }

        for domain_name in enabled_domains:
            domain_paths = self.ensure_domain_storage(project_id, domain_name)
            payload["domains"][domain_name] = {
                "domain_root": str(domain_paths.domain_root),
                "source_dir": str(domain_paths.source_dir) if domain_paths.source_dir else None,
                "promoted_dir": (
                    str(domain_paths.promoted_dir) if domain_paths.promoted_dir else None
                ),
            }

        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest_path
