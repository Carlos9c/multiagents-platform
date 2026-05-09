from __future__ import annotations

from pathlib import Path

from app.services.analysis.analyzers.default_analyzer import DefaultFileAnalyzer
from app.services.analysis.base import FileInput


def make_input(path: str) -> FileInput:
    return FileInput(path=path, absolute_path=Path(path))


def test_returns_one_entry_per_file():
    analyzer = DefaultFileAnalyzer()
    files = [make_input("logo.png"), make_input("archive.zip")]
    results = analyzer.analyze_files(files)
    assert len(results) == 2


def test_file_type_is_asset():
    analyzer = DefaultFileAnalyzer()
    result = analyzer.analyze_files([make_input("image.png")])[0]
    assert result.file_type == "asset"


def test_language_is_none():
    analyzer = DefaultFileAnalyzer()
    result = analyzer.analyze_files([make_input("binary.exe")])[0]
    assert result.language is None


def test_summary_includes_extension():
    analyzer = DefaultFileAnalyzer()
    result = analyzer.analyze_files([make_input("data.bin")])[0]
    assert ".bin" in result.summary


def test_summary_handles_no_extension():
    analyzer = DefaultFileAnalyzer()
    result = analyzer.analyze_files([make_input("Makefile")])[0]
    assert "no extension" in result.summary


def test_key_elements_and_dependencies_are_empty():
    analyzer = DefaultFileAnalyzer()
    result = analyzer.analyze_files([make_input("img.jpg")])[0]
    assert result.key_elements == []
    assert result.dependencies == []


def test_path_is_preserved():
    analyzer = DefaultFileAnalyzer()
    result = analyzer.analyze_files([make_input("assets/icons/logo.svg")])[0]
    assert result.path == "assets/icons/logo.svg"


def test_empty_input_returns_empty_list():
    analyzer = DefaultFileAnalyzer()
    assert analyzer.analyze_files([]) == []


def test_supported_extensions_is_empty():
    # DefaultFileAnalyzer must never claim any extension — it's always the fallback
    assert DefaultFileAnalyzer.supported_extensions == frozenset()
