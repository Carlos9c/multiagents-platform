from __future__ import annotations

from pathlib import Path

import pytest

from app.services.import_source_service import (
    EXCLUDED_DIR_NAMES,
    EXCLUDED_EXTENSIONS,
    EXCLUDED_FILE_NAMES,
    MAX_FILE_SIZE_BYTES,
    ImportSourceError,
    ImportSourceService,
)
from app.services.project_storage import ProjectStorageService


def make_service(tmp_path: Path) -> ImportSourceService:
    storage = ProjectStorageService(root=tmp_path / "agents_root")
    return ImportSourceService(storage_service=storage)


def source_dir(service: ImportSourceService, project_id: int) -> Path:
    return service.storage_service.get_project_paths(project_id).source_dir


# ---------------------------------------------------------------------------
# Basic happy-path
# ---------------------------------------------------------------------------


def test_import_copies_text_files(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    (src / "src").mkdir(parents=True)
    (src / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (src / "README.md").write_text("# readme", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    assert result.total_files_copied == 2
    assert result.total_bytes_copied > 0
    assert not result.skipped_files

    paths = {f.path for f in result.copied_files}
    assert "src/main.py" in paths
    assert "README.md" in paths

    for info in result.copied_files:
        assert info.file_type == "text"


def test_import_creates_destination_files(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    (src / "app.py").mkdir(parents=True)
    (src / "app.py").rmdir()
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    svc.import_source(project_id=1, source_path=src)

    dest = source_dir(svc, 1)
    assert (dest / "app.py").read_text(encoding="utf-8") == "x = 1"


def test_import_detects_binary_files(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    binary_file = src / "image.png"
    binary_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    result = svc.import_source(project_id=1, source_path=src)

    assert result.total_files_copied == 1
    assert result.copied_files[0].file_type == "binary"


def test_result_contains_source_and_destination_paths(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    assert result.source_path == str(src.resolve())
    assert result.destination_path == str(source_dir(svc, 1))


# ---------------------------------------------------------------------------
# Overwrite behaviour
# ---------------------------------------------------------------------------


def test_import_overwrites_existing_source_dir(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    (src / "new.py").write_text("new", encoding="utf-8")

    dest = source_dir(svc, 1)
    svc.storage_service.ensure_project_storage(1)
    (dest / "old.py").write_text("old", encoding="utf-8")

    svc.import_source(project_id=1, source_path=src)

    assert (dest / "new.py").exists()
    assert not (dest / "old.py").exists()


def test_second_import_replaces_first(tmp_path: Path):
    svc = make_service(tmp_path)

    src_v1 = tmp_path / "v1"
    src_v1.mkdir()
    (src_v1 / "v1.py").write_text("v1", encoding="utf-8")
    svc.import_source(project_id=1, source_path=src_v1)

    src_v2 = tmp_path / "v2"
    src_v2.mkdir()
    (src_v2 / "v2.py").write_text("v2", encoding="utf-8")
    svc.import_source(project_id=1, source_path=src_v2)

    dest = source_dir(svc, 1)
    assert (dest / "v2.py").exists()
    assert not (dest / "v1.py").exists()


# ---------------------------------------------------------------------------
# Directory exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("excluded_dir", sorted(EXCLUDED_DIR_NAMES))
def test_excluded_dirs_are_skipped(tmp_path: Path, excluded_dir: str):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    excluded = src / excluded_dir
    excluded.mkdir(parents=True)
    (excluded / "file.py").write_text("x", encoding="utf-8")
    (src / "keep.py").write_text("y", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    paths = {f.path for f in result.copied_files}
    assert "keep.py" in paths
    assert not any(excluded_dir in p for p in paths)


def test_excluded_dir_nested_inside_normal_dir_is_also_skipped(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    (src / "src" / "__pycache__").mkdir(parents=True)
    (src / "src" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00")
    (src / "src" / "mod.py").write_text("pass", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    paths = {f.path for f in result.copied_files}
    assert "src/mod.py" in paths
    assert not any("__pycache__" in p for p in paths)


# ---------------------------------------------------------------------------
# File name and extension exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("excluded_name", sorted(EXCLUDED_FILE_NAMES))
def test_excluded_file_names_are_skipped(tmp_path: Path, excluded_name: str):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    (src / excluded_name).write_bytes(b"data")
    (src / "keep.txt").write_text("keep", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    paths = {f.path for f in result.copied_files}
    assert "keep.txt" in paths
    assert excluded_name not in paths


@pytest.mark.parametrize("ext", sorted(EXCLUDED_EXTENSIONS))
def test_excluded_extensions_are_skipped(tmp_path: Path, ext: str):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    (src / f"compiled{ext}").write_bytes(b"\x00" * 10)
    (src / "source.py").write_text("pass", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    paths = {f.path for f in result.copied_files}
    assert "source.py" in paths
    assert f"compiled{ext}" not in paths


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------


def test_file_over_size_limit_is_skipped(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    big_file = src / "big.bin"
    big_file.write_bytes(b"x" * (MAX_FILE_SIZE_BYTES + 1))
    (src / "small.txt").write_text("ok", encoding="utf-8")

    result = svc.import_source(project_id=1, source_path=src)

    paths = {f.path for f in result.copied_files}
    assert "small.txt" in paths
    assert "big.bin" not in paths

    assert len(result.skipped_files) == 1
    assert "file_too_large" in result.skipped_files[0].reason


def test_file_at_exact_size_limit_is_copied(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()
    (src / "exact.bin").write_bytes(b"x" * MAX_FILE_SIZE_BYTES)

    result = svc.import_source(project_id=1, source_path=src)

    assert result.total_files_copied == 1


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_nonexistent_source_path_raises(tmp_path: Path):
    svc = make_service(tmp_path)
    with pytest.raises(ImportSourceError, match="does not exist"):
        svc.import_source(project_id=1, source_path=tmp_path / "nonexistent")


def test_file_path_instead_of_dir_raises(tmp_path: Path):
    svc = make_service(tmp_path)
    file = tmp_path / "file.txt"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ImportSourceError, match="not a directory"):
        svc.import_source(project_id=1, source_path=file)


# ---------------------------------------------------------------------------
# Empty source
# ---------------------------------------------------------------------------


def test_empty_source_directory_produces_empty_result(tmp_path: Path):
    svc = make_service(tmp_path)
    src = tmp_path / "project"
    src.mkdir()

    result = svc.import_source(project_id=1, source_path=src)

    assert result.total_files_copied == 0
    assert result.total_bytes_copied == 0
    assert not result.copied_files
    assert not result.skipped_files
