from __future__ import annotations

from pathlib import Path


def read_text_file(path: str, encoding: str = "utf-8") -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return file_path.read_text(encoding=encoding)
