"""
Tests for the atomic_task_generator evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.execution_run import EXECUTION_RUN_STATUS_FAILED
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
)
from app.services.supervisor.contracts import AgentEvaluationOutput
from app.services.supervisor.evaluators.atomic_task_generator_evaluator import (
    AtomicTaskGeneratorEvaluator,
    _aggregate_per_parent_results,
    _build_atomic_tasks_context,
    _load_atomic_trace_entries,
)

# ---------------------------------------------------------------------------
# _load_atomic_trace_entries
# ---------------------------------------------------------------------------


def test_load_atomic_trace_entries_returns_mapping_keyed_by_parent_task_id(tmp_path: Path):
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "agent": "atomic_task_generator",
                "call_type": "initial",
                "inputs": {"parent_task_id": 5},
                "reasoning": "split into 3 tasks",
            }
        )
        + "\n"
        + json.dumps(
            {
                "agent": "planner",
                "call_type": "initial",
            }
        )
        + "\n"
        + json.dumps(
            {
                "agent": "atomic_task_generator",
                "call_type": "reatomize",
                "inputs": {"parent_task_id": 5},
                "reasoning": "re-split after failure",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators.atomic_task_generator_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_atomic_trace_entries(1)

    # Parent task 5 should have been overwritten by the reatomize entry (later wins)
    assert 5 in result
    assert result[5]["call_type"] == "reatomize"
    # Planner entry should NOT be included
    assert len(result) == 1


def test_load_atomic_trace_entries_empty_when_no_file(tmp_path: Path):
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"
    with patch(
        "app.services.supervisor.evaluators.atomic_task_generator_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_atomic_trace_entries(99)
    assert result == {}


# ---------------------------------------------------------------------------
# _build_atomic_tasks_context
# ---------------------------------------------------------------------------


def test_build_atomic_tasks_context_includes_failure_info_for_non_completed(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj = make_project(name="Atomic ctx test")
    parent = make_task(
        project_id=proj.id,
        title="Parent",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Child OK",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_COMPLETED,
    )
    child_fail = make_task(
        project_id=proj.id,
        title="Child Failed",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_FAILED,
    )
    make_execution_run(
        task_id=child_fail.id,
        status=EXECUTION_RUN_STATUS_FAILED,
        failure_type="validation",
        remaining_scope="Still need to implement X",
        blockers_found="Missing dependency Y",
    )

    tasks = _build_atomic_tasks_context(db_session, parent)

    assert len(tasks) == 2
    ok_entry = next(t for t in tasks if t["title"] == "Child OK")
    fail_entry = next(t for t in tasks if t["title"] == "Child Failed")

    assert "failure_info" not in ok_entry
    assert "failure_info" in fail_entry
    assert fail_entry["failure_info"]["failure_type"] == "validation"
    assert fail_entry["failure_info"]["remaining_scope"] == "Still need to implement X"


def test_build_atomic_tasks_context_no_failure_info_for_completed(
    db_session: Session,
    make_project,
    make_task,
):
    proj = make_project(name="Clean project")
    parent = make_task(
        project_id=proj.id,
        title="Parent",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Done task",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_COMPLETED,
    )

    tasks = _build_atomic_tasks_context(db_session, parent)
    assert len(tasks) == 1
    assert "failure_info" not in tasks[0]


# ---------------------------------------------------------------------------
# _aggregate_per_parent_results
# ---------------------------------------------------------------------------


def _make_output(verdict: str, issues: list[str] | None = None) -> AgentEvaluationOutput:
    return AgentEvaluationOutput(
        verdict=verdict,
        findings=f"Findings for verdict={verdict}.",
        issues=issues or [],
        suggestions=[],
    )


def test_aggregate_uses_worst_verdict():
    results = [
        _make_output("healthy"),
        _make_output("needs_attention"),
        _make_output("healthy"),
    ]
    agg = _aggregate_per_parent_results(results, ["T1", "T2", "T3"])
    assert agg.verdict == "needs_attention"


def test_aggregate_degraded_wins():
    results = [
        _make_output("healthy"),
        _make_output("degraded", issues=["Critical scope gap"]),
        _make_output("needs_attention"),
    ]
    agg = _aggregate_per_parent_results(results, ["T1", "T2", "T3"])
    assert agg.verdict == "degraded"


def test_aggregate_empty_returns_healthy():
    agg = _aggregate_per_parent_results([], [])
    assert agg.verdict == "healthy"


def test_aggregate_prefixes_issues_with_parent_title():
    results = [
        _make_output("degraded", issues=["Too vague"]),
        _make_output("healthy"),
    ]
    agg = _aggregate_per_parent_results(results, ["Backend", "Frontend"])
    assert any("[Backend]" in issue for issue in agg.issues)


# ---------------------------------------------------------------------------
# AtomicTaskGeneratorEvaluator.evaluate — LLM is mocked
# ---------------------------------------------------------------------------


def _make_llm_output(verdict: str = "healthy") -> dict:
    return {
        "verdict": verdict,
        "findings": "The decomposition covers the parent scope adequately.",
        "issues": [],
        "suggestions": [],
    }


def test_atomic_evaluator_returns_healthy_for_clean_project(
    db_session: Session,
    make_project,
    make_task,
    tmp_path: Path,
):
    proj = make_project(name="Clean eval project")
    parent = make_task(
        project_id=proj.id,
        title="Parent",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Atomic child",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent.id,
        status=TASK_STATUS_COMPLETED,
    )

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    paths_storage = MagicMock()
    paths_storage.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _make_llm_output()

    with (
        patch(
            "app.services.supervisor.evaluators.atomic_task_generator_evaluator._storage.get_project_paths",
            return_value=paths_storage,
        ),
        patch(
            "app.services.supervisor.evaluators.atomic_task_generator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.atomic_task_generator_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = AtomicTaskGeneratorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    assert mock_provider.generate_structured.called


def test_atomic_evaluator_returns_healthy_when_no_parent_tasks(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Empty project")
    evaluator = AtomicTaskGeneratorEvaluator()
    result = evaluator.evaluate(db=db_session, project_id=proj.id)
    assert result.result is None


def test_atomic_evaluator_aggregates_worst_verdict(
    db_session: Session,
    make_project,
    make_task,
    tmp_path: Path,
):
    proj = make_project(name="Multi-parent project")
    parent_a = make_task(
        project_id=proj.id,
        title="Parent A",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    parent_b = make_task(
        project_id=proj.id,
        title="Parent B",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        parent_task_id=None,
    )
    make_task(
        project_id=proj.id,
        title="Child of A",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent_a.id,
        status=TASK_STATUS_COMPLETED,
    )
    make_task(
        project_id=proj.id,
        title="Child of B",
        planning_level=PLANNING_LEVEL_ATOMIC,
        parent_task_id=parent_b.id,
        status=TASK_STATUS_FAILED,
    )

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    paths_storage = MagicMock()
    paths_storage.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    # First call (Parent A) → healthy, second call (Parent B) → degraded
    mock_provider.generate_structured.side_effect = [
        _make_llm_output("healthy"),
        {
            "verdict": "degraded",
            "findings": "Scope was too broad for single task.",
            "issues": ["Task scope too large"],
            "suggestions": ["Split into smaller tasks"],
        },
    ]

    with (
        patch(
            "app.services.supervisor.evaluators.atomic_task_generator_evaluator._storage.get_project_paths",
            return_value=paths_storage,
        ),
        patch(
            "app.services.supervisor.evaluators.atomic_task_generator_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.atomic_task_generator_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = AtomicTaskGeneratorEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "degraded"
    assert mock_provider.generate_structured.call_count == 2
