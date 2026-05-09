from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.services.analysis import ANALYSIS_FILENAME, CodebaseAnalysisService
from app.services.project_storage import ProjectStorageService


def make_service(tmp_path: Path, llm: MagicMock | None = None) -> CodebaseAnalysisService:
    storage = ProjectStorageService(root=tmp_path / "root")
    return CodebaseAnalysisService(storage_service=storage, llm_provider=llm)


def fake_batch(paths: list[str]) -> dict:
    return {
        "file_analyses": [
            {
                "path": p,
                "file_type": "code",
                "language": "Python",
                "summary": f"Summary of {p}.",
                "key_elements": [],
                "dependencies": [],
            }
            for p in paths
        ]
    }


def fake_summary() -> dict:
    return {
        "project_summary": "A test project.",
        "main_technologies": ["Python"],
        "entry_points": ["main.py"],
    }


def source_dir(svc: CodebaseAnalysisService, project_id: int) -> Path:
    return svc.storage_service.ensure_project_storage(project_id).source_dir


# ── Storage guarantee ────────────────────────────────────────────────────────


def test_analyze_ensures_project_meta_exists(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.return_value = fake_summary()
    svc = make_service(tmp_path, llm)
    source_dir(svc, 1)

    svc.analyze(project_id=1, source_path="/some/path")

    paths = svc.storage_service.get_project_paths(1)
    assert paths.project_meta_dir.exists()


def test_analyze_writes_analysis_json(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [fake_batch(["app.py"]), fake_summary()]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    svc.analyze(project_id=1, source_path=str(src))

    paths = svc.storage_service.get_project_paths(1)
    assert (paths.project_meta_dir / ANALYSIS_FILENAME).exists()


# ── Routing ──────────────────────────────────────────────────────────────────


def test_text_files_reach_llm(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [fake_batch(["main.py"]), fake_summary()]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    result = svc.analyze(project_id=1, source_path=str(src))

    # batch call + summary call
    assert llm.generate_structured.call_count == 2
    assert result.files[0].path == "main.py"
    assert result.files[0].file_type == "code"


def test_binary_files_bypass_llm_batch(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.return_value = fake_summary()
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    result = svc.analyze(project_id=1, source_path=str(src))

    # Only the summary call; no batch call for binary
    assert llm.generate_structured.call_count == 1
    assert result.files[0].file_type == "asset"


def test_mixed_files_route_correctly(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [fake_batch(["script.py"]), fake_summary()]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "script.py").write_text("pass", encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG\x00")

    result = svc.analyze(project_id=1, source_path=str(src))

    assert result.total_files == 2
    types = {fa.path: fa.file_type for fa in result.files}
    assert types["script.py"] == "code"
    assert types["logo.png"] == "asset"


# ── Empty project ────────────────────────────────────────────────────────────


def test_empty_source_dir_no_llm_calls(tmp_path: Path):
    llm = MagicMock()
    svc = make_service(tmp_path, llm)
    source_dir(svc, 1)

    result = svc.analyze(project_id=1, source_path="/empty")

    llm.generate_structured.assert_not_called()
    assert result.total_files == 0
    assert "empty" in result.project_summary.lower()


# ── Accumulation and sort ────────────────────────────────────────────────────


def test_results_are_sorted_by_path(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [
        fake_batch(["z.py", "a.py"]),
        fake_summary(),
    ]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "z.py").write_text("z", encoding="utf-8")
    (src / "a.py").write_text("a", encoding="utf-8")

    result = svc.analyze(project_id=1, source_path=str(src))

    paths = [fa.path for fa in result.files]
    assert paths == sorted(paths)


# ── Project summary ──────────────────────────────────────────────────────────


def test_project_summary_populated_from_llm(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [
        fake_batch(["x.py"]),
        {
            "project_summary": "Does something useful.",
            "main_technologies": ["Python", "FastAPI"],
            "entry_points": ["x.py"],
        },
    ]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "x.py").write_text("from fastapi import FastAPI", encoding="utf-8")

    result = svc.analyze(project_id=1, source_path=str(src))

    assert result.project_summary == "Does something useful."
    assert "FastAPI" in result.main_technologies


def test_summary_failure_returns_fallback(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [
        fake_batch(["x.py"]),
        RuntimeError("summary LLM down"),
    ]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "x.py").write_text("pass", encoding="utf-8")

    result = svc.analyze(project_id=1, source_path=str(src))

    assert "could not be generated" in result.project_summary.lower()


# ── Persistence ──────────────────────────────────────────────────────────────


def test_get_analysis_returns_none_when_missing(tmp_path: Path):
    svc = make_service(tmp_path)
    svc.storage_service.ensure_project_storage(1)
    assert svc.get_analysis(project_id=1) is None


def test_analysis_roundtrip(tmp_path: Path):
    llm = MagicMock()
    llm.generate_structured.side_effect = [fake_batch(["app.py"]), fake_summary()]
    svc = make_service(tmp_path, llm)
    src = source_dir(svc, 1)
    (src / "app.py").write_text("import os", encoding="utf-8")

    written = svc.analyze(project_id=1, source_path=str(src))
    loaded = svc.get_analysis(project_id=1)

    assert loaded is not None
    assert loaded.total_files == written.total_files
    assert loaded.project_summary == written.project_summary
    assert loaded.files[0].path == written.files[0].path
