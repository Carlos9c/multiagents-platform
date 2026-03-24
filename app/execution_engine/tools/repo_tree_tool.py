from __future__ import annotations

from pathlib import Path


def build_repo_tree_snapshot(
    root_path: str,
    *,
    max_depth: int = 3,
    max_entries_per_dir: int = 40,
) -> str:
    root = Path(root_path)
    if not root.exists():
        return f"[missing root] {root_path}"

    lines: list[str] = [str(root)]

    def walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            children = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        except Exception:
            lines.append(f"{prefix}[unreadable]")
            return

        children = children[:max_entries_per_dir]

        for child in children:
            marker = "└── "
            lines.append(f"{prefix}{marker}{child.name}")
            if child.is_dir():
                walk(child, prefix + "    ", depth + 1)

    walk(root, "", 1)
    return "\n".join(lines)