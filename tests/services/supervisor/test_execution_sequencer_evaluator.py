"""
Tests for the execution_sequencer evaluator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
)
from app.services.supervisor.contracts import AgentEvaluationOutput
from app.services.supervisor.evaluators.execution_sequencer_evaluator import (
    ExecutionSequencerEvaluator,
    _aggregate_version_results,
    _build_task_outcomes,
    _extract_all_task_ids,
    _load_sequencer_trace_entries,
)

# ---------------------------------------------------------------------------
# _load_sequencer_trace_entries
# ---------------------------------------------------------------------------


def test_load_sequencer_trace_entries_returns_only_sequencer_entries(tmp_path: Path):
    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps({"agent": "execution_sequencer", "plan_version": 1, "call_type": "initial"})
        + "\n"
        + json.dumps({"agent": "planner", "call_type": "initial"})
        + "\n"
        + json.dumps({"agent": "execution_sequencer", "plan_version": 2, "call_type": "resequence"})
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    with patch(
        "app.services.supervisor.evaluators.execution_sequencer_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_sequencer_trace_entries(1)

    assert len(result) == 2
    assert all(e["agent"] == "execution_sequencer" for e in result)
    assert result[0]["plan_version"] == 1
    assert result[1]["plan_version"] == 2


def test_load_sequencer_trace_entries_empty_when_no_file(tmp_path: Path):
    paths = MagicMock()
    paths.project_meta_dir = tmp_path / "nonexistent"
    with patch(
        "app.services.supervisor.evaluators.execution_sequencer_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        result = _load_sequencer_trace_entries(99)
    assert result == []


# ---------------------------------------------------------------------------
# _extract_all_task_ids
# ---------------------------------------------------------------------------


def test_extract_all_task_ids_from_snapshot():
    entry = {
        "output_snapshot": {
            "execution_batches": [
                {"task_ids": [1, 2, 3]},
                {"task_ids": [4, 5]},
                {"task_ids": [3, 6]},  # 3 is a duplicate
            ]
        }
    }
    ids = _extract_all_task_ids(entry)
    assert ids == [1, 2, 3, 4, 5, 6]


def test_extract_all_task_ids_empty_when_no_batches():
    entry = {"output_snapshot": {}}
    assert _extract_all_task_ids(entry) == []


# ---------------------------------------------------------------------------
# _build_task_outcomes
# ---------------------------------------------------------------------------


def test_build_task_outcomes_includes_failure_info(
    db_session: Session,
    make_project,
    make_task,
    make_execution_run,
):
    proj = make_project(name="Outcomes test")
    task_ok = make_task(
        project_id=proj.id,
        title="OK Task",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_COMPLETED,
    )
    task_fail = make_task(
        project_id=proj.id,
        title="Fail Task",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
    )
    make_execution_run(
        task_id=task_fail.id,
        status="failed",
        failure_type="validation",
        blockers_found="Something is blocking",
    )

    outcomes = _build_task_outcomes(db_session, proj.id, [task_ok.id, task_fail.id])

    assert len(outcomes) == 2
    ok_entry = next(o for o in outcomes if o["title"] == "OK Task")
    fail_entry = next(o for o in outcomes if o["title"] == "Fail Task")

    assert "failure_info" not in ok_entry
    assert "failure_info" in fail_entry
    assert fail_entry["failure_info"]["blockers_found"] == "Something is blocking"


def test_build_task_outcomes_handles_missing_task_id(
    db_session: Session,
    make_project,
):
    proj = make_project(name="Missing task test")
    outcomes = _build_task_outcomes(db_session, proj.id, [99999])
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "unknown"


# ---------------------------------------------------------------------------
# _aggregate_version_results
# ---------------------------------------------------------------------------


def _make_output(verdict: str) -> AgentEvaluationOutput:
    return AgentEvaluationOutput(
        verdict=verdict,
        findings=f"Findings for {verdict}.",
        issues=["an issue"] if verdict != "healthy" else [],
        suggestions=[],
    )


def test_aggregate_version_results_worst_wins():
    results = [_make_output("healthy"), _make_output("needs_attention"), _make_output("healthy")]
    agg = _aggregate_version_results(results, [1, 2, 3])
    assert agg.verdict == "needs_attention"


def test_aggregate_version_results_empty_returns_healthy():
    agg = _aggregate_version_results([], [])
    assert agg.verdict == "healthy"
    assert "trace entries" in agg.findings.lower()


def test_aggregate_version_results_issues_labeled_with_version():
    results = [
        _make_output("needs_attention"),
        _make_output("healthy"),
    ]
    agg = _aggregate_version_results(results, [1, 2])
    assert any("[v1]" in issue for issue in agg.issues)


# ---------------------------------------------------------------------------
# ExecutionSequencerEvaluator.evaluate — LLM is mocked
# ---------------------------------------------------------------------------


def _make_llm_output(verdict: str = "healthy") -> dict:
    return {
        "verdict": verdict,
        "findings": "The sequencing order is logical and dependencies are correctly identified.",
        "issues": [],
        "suggestions": [],
    }


def _make_trace_entry(plan_version: int, task_ids: list[int]) -> dict:
    return {
        "agent": "execution_sequencer",
        "plan_version": plan_version,
        "supersedes_plan_version": plan_version - 1 if plan_version > 1 else None,
        "call_type": "initial" if plan_version == 1 else "resequence",
        "input_snapshot": {},
        "output_snapshot": {
            "plan_version": plan_version,
            "execution_batches": [{"task_ids": task_ids}],
        },
        "batch_sizes": [len(task_ids)],
        "batches_produced_count": 1,
    }


def test_execution_sequencer_evaluator_calls_llm_once_per_plan_version(
    db_session: Session,
    make_project,
    make_task,
    tmp_path: Path,
):
    proj = make_project(name="Sequencer eval test")
    task = make_task(
        project_id=proj.id,
        title="Task 1",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_COMPLETED,
    )

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    trace_path.write_text(
        json.dumps(_make_trace_entry(1, [task.id]))
        + "\n"
        + json.dumps(_make_trace_entry(2, [task.id]))
        + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _make_llm_output()

    with (
        patch(
            "app.services.supervisor.evaluators.execution_sequencer_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.execution_sequencer_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.execution_sequencer_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = ExecutionSequencerEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is not None
    assert result.result.verdict == "healthy"
    # Two plan versions → two LLM calls
    assert mock_provider.generate_structured.call_count == 2


def test_resequence_evaluation_includes_previous_plan_snapshot(
    db_session: Session,
    make_project,
    make_task,
    tmp_path: Path,
):
    """When evaluating plan_version=2, the user prompt must reference the v1 output_snapshot."""
    proj = make_project(name="Resequence snapshot test")
    task = make_task(
        project_id=proj.id,
        title="Task A",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=TASK_STATUS_FAILED,
    )

    meta_dir = tmp_path / "project_meta"
    meta_dir.mkdir(parents=True)
    trace_path = meta_dir / "planning_trace.jsonl"
    v1_entry = _make_trace_entry(1, [task.id])
    v2_entry = _make_trace_entry(2, [task.id])
    trace_path.write_text(
        json.dumps(v1_entry) + "\n" + json.dumps(v2_entry) + "\n",
        encoding="utf-8",
    )
    paths = MagicMock()
    paths.project_meta_dir = meta_dir

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = _make_llm_output("needs_attention")

    captured_prompts: list[str] = []

    def capture(**kwargs):
        captured_prompts.append(kwargs.get("user_prompt", ""))
        return mock_provider.generate_structured.return_value

    mock_provider.generate_structured.side_effect = capture

    with (
        patch(
            "app.services.supervisor.evaluators.execution_sequencer_evaluator._storage.get_project_paths",
            return_value=paths,
        ),
        patch(
            "app.services.supervisor.evaluators.execution_sequencer_evaluator.get_llm_provider",
            return_value=mock_provider,
        ),
        patch(
            "app.services.supervisor.evaluators.execution_sequencer_evaluator.resolve_system_prompt",
            return_value="You are an evaluator.",
        ),
    ):
        evaluator = ExecutionSequencerEvaluator()
        evaluator.evaluate(db=db_session, project_id=proj.id)

    assert len(captured_prompts) == 2

    # First call (v1 — initial): NO previous_plan_snapshot section
    assert "Previous plan structure" not in captured_prompts[0]

    # Second call (v2 — resequence): MUST include the v1 output_snapshot
    assert "Previous plan structure" in captured_prompts[1]
    assert "plan_version=1" in captured_prompts[1]  # references v1 in section header


def test_execution_sequencer_evaluator_returns_healthy_when_no_trace(
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
        "app.services.supervisor.evaluators.execution_sequencer_evaluator._storage.get_project_paths",
        return_value=paths,
    ):
        evaluator = ExecutionSequencerEvaluator()
        result = evaluator.evaluate(db=db_session, project_id=proj.id)

    assert result.result is None
