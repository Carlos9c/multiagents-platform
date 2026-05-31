"""
Tests for the environment_planner evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators.environment_planner_evaluator import (
    _REPAIR_THRESHOLD_DEGRADED,
    EnvironmentPlannerEvaluator,
    _count_repairs,
    _get_final_spec_snapshot,
    _load_environment_planner_trace_entries,
)

# ---------------------------------------------------------------------------
# _load_environment_planner_trace_entries
# ---------------------------------------------------------------------------


def test_load_environment_planner_trace_entries_filters_by_agent(tmp_path: Path):
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps({"agent": "environment_planner", "call_type": "initial", "project_id": 1})
        + "\n"
        + json.dumps({"agent": "catalog_selector", "call_type": "initial", "project_id": 1})
        + "\n"
        + json.dumps({"agent": "environment_planner", "call_type": "repair", "project_id": 1})
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_environment_planner_trace_entries(1)

    assert len(result) == 2
    assert all(e["agent"] == "environment_planner" for e in result)
    assert result[0]["call_type"] == "initial"
    assert result[1]["call_type"] == "repair"


def test_load_environment_planner_trace_entries_empty_when_no_file(tmp_path: Path):
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"
    with patch(
        "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_environment_planner_trace_entries(99)
    assert result == []


# ---------------------------------------------------------------------------
# _count_repairs
# ---------------------------------------------------------------------------


def test_count_repairs_zero_when_only_initial():
    entries = [{"agent": "environment_planner", "call_type": "initial"}]
    assert _count_repairs(entries) == 0


def test_count_repairs_counts_only_repair_entries():
    entries = [
        {"agent": "environment_planner", "call_type": "initial"},
        {"agent": "environment_planner", "call_type": "repair"},
        {"agent": "environment_planner", "call_type": "repair"},
    ]
    assert _count_repairs(entries) == 2


# ---------------------------------------------------------------------------
# _get_final_spec_snapshot
# ---------------------------------------------------------------------------


def test_get_final_spec_snapshot_returns_runtime_spec(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Spec test")
    # Manually set runtime_spec on the project
    proj.runtime_spec = {"runtime_type": "python_venv", "image": "python:3.12-slim"}
    db_session.commit()

    snapshot = _get_final_spec_snapshot(db_session, proj.id)
    assert snapshot["runtime_type"] == "python_venv"
    assert snapshot["image"] == "python:3.12-slim"


def test_get_final_spec_snapshot_returns_empty_dict_when_no_spec(
    db_session: Session,
    make_project,
):
    proj = make_project(name="No spec project")
    snapshot = _get_final_spec_snapshot(db_session, proj.id)
    assert snapshot == {}


# ---------------------------------------------------------------------------
# EnvironmentPlannerEvaluator.evaluate — auto-degrade logic
# ---------------------------------------------------------------------------


def test_evaluator_returns_healthy_when_no_trace(
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
        "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        evaluator = EnvironmentPlannerEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)
    assert result.result is None


def _make_trace_entry(call_type: str, *, has_existing_environment_context: bool = False) -> dict:
    base: dict = {
        "agent": "environment_planner",
        "call_type": call_type,
        "project_id": 1,
    }
    if call_type == "initial":
        base.update(
            {
                "inputs": {
                    "project_name": "Test project",
                    "has_existing_environment_context": has_existing_environment_context,
                    "catalog_hint": None,
                },
                "reasoning": "Chose python_venv because the project description requires Python ML libraries.",
                "output_snapshot": {"runtime_type": "python_venv", "image": "python:3.12-slim"},
            }
        )
    else:
        base.update(
            {
                "reasoning": "Fixed missing torch package.",
                "output_snapshot": {
                    "corrected_image": "python:3.12-slim",
                    "corrected_dependencies_count": 5,
                },
            }
        )
    return base


def test_evaluator_auto_degrades_without_llm_when_repair_count_reaches_threshold(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Many repairs project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    # Write initial + exactly REPAIR_THRESHOLD_DEGRADED repair entries
    entries = [_make_trace_entry("initial")]
    for _ in range(_REPAIR_THRESHOLD_DEGRADED):
        entries.append(_make_trace_entry("repair"))
    trace_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
    ):
        evaluator = EnvironmentPlannerEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "degraded"
    # LLM must NOT have been called
    mock_provider.generate_structured.assert_not_called()
    assert str(_REPAIR_THRESHOLD_DEGRADED) in result.result.findings


def test_evaluator_calls_llm_when_repair_count_below_threshold(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="One repair project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("initial"))
        + "\n"
        + json.dumps(_make_trace_entry("repair"))
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "needs_attention",
        "findings": "Initial spec required one repair to add the torch package.",
        "issues": ["Missing torch in initial dependency list"],
        "suggestions": ["Include torch when tasks mention neural networks"],
    }

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = EnvironmentPlannerEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "needs_attention"
    mock_provider.generate_structured.assert_called_once()


def test_evaluator_returns_healthy_for_zero_repairs(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    proj = make_project(name="Clean environment project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("initial")) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "verdict": "healthy",
        "findings": "Initial spec was correct: python_venv with all required packages.",
        "issues": [],
        "suggestions": [],
    }

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = EnvironmentPlannerEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    mock_provider.generate_structured.assert_called_once()


def test_evaluator_includes_catalog_trace_in_prompt_when_present(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When the JSONL has a catalog_selector entry, the user prompt must reference it."""
    proj = make_project(name="Catalog cross-ref project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    catalog_entry = {
        "agent": "catalog_selector",
        "call_type": "initial",
        "project_id": proj.id,
        "reasoning": "Selected python:3.12-slim for Python ML tasks.",
        "output_snapshot": {"selected_image": "python:3.12-slim"},
    }
    trace_path.write_text(
        json.dumps(catalog_entry) + "\n" + json.dumps(_make_trace_entry("initial")) + "\n",
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
            "findings": "Good spec built on top of catalog selection.",
            "issues": [],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = EnvironmentPlannerEvaluator()
        evaluator.evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 1
    assert "catalog_selector" in captured_prompts[0]
    assert "python:3.12-slim" in captured_prompts[0]


def test_evaluator_omits_catalog_section_when_no_catalog_entry(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When no catalog_selector entry exists, no catalog section appears in the prompt."""
    proj = make_project(name="No catalog project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("initial")) + "\n",
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
            "findings": "Good spec without catalog.",
            "issues": [],
            "suggestions": [],
        }

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = EnvironmentPlannerEvaluator()
        evaluator.evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 1
    assert "Catalog selector context" not in captured_prompts[0]


def test_evaluator_includes_evolutionary_note_when_context_present(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When trace shows has_existing_environment_context=True, evolutionary note must appear in prompt."""
    proj = make_project(name="Evolutionary project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("initial", has_existing_environment_context=True)) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    captured_prompts: list[str] = []

    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = lambda **kwargs: (
        captured_prompts.append(kwargs.get("user_prompt", ""))
        or {
            "verdict": "healthy",
            "findings": "Correctly preserved existing pinned versions.",
            "issues": [],
            "suggestions": [],
        }
    )

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        EnvironmentPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 1
    assert "EVOLUTIONARY" in captured_prompts[0]
    assert "existing manifest" in captured_prompts[0].lower()


def test_evaluator_omits_evolutionary_note_for_new_project(
    db_session: Session,
    make_project,
    tmp_path: Path,
):
    """When has_existing_environment_context=False, no evolutionary note in prompt."""
    proj = make_project(name="New project")

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry("initial", has_existing_environment_context=False)) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    captured_prompts: list[str] = []

    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = lambda **kwargs: (
        captured_prompts.append(kwargs.get("user_prompt", ""))
        or {
            "verdict": "healthy",
            "findings": "Clean initial spec for a new project without existing manifests.",
            "issues": [],
            "suggestions": [],
        }
    )

    with (
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.environment_planner_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        EnvironmentPlannerEvaluator().evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 1
    assert "EVOLUTIONARY" not in captured_prompts[0]
