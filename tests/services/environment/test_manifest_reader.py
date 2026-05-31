from __future__ import annotations

import pathlib

from app.services.environment.manifest_reader import read_manifest_content

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(directory: pathlib.Path, filename: str, content: str) -> pathlib.Path:
    p = directory / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_none_for_empty_directory(tmp_path: pathlib.Path) -> None:
    result = read_manifest_content(tmp_path)
    assert result is None


def test_returns_none_for_nonexistent_directory(tmp_path: pathlib.Path) -> None:
    result = read_manifest_content(tmp_path / "does_not_exist")
    assert result is None


def test_reads_requirements_txt(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "requirements.txt", "fastapi==0.115.0\nuvicorn==0.34.0\n")
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "requirements.txt" in result
    assert "fastapi==0.115.0" in result
    assert "uvicorn==0.34.0" in result


def test_reads_pyproject_toml(tmp_path: pathlib.Path) -> None:
    content = '[tool.poetry.dependencies]\npython = "^3.12"\nfastapi = "^0.115.0"\n'
    _write(tmp_path, "pyproject.toml", content)
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "pyproject.toml" in result
    assert "fastapi" in result


def test_reads_package_json(tmp_path: pathlib.Path) -> None:
    content = '{"name": "my-app", "dependencies": {"react": "^18.0.0"}}'
    _write(tmp_path, "package.json", content)
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "package.json" in result
    assert "react" in result


def test_reads_multiple_manifests(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "requirements.txt", "numpy==1.26.0\n")
    _write(tmp_path, "pyproject.toml", '[project]\nname = "my-app"\n')
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "requirements.txt" in result
    assert "pyproject.toml" in result
    assert "numpy==1.26.0" in result


def test_reads_go_mod(tmp_path: pathlib.Path) -> None:
    content = "module github.com/user/myapp\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.10.0\n)\n"
    _write(tmp_path, "go.mod", content)
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "go.mod" in result
    assert "gin-gonic" in result


def test_reads_cargo_toml(tmp_path: pathlib.Path) -> None:
    content = '[package]\nname = "my-app"\nversion = "0.1.0"\n\n[dependencies]\ntokio = "1.40.0"\n'
    _write(tmp_path, "Cargo.toml", content)
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "Cargo.toml" in result
    assert "tokio" in result


def test_reads_glob_requirements_dev(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "requirements-dev.txt", "pytest==8.3.0\n")
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "requirements-dev.txt" in result
    assert "pytest==8.3.0" in result


def test_reads_csproj(tmp_path: pathlib.Path) -> None:
    content = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net9.0</TargetFramework></PropertyGroup></Project>'
    _write(tmp_path, "MyApp.csproj", content)
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "MyApp.csproj" in result
    assert "net9.0" in result


def test_ignores_files_in_subdirectories(tmp_path: pathlib.Path) -> None:
    subdir = tmp_path / "subproject"
    subdir.mkdir()
    _write(subdir, "requirements.txt", "requests==2.32.0\n")
    result = read_manifest_content(tmp_path)
    assert result is None


def test_truncates_large_manifest(tmp_path: pathlib.Path) -> None:
    large_content = "somepackage==1.0.0\n" * 1000  # well over 8000 chars
    _write(tmp_path, "requirements.txt", large_content)
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert len(result) < len(large_content) + 200  # truncated


def test_output_format_has_code_fences(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "requirements.txt", "fastapi==0.115.0\n")
    result = read_manifest_content(tmp_path)
    assert result is not None
    assert "```" in result
    assert "###" in result
