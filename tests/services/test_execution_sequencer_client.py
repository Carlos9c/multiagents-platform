"""Tests for execution_sequencer_client pre-processing helpers."""

from types import SimpleNamespace

from app.services.execution_sequencer_client import (
    _ensure_final_checkpoint_stage_closure,
    _sort_execution_batches_by_index,
    _validate_dependency_ordering,
)

# ---------------------------------------------------------------------------
# _sort_execution_batches_by_index
# ---------------------------------------------------------------------------


def _batch(batch_index: int, task_ids: list[int] | None = None) -> dict:
    return {
        "batch_index": batch_index,
        "task_ids": task_ids or [batch_index],
        "name": f"Batch {batch_index}",
    }


def test_sort_batches_already_in_order_is_idempotent():
    raw = {
        "execution_batches": [_batch(1), _batch(2), _batch(3)],
    }
    result = _sort_execution_batches_by_index(raw)
    assert [b["batch_index"] for b in result["execution_batches"]] == [1, 2, 3]


def test_sort_batches_out_of_order_is_corrected():
    raw = {
        "execution_batches": [_batch(1), _batch(3), _batch(2)],
    }
    result = _sort_execution_batches_by_index(raw)
    assert [b["batch_index"] for b in result["execution_batches"]] == [1, 2, 3]


def test_sort_batches_completely_reversed():
    raw = {
        "execution_batches": [_batch(4), _batch(3), _batch(2), _batch(1)],
    }
    result = _sort_execution_batches_by_index(raw)
    assert [b["batch_index"] for b in result["execution_batches"]] == [1, 2, 3, 4]


def test_sort_batches_single_batch_unchanged():
    raw = {
        "execution_batches": [_batch(1)],
    }
    result = _sort_execution_batches_by_index(raw)
    assert len(result["execution_batches"]) == 1
    assert result["execution_batches"][0]["batch_index"] == 1


def test_sort_batches_non_dict_input_returned_unchanged():
    result = _sort_execution_batches_by_index("not a dict")  # type: ignore[arg-type]
    assert result == "not a dict"


def test_sort_batches_missing_execution_batches_returned_unchanged():
    raw = {"checkpoints": []}
    result = _sort_execution_batches_by_index(raw)
    assert "execution_batches" not in result


def test_sort_batches_empty_execution_batches_returned_unchanged():
    raw = {"execution_batches": []}
    result = _sort_execution_batches_by_index(raw)
    assert result["execution_batches"] == []


def test_sort_batches_non_dict_batch_entries_do_not_raise():
    """Non-dict entries (malformed LLM output) must not crash the pre-sort."""
    raw = {
        "execution_batches": [_batch(3), "not_a_dict", _batch(1)],
    }
    result = _sort_execution_batches_by_index(raw)
    # Malformed entries sort with key=0, so they end up first; no exception raised.
    assert isinstance(result["execution_batches"], list)


def test_sort_batches_preserves_other_fields():
    raw = {
        "plan_version": 1,
        "execution_batches": [_batch(2, [10, 20]), _batch(1, [5])],
        "checkpoints": [],
    }
    result = _sort_execution_batches_by_index(raw)
    assert result["plan_version"] == 1
    assert result["checkpoints"] == []
    assert result["execution_batches"][0]["batch_index"] == 1
    assert result["execution_batches"][1]["batch_index"] == 2


# ---------------------------------------------------------------------------
# _ensure_final_checkpoint_stage_closure — existing behaviour is not broken
# ---------------------------------------------------------------------------


def test_stage_closure_added_when_missing():
    raw = {
        "execution_batches": [
            {"batch_index": 1, "checkpoint_id": "cp1"},
        ],
        "checkpoints": [
            {"checkpoint_id": "cp1", "evaluation_focus": ["functional_coverage"]},
        ],
    }
    result = _ensure_final_checkpoint_stage_closure(raw)
    final_cp = next(c for c in result["checkpoints"] if c["checkpoint_id"] == "cp1")
    assert "stage_closure" in final_cp["evaluation_focus"]


def test_stage_closure_not_duplicated_when_already_present():
    raw = {
        "execution_batches": [
            {"batch_index": 1, "checkpoint_id": "cp1"},
        ],
        "checkpoints": [
            {"checkpoint_id": "cp1", "evaluation_focus": ["stage_closure"]},
        ],
    }
    result = _ensure_final_checkpoint_stage_closure(raw)
    final_cp = next(c for c in result["checkpoints"] if c["checkpoint_id"] == "cp1")
    assert final_cp["evaluation_focus"].count("stage_closure") == 1


# ---------------------------------------------------------------------------
# _validate_dependency_ordering
# ---------------------------------------------------------------------------

# Helpers: create lightweight namespace mocks that satisfy attribute access patterns
# used inside _validate_dependency_ordering without instantiating full Pydantic models.


def _ns_batch(batch_index: int, task_ids: list[int]):
    return SimpleNamespace(batch_index=batch_index, task_ids=task_ids)


def _ns_task(task_id: int, title: str, depends_on: list[str] | None = None):
    return SimpleNamespace(
        task_id=task_id,
        title=title,
        depends_on_task_titles=depends_on or [],
    )


def _make_plan_and_input(batches, candidates):
    plan = SimpleNamespace(execution_batches=batches)
    seq_input = SimpleNamespace(candidate_atomic_tasks=candidates)
    return plan, seq_input


def test_validate_dep_ordering_no_deps_returns_empty():
    """Tasks with no depends_on_task_titles produce zero violations."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [1]), _ns_batch(2, [2])],
        candidates=[_ns_task(1, "A"), _ns_task(2, "B")],
    )
    assert _validate_dependency_ordering(plan, seq) == []


def test_validate_dep_ordering_correct_order_returns_empty():
    """B in batch 1, A in batch 2, A depends on B — no violation."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [10]), _ns_batch(2, [20])],
        candidates=[
            _ns_task(10, "B"),
            _ns_task(20, "A", depends_on=["B"]),
        ],
    )
    assert _validate_dependency_ordering(plan, seq) == []


def test_validate_dep_ordering_same_batch_is_violation():
    """A depends on B but both are in batch 1 — one violation expected."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [10, 20])],
        candidates=[
            _ns_task(10, "B"),
            _ns_task(20, "A", depends_on=["B"]),
        ],
    )
    violations = _validate_dependency_ordering(plan, seq)
    assert len(violations) == 1
    assert "A" in violations[0]
    assert "B" in violations[0]


def test_validate_dep_ordering_reversed_order_is_violation():
    """A is in batch 1, B is in batch 2, but A declares B as prerequisite."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [20]), _ns_batch(2, [10])],
        candidates=[
            _ns_task(10, "B"),
            _ns_task(20, "A", depends_on=["B"]),
        ],
    )
    violations = _validate_dependency_ordering(plan, seq)
    assert len(violations) == 1


def test_validate_dep_ordering_title_not_in_candidates_is_silently_skipped():
    """depends_on_task_titles referencing an unknown title produces no violation."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [10])],
        candidates=[_ns_task(10, "A", depends_on=["NonexistentTask"])],
    )
    assert _validate_dependency_ordering(plan, seq) == []


def test_validate_dep_ordering_declaring_task_not_in_plan_is_skipped():
    """A blocked/unscheduled declaring task (not in any batch) is silently skipped."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [10])],
        candidates=[
            _ns_task(10, "B"),
            _ns_task(99, "A", depends_on=["B"]),  # task 99 is not in any batch
        ],
    )
    assert _validate_dependency_ordering(plan, seq) == []


def test_validate_dep_ordering_dep_not_in_plan_is_skipped():
    """If the prerequisite task is not scheduled (blocked), no violation is raised."""
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [20])],
        candidates=[
            _ns_task(10, "B"),  # B is NOT in any batch
            _ns_task(20, "A", depends_on=["B"]),
        ],
    )
    assert _validate_dependency_ordering(plan, seq) == []


def test_validate_dep_ordering_multiple_violations():
    """Two tasks each violate their dependency constraint → two violations."""
    plan, seq = _make_plan_and_input(
        batches=[
            _ns_batch(1, [10, 20, 30, 40]),
        ],
        candidates=[
            _ns_task(10, "Setup"),
            _ns_task(20, "Feature A", depends_on=["Setup"]),
            _ns_task(30, "Feature B", depends_on=["Setup"]),
            _ns_task(40, "Integration"),
        ],
    )
    violations = _validate_dependency_ordering(plan, seq)
    assert len(violations) == 2


def test_validate_dep_ordering_multiple_deps_partial_violation():
    """Task with two deps — one satisfied, one violated → exactly one violation.

    Layout: First=batch1, Combo=batch2, Second=batch3.
    Combo declares both First and Second as prerequisites.
    Combo(2) > First(1) → OK.
    Combo(2) < Second(3) → VIOLATION.
    """
    plan, seq = _make_plan_and_input(
        batches=[_ns_batch(1, [10]), _ns_batch(2, [30]), _ns_batch(3, [20])],
        candidates=[
            _ns_task(10, "First"),  # batch 1 — satisfied prerequisite
            _ns_task(20, "Second"),  # batch 3 — violated prerequisite
            _ns_task(30, "Combo", depends_on=["First", "Second"]),  # batch 2
        ],
    )
    violations = _validate_dependency_ordering(plan, seq)
    assert len(violations) == 1
    assert "Second" in violations[0]
