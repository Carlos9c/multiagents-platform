"""Source file reader for QA agents.

Reads the most relevant project source files for LLM analysis,
applying product-type-specific prioritization and a total size cap.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Total character budget across all files read per agent call.
_TOTAL_CHAR_BUDGET = 24_000

# Max characters read from any single file.
_MAX_FILE_CHARS = 6_000

# Files that are always read first, regardless of product type.
_UNIVERSAL_PRIORITY = [
    "README.md",
    "README.rst",
    "README.txt",
    "readme.md",
]

# Product-type-specific file patterns (checked in order).
_PRIORITY_PATTERNS: dict[str, list[str]] = {
    "web_app": [
        "package.json",
        "vite.config.*",
        "webpack.config.*",
        "src/main.*",
        "src/App.*",
        "src/index.*",
        "src/router.*",
        "*.config.js",
        "*.config.ts",
    ],
    "rest_api": [
        "openapi.yaml",
        "openapi.yml",
        "swagger.yaml",
        "swagger.yml",
        "openapi.json",
        "swagger.json",
        "main.py",
        "app.py",
        "app/main.py",
        "src/main.*",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
    ],
    "graphql_api": [
        "schema.graphql",
        "schema.gql",
        "*.graphql",
        "*.gql",
        "main.py",
        "app.py",
        "src/main.*",
    ],
    "cli_tool": [
        "main.py",
        "cli.py",
        "__main__.py",
        "src/main.*",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
    ],
    "mobile_android": [
        "AndroidManifest.xml",
        "app/src/main/AndroidManifest.xml",
        "build.gradle",
        "app/build.gradle",
        "pubspec.yaml",
        "package.json",
        "lib/main.dart",
        "app/src/main/java/**/*.kt",
        "app/src/main/java/**/*.java",
    ],
    "library": [
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "src/**/*.py",
        "src/**/*.ts",
        "src/**/*.rs",
    ],
    "desktop_app": [
        "package.json",
        "main.js",
        "electron.js",
        "src-tauri/tauri.conf.json",
        "src/main.*",
        "pyproject.toml",
        "requirements.txt",
    ],
}

# Always include test files if present.
_TEST_PATTERNS = [
    "tests/**/*.py",
    "test/**/*.py",
    "**/*.test.ts",
    "**/*.test.js",
    "**/*.spec.ts",
    "**/*.spec.js",
    "**/*.test.kt",
]


def _read_file(path: Path, max_chars: int = _MAX_FILE_CHARS) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:max_chars]
    except OSError:
        return None


def _glob_limited(root: Path, pattern: str, limit: int = 5) -> list[Path]:
    """Glob with a result cap to avoid collecting huge file trees."""
    try:
        return sorted(root.glob(pattern))[:limit]
    except Exception:
        return []


def read_for_analysis(
    source_path: str,
    product_type: str,
    *,
    max_total_chars: int = _TOTAL_CHAR_BUDGET,
) -> dict[str, str]:
    """Return {relative_path: content} for the most relevant source files.

    Files are selected in priority order and capped to max_total_chars total.
    """
    root = Path(source_path)
    result: dict[str, str] = {}
    total = 0

    def _try_add(path: Path) -> None:
        nonlocal total
        if total >= max_total_chars:
            return
        rel = str(path.relative_to(root))
        if rel in result:
            return
        content = _read_file(path, _MAX_FILE_CHARS)
        if content is None:
            return
        remaining = max_total_chars - total
        snippet = content[:remaining]
        result[rel] = snippet
        total += len(snippet)

    # 1. Universal priority files
    for fname in _UNIVERSAL_PRIORITY:
        candidate = root / fname
        if candidate.exists():
            _try_add(candidate)

    # 2. Product-type-specific files
    for pattern in _PRIORITY_PATTERNS.get(product_type, []):
        if "**" in pattern or "*" in pattern:
            for path in _glob_limited(root, pattern):
                _try_add(path)
        else:
            candidate = root / pattern
            if candidate.exists():
                _try_add(candidate)

    # 3. Test files (up to 3)
    test_count = 0
    for pattern in _TEST_PATTERNS:
        if test_count >= 3:
            break
        for path in _glob_limited(root, pattern, limit=2):
            if test_count >= 3:
                break
            _try_add(path)
            test_count += 1

    return result


def format_for_prompt(files: dict[str, str]) -> str:
    """Format the file dict as a readable block for an LLM prompt."""
    if not files:
        return "(no source files found)"
    parts = []
    for rel_path, content in files.items():
        parts.append(f"=== {rel_path} ===\n{content}")
    return "\n\n".join(parts)
