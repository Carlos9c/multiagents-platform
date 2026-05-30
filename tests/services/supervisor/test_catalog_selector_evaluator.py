"""
Tests for the catalog_selector evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.catalog_selector_evaluator import (
    CatalogSelectorEvaluator,
    _get_final_runtime_info,
    _load_catalog_selector_trace_entries,
)

# ---------------------------------------------------------------------------
# _load_catalog_selector_trace_entries
# ---------------------------------------------------------------------------


def test_load_catalog_selector_trace_entries_filters_by_agent(tmp_path: Path):
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "agent": "catalog_selector",
                "call_type": "initial",
                "project_id": 1,
                "output_snapshot": {"selected_image": "python:3.12-slim"},
            }
        )
        + "\n"
        + json.dumps({"agent": "environment_planner", "call_type": "initial", "project_id": 1})
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_catalog_selector_trace_entries(1)

    assert len(result) == 1
    assert result[0]["agent"] == "catalog_selector"
    assert result[0]["output_snapshot"]["selected_image"] == "python:3.12-slim"


def test_load_catalog_selector_trace_entries_empty_when_no_file(tmp_path: Path):
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"
    with patch(
        "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_catalog_selector_trace_entries(99)
    assert result == []


def test_load_catalog_selector_trace_entries_returns_multiple_in_order(tmp_path: Path):
    """Multiple entries (e.g. project re-setup) are returned in insertion order."""
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "agent": "catalog_selector",
                "run": 1,
                "output_snapshot": {"selected_image": "node:20"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "agent": "catalog_selector",
                "run": 2,
                "output_snapshot": {"selected_image": "python:3.12"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_catalog_selector_trace_entries(1)

    assert len(result) == 2
    assert result[-1]["run"] == 2  # most recent last


# ---------------------------------------------------------------------------
# _get_final_runtime_info
# ---------------------------------------------------------------------------


def test_get_final_runtime_info_from_project_spec(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Runtime info test")
    proj.runtime_spec = {"runtime_type": "node_npm", "image": "node:20-slim"}
    db_session.commit()

    runtime_type, image = _get_final_runtime_info(db_session, proj.id)
    assert runtime_type == "node_npm"
    assert image == "node:20-slim"


def test_get_final_runtime_info_returns_none_when_no_spec(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No spec project")
    runtime_type, image = _get_final_runtime_info(db_session, proj.id)
    assert runtime_type is None
    assert image is None


# ---------------------------------------------------------------------------
# CatalogSelectorEvaluator.evaluate — LLM is mocked
# ---------------------------------------------------------------------------


def _make_trace_entry(selected_image: str | None = "python:3.12-slim") -> dict:
    return {
        "agent": "catalog_selector",
        "call_type": "initial",
        "project_id": 1,
        "inputs": {
            "project_name": "Test project",
            "task_titles_count": 5,
            "catalog_images_count": 4,
        },
        "reasoning": "The project uses Python ML libraries, python:3.12-slim is the best match.",
        "output_snapshot": {"selected_image": selected_image},
    }


def test_catalog_selector_evaluator_returns_healthy_when_no_trace(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="No trace project")
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        evaluator = CatalogSelectorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)
    assert result.result is None


def test_catalog_selector_evaluator_calls_llm_once(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Catalog eval project")
    proj.runtime_spec = {"runtime_type": "python_venv", "image": "python:3.12-slim"}
    db_session.commit()

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("python:3.12-slim")) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "healthy",
        "findings": "The selection of python:3.12-slim is correct for this ML project.",
        "issues": [],
        "suggestions": [],
    }

    with (
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = CatalogSelectorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_catalog_selector_evaluator_uses_most_recent_entry(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When multiple catalog_selector entries exist, only the last one is evaluated."""
    proj = make_project(name="Multi-run project")
    proj.runtime_spec = {"runtime_type": "python_venv", "image": "python:3.12-slim"}
    db_session.commit()

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("node:20"))  # old/wrong selection
        + "\n"
        + json.dumps(_make_trace_entry("python:3.12-slim"))  # correct re-run
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "needs_attention",
        "findings": "Second selection was correct but first was wrong technology.",
        "issues": ["First run selected wrong runtime"],
        "suggestions": [],
    }

    captured_prompt: list[str] = []

    def capture_generate(**kwargs):
        captured_prompt.append(kwargs.get("user_prompt", ""))
        return mock_provider.generate_structured.return_value

    mock_provider.generate_structured.side_effect = capture_generate

    with (
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = CatalogSelectorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "needs_attention"
    # Only one LLM call (not two)
    assert mock_provider.generate_structured.call_count == 1
    # The prompt should reference the most recent selection
    assert "python:3.12-slim" in captured_prompt[0]


def test_catalog_selector_evaluator_degraded_verdict_propagates(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Wrong image project")
    proj.runtime_spec = {"runtime_type": "python_venv", "image": "python:3.12-slim"}
    db_session.commit()

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    # LLM selected node:20 for a Python project
    trace_path.write_text(
        json.dumps(_make_trace_entry("node:20")) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "degraded",
        "findings": "Wrong technology selected: node:20 for a pure Python project.",
        "issues": ["Selected node:20 for a Python project"],
        "suggestions": ["Add project description quality checks"],
    }

    with (
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = CatalogSelectorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "degraded"
    assert len(result.result.issues) == 1


def test_catalog_evaluator_includes_env_planner_summary_in_prompt(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When env_planner entries exist in the JSONL, the user prompt includes the downstream summary."""
    proj = make_project(name="Cross-ref project")
    proj.runtime_spec = {"runtime_type": "python_venv", "image": "python:3.12-slim"}
    db_session.commit()

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"

    env_initial = {
        "agent": "environment_planner",
        "call_type": "initial",
        "project_id": proj.id,
        "reasoning": "Chose python_venv for ML project.",
        "output_snapshot": {
            "runtime_type": "python_venv",
            "image": "python:3.12-slim",
            "dependencies_count": 8,
            "env_vars_count": 0,
        },
    }
    env_repair = {
        "agent": "environment_planner",
        "call_type": "repair",
        "project_id": proj.id,
        "reasoning": "Added missing torch package.",
        "output_snapshot": {
            "corrected_image": "python:3.12-slim",
            "corrected_dependencies_count": 9,
        },
    }
    catalog_entry = _make_trace_entry("python:3.12-slim")

    trace_path.write_text(
        json.dumps(catalog_entry)
        + "\n"
        + json.dumps(env_initial)
        + "\n"
        + json.dumps(env_repair)
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    captured_prompts: list[str] = []

    def capture(**kwargs):
        captured_prompts.append(kwargs.get("user_prompt", ""))
        return {
            "verdict": "healthy",
            "findings": "Good selection, repair was due to dependency gap not image choice.",
            "issues": [],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = CatalogSelectorEvaluator()
        evaluator.evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 1
    # Downstream impact section must appear
    assert "Downstream impact" in captured_prompts[0]
    assert "repair_count=1" in captured_prompts[0]
    # Both call_types from env_planner trace should appear
    assert "initial" in captured_prompts[0]
    assert "repair" in captured_prompts[0]


def test_catalog_evaluator_omits_downstream_section_when_no_env_planner_trace(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When no env_planner entries exist, no downstream section appears in the prompt."""
    proj = make_project(name="No env trace project")
    proj.runtime_spec = {"runtime_type": "python_venv", "image": "python:3.12-slim"}
    db_session.commit()

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("python:3.12-slim")) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    captured_prompts: list[str] = []

    def capture(**kwargs):
        captured_prompts.append(kwargs.get("user_prompt", ""))
        return {
            "verdict": "healthy",
            "findings": "Good selection, no downstream issues.",
            "issues": [],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.catalog_selector_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = CatalogSelectorEvaluator()
        evaluator.evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 1
    assert "Downstream impact" not in captured_prompts[0]
