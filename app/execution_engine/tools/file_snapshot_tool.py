from __future__ import annotations

from pathlib import Path


def capture_file_snapshot(
    *,
    root_dir: str,
    relative_paths: list[str],
    encoding: str = "utf-8",
) -> dict[str, str | None]:
    """
    Capture pre-write state for a set of files.
    None means the file did not exist.
    """
    root = Path(root_dir).resolve()
    snapshots: dict[str, str | None] = {}

    for relative_path in relative_paths:
        abs_path = (root / relative_path).resolve()

        if not str(abs_path).startswith(str(root)):
            raise ValueError(
                f"Refusing to snapshot outside workspace root. path={relative_path}"
            )

        if abs_path.exists() and abs_path.is_file():
            snapshots[relative_path] = abs_path.read_text(encoding=encoding)
        else:
            snapshots[relative_path] = None

    return snapshots


def restore_file_snapshot(
    *,
    root_dir: str,
    snapshots: dict[str, str | None],
    encoding: str = "utf-8",
) -> None:
    """
    Restore files to their pre-write state.
    None means delete the file if it exists.
    """
    root = Path(root_dir).resolve()

    for relative_path, previous_content in snapshots.items():
        abs_path = (root / relative_path).resolve()

        if not str(abs_path).startswith(str(root)):
            raise ValueError(
                f"Refusing to restore outside workspace root. path={relative_path}"
            )

        if previous_content is None:
            if abs_path.exists():
                abs_path.unlink()
            continue

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(previous_content, encoding=encoding)
