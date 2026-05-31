"""Reads dependency manifest files from a project source directory.

Used by the environment planner to detect the existing tech stack in
evolutionary (existing-codebase) projects. Pure filesystem reads — no LLM.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Root-level manifest filenames to scan (order matters — listed top to bottom).
_ROOT_MANIFESTS: list[str] = [
    "requirements.txt",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
]

# Glob patterns for manifests that can have variable names.
_GLOB_PATTERNS: list[str] = [
    "requirements-*.txt",
    "requirements_*.txt",
    "*.csproj",
]

# Truncate individual manifest files at this size to keep LLM context reasonable.
_MAX_MANIFEST_CHARS = 8_000


def read_manifest_content(source_dir: Path) -> str | None:
    """Return formatted content of dependency manifests found at the root of *source_dir*.

    Returns ``None`` when no manifests are found or the directory does not exist.
    Only scans the project root — does not recurse into subdirectories.
    """
    if not source_dir.is_dir():
        return None

    found: list[tuple[str, str]] = []

    # Fixed-name manifests
    for name in _ROOT_MANIFESTS:
        path = source_dir / name
        _try_read(path, found)

    # Glob-pattern manifests (root only)
    seen_names = {name for name, _ in found}
    for pattern in _GLOB_PATTERNS:
        for path in sorted(source_dir.glob(pattern)):
            if path.is_file() and path.name not in seen_names:
                if _try_read(path, found):
                    seen_names.add(path.name)
                    if pattern == "*.csproj":
                        break  # One .csproj is enough

    if not found:
        return None

    sections: list[str] = []
    for filename, content in found:
        sections.append(f"### {filename}\n```\n{content}\n```")

    logger.debug("manifest_reader_found files=%s", [f for f, _ in found])
    return "\n\n".join(sections)


def _try_read(path: Path, found: list[tuple[str, str]]) -> bool:
    """Attempt to read *path* and append its name + truncated content to *found*.

    Returns True on success, False otherwise.
    """
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").strip()
        if content:
            found.append((path.name, content[:_MAX_MANIFEST_CHARS]))
            return True
    except OSError:
        pass
    return False
