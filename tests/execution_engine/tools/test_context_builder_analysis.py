from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.execution_engine.tools.context_builder_tool import (
    _MAX_CATALOGUE_FILES,
    _build_codebase_analysis_excerpt,
    _render_codebase_analysis_excerpt,
    build_context_selection_input,
)
from app.models.task import PLANNING_LEVEL_ATOMIC, TASK_STATUS_PENDING
from app.services.analysis.base import CodebaseAnalysis, FileAnalysis

# ── helpers ───────────────────────────────────────────────────────────────────


def make_file_analysis(path: str, language: str | None = "Python") -> FileAnalysis:
    return FileAnalysis(
        path=path,
        file_type="code",
        language=language,
        summary=f"Module at {path}.",
        key_elements=[],
        dependencies=[],
    )


def make_analysis(*, total_files: int = 3) -> CodebaseAnalysis:
    files = [make_file_analysis(f"mod_{i}.py") for i in range(total_files)]
    return CodebaseAnalysis(
        analyzed_at="2026-01-01T00:00:00+00:00",
        source_path="/project/src",
        total_files=total_files,
        project_summary="A well-structured Python project with REST API and tests.",
        main_technologies=["Python", "FastAPI"],
        entry_points=["main.py"],
        files=files,
    )


# ── _render_codebase_analysis_excerpt ────────────────────────────────────────


def test_render_includes_project_summary():
    analysis = make_analysis()
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "well-structured Python project" in excerpt


def test_render_includes_technologies():
    analysis = make_analysis()
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "FastAPI" in excerpt


def test_render_includes_entry_points():
    analysis = make_analysis()
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "main.py" in excerpt


def test_render_includes_total_file_count():
    analysis = make_analysis(total_files=7)
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "7" in excerpt


def test_render_includes_file_paths():
    analysis = make_analysis(total_files=2)
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "mod_0.py" in excerpt
    assert "mod_1.py" in excerpt


def test_render_includes_language_when_present():
    analysis = make_analysis(total_files=1)
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "Python" in excerpt


def test_render_file_without_language_skips_lang():
    files = [make_file_analysis("README.md", language=None)]
    analysis = CodebaseAnalysis(
        analyzed_at="2026-01-01T00:00:00+00:00",
        source_path="/project",
        total_files=1,
        project_summary="A project.",
        main_technologies=[],
        entry_points=[],
        files=files,
    )
    excerpt = _render_codebase_analysis_excerpt(analysis)
    assert "README.md" in excerpt
    assert "(code)" in excerpt


def test_render_caps_catalogue_at_max_files():
    total = _MAX_CATALOGUE_FILES + 10
    analysis = make_analysis(total_files=total)
    excerpt = _render_codebase_analysis_excerpt(analysis)
    # The header should mention only max files in the catalogue
    assert f"{_MAX_CATALOGUE_FILES} of {total}" in excerpt
    # Only the first _MAX_CATALOGUE_FILES paths should appear
    assert f"mod_{_MAX_CATALOGUE_FILES - 1}.py" in excerpt
    assert f"mod_{_MAX_CATALOGUE_FILES}.py" not in excerpt


# ── _build_codebase_analysis_excerpt ─────────────────────────────────────────


def test_build_returns_none_when_no_analysis():
    with patch(
        "app.execution_engine.tools.context_builder_tool.CodebaseAnalysisService"
    ) as MockSvc:
        MockSvc.return_value.get_analysis.return_value = None
        result = _build_codebase_analysis_excerpt(project_id=1)
    assert result is None


def test_build_returns_excerpt_when_analysis_exists():
    with patch(
        "app.execution_engine.tools.context_builder_tool.CodebaseAnalysisService"
    ) as MockSvc:
        MockSvc.return_value.get_analysis.return_value = make_analysis()
        result = _build_codebase_analysis_excerpt(project_id=1)
    assert result is not None
    assert "FastAPI" in result


def test_build_returns_none_on_exception():
    with patch(
        "app.execution_engine.tools.context_builder_tool.CodebaseAnalysisService"
    ) as MockSvc:
        MockSvc.return_value.get_analysis.side_effect = RuntimeError("storage unavailable")
        result = _build_codebase_analysis_excerpt(project_id=1)
    assert result is None


# ── build_context_selection_input ────────────────────────────────────────────


def test_build_context_selection_input_populates_analysis_excerpt(
    db_session: Session, make_project, make_task
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        status=TASK_STATUS_PENDING,
        planning_level=PLANNING_LEVEL_ATOMIC,
    )

    with patch(
        "app.execution_engine.tools.context_builder_tool._build_codebase_analysis_excerpt"
    ) as mock_excerpt:
        mock_excerpt.return_value = "Codebase excerpt content."
        result = build_context_selection_input(db=db_session, current_task=task)

    assert result.codebase_analysis_excerpt == "Codebase excerpt content."
    mock_excerpt.assert_called_once_with(project.id)


def test_build_context_selection_input_analysis_none_when_no_analysis(
    db_session: Session, make_project, make_task
):
    project = make_project()
    task = make_task(
        project_id=project.id,
        status=TASK_STATUS_PENDING,
        planning_level=PLANNING_LEVEL_ATOMIC,
    )

    with patch(
        "app.execution_engine.tools.context_builder_tool._build_codebase_analysis_excerpt"
    ) as mock_excerpt:
        mock_excerpt.return_value = None
        result = build_context_selection_input(db=db_session, current_task=task)

    assert result.codebase_analysis_excerpt is None
