from __future__ import annotations

from pathlib import Path


def list_workspace_files(
    root_dir: str,
    *,
    max_files: int = 500,
) -> list[str]:
    root = Path(root_dir)
    if not root.exists():
        return []

    results: list[str] = []

    for path in root.rglob("*"):
        if len(results) >= max_files:
            break

        if path.is_dir():
            continue

        try:
            relative = path.relative_to(root).as_posix()
        except Exception:
            continue

        results.append(relative)

    results.sort()
    return results