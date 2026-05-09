from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.services.project_storage import ProjectStorageService

logger = logging.getLogger(__name__)

# Directory names that are never imported, regardless of where they appear in the tree.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".tox",
        ".eggs",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        "vendor",
        "target",
        ".idea",
        ".vscode",
        "htmlcov",
        "coverage_html_report",
    }
)

# Individual file names that are always excluded.
EXCLUDED_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    }
)

# File extensions whose compiled/cache nature makes them useless as source.
EXCLUDED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".class",
    }
)

# Any single file above this threshold is skipped.
MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB

# Bytes inspected to decide text vs binary.
_BINARY_DETECTION_CHUNK = 8_000


class ImportSourceError(Exception):
    """Raised when the import cannot proceed due to a configuration or I/O problem."""


@dataclass
class ImportedFileInfo:
    path: str  # Relative POSIX path inside source_dir
    size_bytes: int
    file_type: str  # "text" or "binary"


@dataclass
class SkippedFileInfo:
    path: str
    reason: str


@dataclass
class ImportSourceResult:
    copied_files: list[ImportedFileInfo] = field(default_factory=list)
    skipped_files: list[SkippedFileInfo] = field(default_factory=list)
    total_files_copied: int = 0
    total_bytes_copied: int = 0
    source_path: str = ""
    destination_path: str = ""


class ImportSourceService:
    """
    Copies an external directory tree into a project's source_dir.

    The import is always a full overwrite: source_dir is cleared before copying.
    Filtering removes dependency caches, VCS metadata, compiled artefacts, and
    files that exceed the size limit. Binary files (images, archives, etc.) are
    copied normally and classified as "binary" in the result manifest.
    """

    def __init__(self, storage_service: ProjectStorageService | None = None) -> None:
        self.storage_service = storage_service or ProjectStorageService()

    def import_source(
        self,
        project_id: int,
        source_path: str | Path,
    ) -> ImportSourceResult:
        source = Path(source_path).expanduser().resolve()

        if not source.exists():
            raise ImportSourceError(f"Source path does not exist: {source}")
        if not source.is_dir():
            raise ImportSourceError(f"Source path is not a directory: {source}")

        paths = self.storage_service.ensure_project_storage(project_id)
        dest = paths.source_dir

        self._clear_directory(dest)

        # Materialise the storage manifest so project_meta is explicitly
        # documented before any subsequent service writes to it.
        self.storage_service.write_project_storage_manifest(project_id)

        result = ImportSourceResult(
            source_path=str(source),
            destination_path=str(dest),
        )

        for file_path in self._walk_files(source):
            relative = file_path.relative_to(source)
            relative_posix = relative.as_posix()

            try:
                size = file_path.stat().st_size
            except OSError as exc:
                result.skipped_files.append(
                    SkippedFileInfo(path=relative_posix, reason=f"stat_error: {exc}")
                )
                continue

            if size > MAX_FILE_SIZE_BYTES:
                result.skipped_files.append(
                    SkippedFileInfo(
                        path=relative_posix,
                        reason=f"file_too_large ({size} bytes, limit {MAX_FILE_SIZE_BYTES})",
                    )
                )
                continue

            dest_file = dest / relative
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            try:
                shutil.copy2(file_path, dest_file)
            except OSError as exc:
                result.skipped_files.append(
                    SkippedFileInfo(path=relative_posix, reason=f"copy_error: {exc}")
                )
                continue

            file_type = self._detect_file_type(file_path)
            result.copied_files.append(
                ImportedFileInfo(path=relative_posix, size_bytes=size, file_type=file_type)
            )

        result.total_files_copied = len(result.copied_files)
        result.total_bytes_copied = sum(f.size_bytes for f in result.copied_files)

        logger.info(
            "import_source_complete project_id=%s source=%s copied=%d skipped=%d bytes=%d",
            project_id,
            str(source),
            result.total_files_copied,
            len(result.skipped_files),
            result.total_bytes_copied,
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _walk_files(self, source: Path) -> Iterator[Path]:
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            if self._any_part_excluded(relative):
                continue
            if item.name in EXCLUDED_FILE_NAMES:
                continue
            if item.suffix.lower() in EXCLUDED_EXTENSIONS:
                continue
            yield item

    def _any_part_excluded(self, relative: Path) -> bool:
        # Check every directory component (all parts except the final filename).
        return any(part in EXCLUDED_DIR_NAMES for part in relative.parts[:-1])

    def _clear_directory(self, directory: Path) -> None:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            return
        for item in directory.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

    def _detect_file_type(self, file_path: Path) -> str:
        try:
            with file_path.open("rb") as fh:
                chunk = fh.read(_BINARY_DETECTION_CHUNK)
            return "binary" if b"\x00" in chunk else "text"
        except OSError:
            return "binary"
