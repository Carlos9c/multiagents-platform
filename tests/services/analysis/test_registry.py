from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.services.analysis.base import BaseFileAnalyzer, FileInput
from app.services.analysis.registry import FileAnalyzerRegistry


def make_input(path: str) -> FileInput:
    return FileInput(path=path, absolute_path=Path(path))


def make_analyzer(extensions: frozenset[str]) -> BaseFileAnalyzer:
    analyzer = MagicMock(spec=BaseFileAnalyzer)
    analyzer.supported_extensions = extensions
    return analyzer


def make_fallback() -> BaseFileAnalyzer:
    return make_analyzer(frozenset())


# ── get_analyzer ────────────────────────────────────────────────────────────


def test_routes_to_analyzer_by_extension():
    py_analyzer = make_analyzer(frozenset({".py"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[py_analyzer], fallback=fallback)

    assert registry.get_analyzer("src/main.py") is py_analyzer


def test_routes_to_fallback_for_unknown_extension():
    py_analyzer = make_analyzer(frozenset({".py"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[py_analyzer], fallback=fallback)

    assert registry.get_analyzer("logo.png") is fallback


def test_routes_to_fallback_for_no_extension():
    py_analyzer = make_analyzer(frozenset({".py"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[py_analyzer], fallback=fallback)

    assert registry.get_analyzer("Makefile") is fallback


def test_extension_matching_is_case_insensitive():
    py_analyzer = make_analyzer(frozenset({".py"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[py_analyzer], fallback=fallback)

    assert registry.get_analyzer("script.PY") is py_analyzer


def test_first_matching_analyzer_wins():
    a = make_analyzer(frozenset({".py"}))
    b = make_analyzer(frozenset({".py", ".js"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[a, b], fallback=fallback)

    assert registry.get_analyzer("mod.py") is a


# ── group_by_analyzer ───────────────────────────────────────────────────────


def test_groups_files_by_analyzer():
    py_analyzer = make_analyzer(frozenset({".py"}))
    js_analyzer = make_analyzer(frozenset({".js"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[py_analyzer, js_analyzer], fallback=fallback)

    files = [
        make_input("a.py"),
        make_input("b.js"),
        make_input("c.py"),
        make_input("logo.png"),
    ]

    groups = dict(registry.group_by_analyzer(files))

    assert len(groups[py_analyzer]) == 2
    assert len(groups[js_analyzer]) == 1
    assert len(groups[fallback]) == 1


def test_empty_file_list_returns_empty_groups():
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[], fallback=fallback)
    assert registry.group_by_analyzer([]) == []


def test_group_order_follows_first_file_seen():
    a = make_analyzer(frozenset({".py"}))
    b = make_analyzer(frozenset({".js"}))
    fallback = make_fallback()
    registry = FileAnalyzerRegistry(analyzers=[a, b], fallback=fallback)

    files = [make_input("x.js"), make_input("y.py")]
    groups = registry.group_by_analyzer(files)

    # First file is .js → b appears first
    assert groups[0][0] is b
    assert groups[1][0] is a
