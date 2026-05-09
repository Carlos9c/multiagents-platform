from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.services.analysis.analyzers.text_analyzer import (
    _MAX_BATCH_CHARS,
    TEXT_EXTENSIONS,
    TextFileAnalyzer,
)
from app.services.analysis.base import FileInput


def make_input(tmp_path: Path, relative: str, content: str) -> FileInput:
    abs_path = tmp_path / relative
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return FileInput(path=relative, absolute_path=abs_path)


def fake_batch_response(paths: list[str]) -> dict:
    return {
        "file_analyses": [
            {
                "path": p,
                "file_type": "code",
                "language": "Python",
                "summary": f"Summary of {p}.",
                "key_elements": ["foo"],
                "dependencies": ["bar"],
            }
            for p in paths
        ]
    }


def make_llm(responses: list[dict]) -> MagicMock:
    llm = MagicMock()
    llm.generate_structured.side_effect = responses
    return llm


# ── Extension coverage ───────────────────────────────────────────────────────


def test_py_extension_is_supported():
    assert ".py" in TEXT_EXTENSIONS


def test_common_extensions_covered():
    for ext in [".js", ".ts", ".go", ".rs", ".java", ".md", ".json", ".yaml", ".sh"]:
        assert ext in TEXT_EXTENSIONS, f"{ext} missing from TEXT_EXTENSIONS"


# ── analyze_files ────────────────────────────────────────────────────────────


def test_single_file_produces_one_analysis(tmp_path: Path):
    llm = make_llm([fake_batch_response(["main.py"])])
    analyzer = TextFileAnalyzer(llm_provider=llm)
    files = [make_input(tmp_path, "main.py", "print('hello')")]

    results = analyzer.analyze_files(files)

    assert len(results) == 1
    assert results[0].path == "main.py"
    assert results[0].language == "Python"


def test_multiple_files_within_budget_use_one_llm_call(tmp_path: Path):
    llm = make_llm([fake_batch_response(["a.py", "b.py"])])
    analyzer = TextFileAnalyzer(llm_provider=llm)
    files = [
        make_input(tmp_path, "a.py", "x = 1"),
        make_input(tmp_path, "b.py", "y = 2"),
    ]

    results = analyzer.analyze_files(files)

    assert llm.generate_structured.call_count == 1
    assert len(results) == 2


def test_files_exceeding_batch_budget_split_into_multiple_calls(tmp_path: Path):
    half = _MAX_BATCH_CHARS // 2 + 1
    llm = make_llm(
        [
            fake_batch_response(["big_a.py"]),
            fake_batch_response(["big_b.py"]),
        ]
    )
    analyzer = TextFileAnalyzer(llm_provider=llm)
    files = [
        make_input(tmp_path, "big_a.py", "x" * half),
        make_input(tmp_path, "big_b.py", "y" * half),
    ]

    results = analyzer.analyze_files(files)

    assert llm.generate_structured.call_count == 2
    assert len(results) == 2


def test_single_file_exceeding_budget_gets_own_batch(tmp_path: Path):
    llm = make_llm([fake_batch_response(["huge.py"])])
    analyzer = TextFileAnalyzer(llm_provider=llm)
    files = [make_input(tmp_path, "huge.py", "z" * (_MAX_BATCH_CHARS + 1))]

    results = analyzer.analyze_files(files)

    assert llm.generate_structured.call_count == 1
    assert len(results) == 1


def test_llm_failure_returns_fallback_entries(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = RuntimeError("LLM down")
    analyzer = TextFileAnalyzer(llm_provider=llm)
    files = [make_input(tmp_path, "mod.py", "pass")]

    results = analyzer.analyze_files(files)

    assert len(results) == 1
    assert "failed" in results[0].summary.lower()
    assert results[0].path == "mod.py"


def test_unreadable_file_uses_placeholder_content(tmp_path: Path):
    llm = make_llm([fake_batch_response(["ghost.py"])])
    analyzer = TextFileAnalyzer(llm_provider=llm)

    # Point to a non-existent file — _read should return the placeholder
    fi = FileInput(path="ghost.py", absolute_path=tmp_path / "ghost.py")
    results = analyzer.analyze_files([fi])

    assert len(results) == 1
    # The LLM was still called (placeholder content was passed)
    assert llm.generate_structured.call_count == 1


def test_empty_file_list_returns_empty_results():
    llm = MagicMock()
    analyzer = TextFileAnalyzer(llm_provider=llm)
    assert analyzer.analyze_files([]) == []
    llm.generate_structured.assert_not_called()
