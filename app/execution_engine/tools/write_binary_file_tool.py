from __future__ import annotations

from pathlib import Path


def write_binary_file(
    *,
    root_dir: str,
    relative_path: str,
    content: bytes,
) -> str:
    """
    Write binary content (e.g. images) safely under the workspace root.

    Enforces the same boundary contract as write_text_file: refuses any path
    that resolves outside root_dir.  Creates intermediate directories when
    needed.  Returns the absolute path of the written file.
    """
    root = Path(root_dir).resolve()
    destination = (root / relative_path).resolve()

    if not str(destination).startswith(str(root)):
        raise ValueError(f"Refusing to write outside workspace root. path={relative_path}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return str(destination)
