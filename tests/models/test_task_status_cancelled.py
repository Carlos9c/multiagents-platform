"""Tests for TASK_STATUS_CANCELLED and the Phase 1 schema additions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.task import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TERMINAL_TASK_STATUSES,
    VALID_TASK_STATUSES,
)
from app.services.conversation.impact_assessment_agent import (
    NewWorkBlock,
    TaskContextForReview,
    TaskRevisionSpec,
)


class TestCancelledStatus:
    def test_cancelled_is_in_terminal_statuses(self):
        assert TASK_STATUS_CANCELLED in TERMINAL_TASK_STATUSES

    def test_cancelled_is_in_valid_statuses(self):
        assert TASK_STATUS_CANCELLED in VALID_TASK_STATUSES

    def test_cancelled_distinct_from_completed(self):
        assert TASK_STATUS_CANCELLED != TASK_STATUS_COMPLETED

    def test_cancelled_value(self):
        assert TASK_STATUS_CANCELLED == "cancelled"

    def test_all_terminal_statuses_present(self):
        expected = {
            "partial",
            "completed",
            "failed",
            "reatomized",
            "followed_up",
            "superseded",
            "cancelled",
        }
        assert TERMINAL_TASK_STATUSES == expected


class TestTaskContextForReview:
    def test_construct_minimal(self):
        ctx = TaskContextForReview(
            task_id=1,
            title="Setup DB",
            description="Configure PostgreSQL",
            planning_level="atomic",
            parent_task_id=None,
            parent_title=None,
            status="pending",
            depends_on_task_titles=[],
            acceptance_criteria=None,
            sequence_order=1,
        )
        assert ctx.task_id == 1
        assert ctx.depends_on_task_titles == []

    def test_construct_with_parent(self):
        ctx = TaskContextForReview(
            task_id=5,
            title="Write migrations",
            description=None,
            planning_level="atomic",
            parent_task_id=2,
            parent_title="Database layer",
            status="pending",
            depends_on_task_titles=["Setup DB"],
            acceptance_criteria="Migration runs without error",
            sequence_order=3,
        )
        assert ctx.parent_task_id == 2
        assert ctx.parent_title == "Database layer"
        assert "Setup DB" in ctx.depends_on_task_titles


class TestTaskRevisionSpecExtended:
    def test_new_depends_on_task_titles_accepted(self):
        spec = TaskRevisionSpec(
            task_id=6,
            new_description="Updated desc",
            new_objective=None,
            new_implementation_steps=None,
            new_acceptance_criteria=None,
            new_technical_constraints=None,
            new_depends_on_task_titles=["Setup DB", "Configure cache"],
            reason="Dependencies changed",
        )
        assert spec.new_depends_on_task_titles == ["Setup DB", "Configure cache"]

    def test_new_depends_on_task_titles_nullable(self):
        spec = TaskRevisionSpec(
            task_id=6,
            new_description=None,
            new_objective=None,
            new_implementation_steps=None,
            new_acceptance_criteria=None,
            new_technical_constraints=None,
            new_depends_on_task_titles=None,
            reason="Only description changed",
        )
        assert spec.new_depends_on_task_titles is None


class TestNewWorkBlock:
    def _make_block(self, **overrides) -> dict:
        base = dict(
            title="Implement caching layer",
            description="Add Redis-based caching",
            objective="Reduce DB load",
            proposed_solution="Use Redis with TTL keys",
            acceptance_criteria="Cache hit rate > 70%",
            technical_constraints="Redis 7+",
            out_of_scope="Persistent storage",
            task_type="implementation",
            depends_on_task_titles=["Setup API"],
            planning_level="high_level",
            parent_task_id=None,
            reason="User requested caching",
        )
        base.update(overrides)
        return base

    def test_high_level_no_parent(self):
        block = NewWorkBlock(**self._make_block())
        assert block.planning_level == "high_level"
        assert block.parent_task_id is None

    def test_atomic_with_parent(self):
        block = NewWorkBlock(**self._make_block(planning_level="atomic", parent_task_id=3))
        assert block.planning_level == "atomic"
        assert block.parent_task_id == 3

    def test_invalid_planning_level(self):
        with pytest.raises(ValidationError):
            NewWorkBlock(**self._make_block(planning_level="refined"))

    def test_depends_on_empty_list(self):
        block = NewWorkBlock(**self._make_block(depends_on_task_titles=[]))
        assert block.depends_on_task_titles == []
