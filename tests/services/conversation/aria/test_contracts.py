"""Tests for Aria contracts — serialisation/deserialisation of ReviewContext ADT."""

import pytest

from app.services.conversation.aria.contracts import (
    ProjectReviewContext,
    TaskReviewContext,
    classify_workflow_failure,
    review_context_from_json,
    review_context_to_json,
)


class TestTaskReviewContext:
    def test_round_trip(self):
        ctx = TaskReviewContext(
            task_id=42,
            task_title="Implement auth",
            task_description="Use OAuth2",
            validation_notes="SSL error on callback",
        )
        serialised = review_context_to_json(ctx)
        deserialised = review_context_from_json(serialised)

        assert isinstance(deserialised, TaskReviewContext)
        assert deserialised.kind == "task"
        assert deserialised.task_id == 42
        assert deserialised.task_title == "Implement auth"
        assert deserialised.validation_notes == "SSL error on callback"

    def test_optional_fields_none(self):
        ctx = TaskReviewContext(task_id=1, task_title="T")
        serialised = review_context_to_json(ctx)
        deserialised = review_context_from_json(serialised)

        assert isinstance(deserialised, TaskReviewContext)
        assert deserialised.task_description is None
        assert deserialised.validation_notes is None


class TestProjectReviewContext:
    def test_round_trip(self):
        ctx = ProjectReviewContext(
            failure_type="bootstrap",
            failure_reason="Docker image not found",
        )
        serialised = review_context_to_json(ctx)
        deserialised = review_context_from_json(serialised)

        assert isinstance(deserialised, ProjectReviewContext)
        assert deserialised.kind == "project"
        assert deserialised.failure_type == "bootstrap"
        assert deserialised.failure_reason == "Docker image not found"

    def test_all_failure_types(self):
        for ft in ("bootstrap", "plan_empty", "iteration_limit", "unknown"):
            ctx = ProjectReviewContext(failure_type=ft, failure_reason="reason")
            assert review_context_from_json(review_context_to_json(ctx)).failure_type == ft


class TestClassifyWorkflowFailure:
    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("Docker image pull failed", "bootstrap"),
            ("Environment bootstrap error", "bootstrap"),
            ("Entorno no disponible", "bootstrap"),
            ("empty_execution_plan — no batches", "plan_empty"),
            ("Plan vacío sin tareas", "plan_empty"),
            ("Iteration limit exhausted", "iteration_limit"),
            ("Límite de iteraciones alcanzado", "iteration_limit"),
            ("Unexpected error occurred", "unknown"),
        ],
    )
    def test_classification(self, reason: str, expected: str):
        assert classify_workflow_failure(reason) == expected
