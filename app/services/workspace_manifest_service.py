from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from app.execution_engine.contracts import FileDocumentationEntry

logger = logging.getLogger(__name__)

WORKSPACE_MANIFEST_FILENAME = "workspace_manifest.json"


class ManifestChange(BaseModel):
    agent: str
    task_id: int
    run_id: int
    operation: str  # "create" | "modify"
    summary: str
    timestamp: str


class FileManifestEntry(BaseModel):
    path: str
    description: str
    agent: str
    task_id: int
    last_modified: str
    historical_changes: list[ManifestChange] = Field(default_factory=list)


class WorkspaceManifestService:
    """
    Manages the per-project workspace manifest file at project_meta/workspace_manifest.json.

    The manifest tracks every file written by any execution subagent across all tasks.
    Each entry holds the current documentation of the file (what it does, what it exposes,
    how it fits the project) and an append-only history of changes.

    The manifest is written at promotion time (after workspace is promoted to source),
    so it only reflects files that have been consolidated into the project source tree.

    Schema:
      workspace_manifest.json  →  dict[path, FileManifestEntry]
    """

    def read_manifest(self, project_meta_dir: Path) -> dict[str, FileManifestEntry]:
        """Read the manifest file. Returns empty dict if the file does not exist yet."""
        manifest_path = project_meta_dir / WORKSPACE_MANIFEST_FILENAME
        if not manifest_path.exists():
            return {}
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            return {path: FileManifestEntry.model_validate(entry) for path, entry in raw.items()}
        except Exception:
            logger.exception("workspace_manifest_read_failed path=%s", str(manifest_path))
            return {}

    def upsert_entries(
        self,
        project_meta_dir: Path,
        entries: list[FileDocumentationEntry],
        *,
        task_id: int,
        run_id: int,
    ) -> None:
        """
        Upsert file documentation entries into the manifest.

        For each entry:
        - If the path is new: creates a fresh FileManifestEntry.
        - If the path exists: updates description + appends to historical_changes.

        Entries with empty documentation are silently skipped.
        """
        if not entries:
            return

        manifest = self.read_manifest(project_meta_dir)
        now = datetime.now(timezone.utc).isoformat()

        for doc in entries:
            if not doc.documentation.strip():
                continue

            change = ManifestChange(
                agent=doc.agent,
                task_id=task_id,
                run_id=run_id,
                operation=doc.operation,
                summary=doc.change_summary or doc.documentation[:120],
                timestamp=now,
            )

            existing = manifest.get(doc.path)
            if existing is None:
                manifest[doc.path] = FileManifestEntry(
                    path=doc.path,
                    description=doc.documentation,
                    agent=doc.agent,
                    task_id=task_id,
                    last_modified=now,
                    historical_changes=[change],
                )
            else:
                existing.description = doc.documentation
                existing.agent = doc.agent
                existing.task_id = task_id
                existing.last_modified = now
                existing.historical_changes.append(change)

        project_meta_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = project_meta_dir / WORKSPACE_MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(
                {path: entry.model_dump() for path, entry in manifest.items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info(
            "workspace_manifest_updated project_meta_dir=%s entries_upserted=%d",
            str(project_meta_dir),
            len([e for e in entries if e.documentation.strip()]),
        )

    def get_path_descriptions(self, project_meta_dir: Path) -> dict[str, str]:
        """
        Returns a lightweight {path: description} dict for prompt injection.

        This is the form consumed by agents (context_selection_agent,
        document_writer_agent). The full historical_changes data is for
        auditing only and is not exposed to LLM prompts.
        """
        manifest = self.read_manifest(project_meta_dir)
        return {path: entry.description for path, entry in manifest.items()}
