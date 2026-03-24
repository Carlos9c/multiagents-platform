from __future__ import annotations

from pathlib import Path


def write_text_file(
    *,
    root_dir: str,
    relative_path: str,
    content: str,
    encoding: str = "utf-8",
) -> str:
    """
    Deterministic file write tool.

    The LLM decides the path/content.
    This tool performs the actual write safely under the workspace root.
    """
    root = Path(root_dir).resolve()
    destination = (root / relative_path).resolve()

    if not str(destination).startswith(str(root)):
        raise ValueError(
            f"Refusing to write outside workspace root. path={relative_path}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding=encoding)
    return str(destination)