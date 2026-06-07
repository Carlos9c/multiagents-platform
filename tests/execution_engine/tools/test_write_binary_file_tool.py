from __future__ import annotations

from pathlib import Path

import pytest

from app.execution_engine.tools.write_binary_file_tool import write_binary_file


def test_write_creates_file(tmp_path: Path):
    content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    result = write_binary_file(
        root_dir=str(tmp_path),
        relative_path="assets/icon.png",
        content=content,
    )

    written = Path(result)
    assert written.exists()
    assert written.read_bytes() == content


def test_write_creates_intermediate_dirs(tmp_path: Path):
    write_binary_file(
        root_dir=str(tmp_path),
        relative_path="a/b/c/image.webp",
        content=b"RIFF\x00\x00\x00\x00WEBP",
    )
    assert (tmp_path / "a" / "b" / "c" / "image.webp").exists()


def test_write_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="outside workspace root"):
        write_binary_file(
            root_dir=str(tmp_path),
            relative_path="../escape.png",
            content=b"data",
        )


def test_write_returns_absolute_path(tmp_path: Path):
    result = write_binary_file(
        root_dir=str(tmp_path),
        relative_path="logo.png",
        content=b"\xff\xd8\xff",
    )
    assert Path(result).is_absolute()
