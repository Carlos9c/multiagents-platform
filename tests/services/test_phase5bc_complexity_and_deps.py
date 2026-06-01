"""
Phase 5B/5C tests — estimated_complexity and depends_on_task_titles.

Covers:
- AtomicTaskOutput schema: new required fields validated correctly
- Task model: new columns stored and round-trip through SQLite JSON
- CandidateAtomicTask: new fields present in the schema
- _build_candidate_atomic_task: threads new fields from Task to candidate
- execution_plan_service: depends_on_task_titles defaults to [] when Task has None
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.task import EXECUTION_ENGINE, PLANNING_LEVEL_ATOMIC, PLANNING_LEVEL_HIGH_LEVEL
from app.schemas.atomic_task_generator import AtomicTaskOutput, ComplexityLevel
from app.schemas.execution_plan import CandidateAtomicTask
from app.services.execution_plan_service import _build_candidate_atomic_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_atomic_output(**overrides) -> dict:
    defaults = dict(
        title="Implement database migration for users table",
        description="Write and apply the Alembic migration for the users table schema.",
        summary="Users table Alembic migration",
        objective="Produce the Alembic migration file and apply it successfully",
        proposed_solution=(
            "Use Alembic op.create_table() with SQLAlchemy column definitions; "
            "run alembic upgrade head inside the Docker container."
        ),
        implementation_steps=[
            "Define the users table schema in a new Alembic version file",
            "Apply the migration with alembic upgrade head",
        ],
        tests_required=[
            "Verify the users table exists after migration",
            "Verify all declared columns are present with correct types",
        ],
        acceptance_criteria=[
            "The users table is present in the database after running alembic upgrade head",
            "All declared columns exist with the specified types and constraints",
        ],
        technical_constraints="Alembic 1.x, PostgreSQL 15+",
        out_of_scope="Seed data insertion",
        priority="high",
        task_type="implementation",
        verification_level="runtime",
        estimated_complexity="S",
        depends_on_task_titles=[],
    )
    defaults.update(overrides)
    return defaults


# ===========================================================================
# AtomicTaskOutput schema — estimated_complexity
# ===========================================================================


class TestAtomicTaskOutputComplexity:
    def test_all_valid_complexity_levels_accepted(self):
        for level in ("XS", "S", "M", "L", "XL"):
            output = AtomicTaskOutput(**_minimal_atomic_output(estimated_complexity=level))
            assert output.estimated_complexity == level

    def test_invalid_complexity_level_rejected(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**_minimal_atomic_output(estimated_complexity="XXL"))

    def test_lowercase_complexity_rejected(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**_minimal_atomic_output(estimated_complexity="m"))

    def test_missing_complexity_rejected(self):
        data = _minimal_atomic_output()
        del data["estimated_complexity"]
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**data)

    def test_none_complexity_rejected(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**_minimal_atomic_output(estimated_complexity=None))

    def test_complexity_stored_on_model(self):
        output = AtomicTaskOutput(**_minimal_atomic_output(estimated_complexity="L"))
        assert output.estimated_complexity == "L"


# ===========================================================================
# AtomicTaskOutput schema — depends_on_task_titles
# ===========================================================================


class TestAtomicTaskOutputDependencies:
    def test_empty_list_accepted(self):
        output = AtomicTaskOutput(**_minimal_atomic_output(depends_on_task_titles=[]))
        assert output.depends_on_task_titles == []

    def test_list_with_titles_accepted(self):
        output = AtomicTaskOutput(
            **_minimal_atomic_output(
                depends_on_task_titles=[
                    "Configure Docker environment",
                    "Define SQLAlchemy base model",
                ]
            )
        )
        assert len(output.depends_on_task_titles) == 2

    def test_missing_field_rejected(self):
        data = _minimal_atomic_output()
        del data["depends_on_task_titles"]
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**data)

    def test_none_field_rejected(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**_minimal_atomic_output(depends_on_task_titles=None))

    def test_plain_string_rejected(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(
                **_minimal_atomic_output(depends_on_task_titles="Configure Docker environment")
            )


# ===========================================================================
# ComplexityLevel type export
# ===========================================================================


def test_complexity_level_type_is_literal():
    """ComplexityLevel is importable and covers exactly five values."""
    import typing

    args = typing.get_args(ComplexityLevel)
    assert set(args) == {"XS", "S", "M", "L", "XL"}


# ===========================================================================
# Task model — new columns persist and round-trip through SQLite JSON
# ===========================================================================


class TestTaskModelNewColumns:
    def test_estimated_complexity_stored_and_retrieved(self, db_session, make_project):
        from app.models.task import Task

        project = make_project()
        task = Task(
            project_id=project.id,
            title="Implement JWT authentication",
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=EXECUTION_ENGINE,
            task_type="implementation",
            status="pending",
            priority="medium",
            estimated_complexity="L",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        assert task.estimated_complexity == "L"

    def test_depends_on_task_titles_stored_as_list(self, db_session, make_project):
        from app.models.task import Task

        project = make_project()
        task = Task(
            project_id=project.id,
            title="Implement API endpoints",
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=EXECUTION_ENGINE,
            task_type="implementation",
            status="pending",
            priority="medium",
            depends_on_task_titles=["Set up Docker environment", "Apply database migration"],
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        assert task.depends_on_task_titles == [
            "Set up Docker environment",
            "Apply database migration",
        ]

    def test_depends_on_task_titles_empty_list_stored(self, db_session, make_project):
        from app.models.task import Task

        project = make_project()
        task = Task(
            project_id=project.id,
            title="Bootstrap project scaffold",
            planning_level=PLANNING_LEVEL_ATOMIC,
            executor_type=EXECUTION_ENGINE,
            task_type="configuration",
            status="pending",
            priority="high",
            depends_on_task_titles=[],
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        assert task.depends_on_task_titles == []

    def test_new_columns_nullable_for_existing_rows(self, db_session, make_project, make_task):
        """Tasks created without new fields default to None — no migration breakage."""
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)

        db_session.refresh(task)
        assert task.estimated_complexity is None
        assert task.depends_on_task_titles is None


# ===========================================================================
# CandidateAtomicTask schema — new fields present
# ===========================================================================


class TestCandidateAtomicTaskSchema:
    def _make_candidate(self, **overrides) -> CandidateAtomicTask:
        defaults = dict(
            task_id=1,
            title="Implement users REST endpoint",
            task_type="implementation",
            priority="medium",
            planning_level="atomic",
            executor_type=EXECUTION_ENGINE,
            status="pending",
            estimated_complexity="M",
            depends_on_task_titles=["Apply database migration"],
        )
        defaults.update(overrides)
        return CandidateAtomicTask(**defaults)

    def test_new_fields_accepted(self):
        candidate = self._make_candidate()
        assert candidate.estimated_complexity == "M"
        assert candidate.depends_on_task_titles == ["Apply database migration"]

    def test_estimated_complexity_defaults_to_none(self):
        candidate = self._make_candidate(estimated_complexity=None)
        assert candidate.estimated_complexity is None

    def test_depends_on_task_titles_defaults_to_empty_list(self):
        candidate = CandidateAtomicTask(
            task_id=1,
            title="Bootstrap project",
            task_type="configuration",
            priority="high",
            planning_level="atomic",
            executor_type=EXECUTION_ENGINE,
            status="pending",
        )
        assert candidate.depends_on_task_titles == []

    def test_depends_on_task_titles_with_multiple_entries(self):
        candidate = self._make_candidate(depends_on_task_titles=["Task A", "Task B", "Task C"])
        assert len(candidate.depends_on_task_titles) == 3


# ===========================================================================
# _build_candidate_atomic_task — threads new fields from Task to candidate
# ===========================================================================


class TestBuildCandidateAtomicTask:
    def test_threads_estimated_complexity(self, db_session, make_project, make_task):
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        task.estimated_complexity = "XL"
        db_session.commit()
        db_session.refresh(task)

        candidate = _build_candidate_atomic_task(task, parent_task=None)
        assert candidate.estimated_complexity == "XL"

    def test_threads_depends_on_task_titles(self, db_session, make_project, make_task):
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        task.depends_on_task_titles = ["Configure environment", "Apply migrations"]
        db_session.commit()
        db_session.refresh(task)

        candidate = _build_candidate_atomic_task(task, parent_task=None)
        assert candidate.depends_on_task_titles == [
            "Configure environment",
            "Apply migrations",
        ]

    def test_none_depends_on_becomes_empty_list(self, db_session, make_project, make_task):
        """Task with depends_on_task_titles=None → candidate gets []."""
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        assert task.depends_on_task_titles is None

        candidate = _build_candidate_atomic_task(task, parent_task=None)
        assert candidate.depends_on_task_titles == []

    def test_none_complexity_passed_through(self, db_session, make_project, make_task):
        """Older tasks without complexity set → candidate gets None."""
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        assert task.estimated_complexity is None

        candidate = _build_candidate_atomic_task(task, parent_task=None)
        assert candidate.estimated_complexity is None

    def test_parent_context_still_works_with_new_fields(self, db_session, make_project, make_task):
        """New fields don't break parent_task hierarchy resolution."""
        project = make_project()
        parent = make_task(
            project_id=project.id,
            planning_level=PLANNING_LEVEL_HIGH_LEVEL,
            title="Build REST API layer",
        )
        child = make_task(
            project_id=project.id,
            planning_level=PLANNING_LEVEL_ATOMIC,
            parent_task_id=parent.id,
        )
        child.estimated_complexity = "S"
        child.depends_on_task_titles = []
        db_session.commit()
        db_session.refresh(child)

        candidate = _build_candidate_atomic_task(child, parent_task=parent)
        assert candidate.parent_high_level_title == "Build REST API layer"
        assert candidate.estimated_complexity == "S"
        assert candidate.depends_on_task_titles == []


class TestBuildCandidateAtomicTaskAcceptanceCriteria:
    def test_list_acceptance_criteria_serialised_to_string(
        self, db_session, make_project, make_task
    ):
        """Phase 3: Task.acceptance_criteria is list[str] — candidate must receive str."""
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        task.acceptance_criteria = [
            "The endpoint returns HTTP 200 for valid credentials",
            "The endpoint returns HTTP 401 for invalid credentials",
        ]
        db_session.commit()
        db_session.refresh(task)

        candidate = _build_candidate_atomic_task(task, parent_task=None)

        assert isinstance(candidate.acceptance_criteria, str)
        assert "HTTP 200" in candidate.acceptance_criteria
        assert "HTTP 401" in candidate.acceptance_criteria

    def test_none_acceptance_criteria_stays_none(self, db_session, make_project, make_task):
        """Task with no acceptance_criteria → candidate gets None (not empty string)."""
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        task.acceptance_criteria = None
        db_session.commit()
        db_session.refresh(task)

        candidate = _build_candidate_atomic_task(task, parent_task=None)
        assert candidate.acceptance_criteria is None

    def test_string_acceptance_criteria_passed_through(self, db_session, make_project, make_task):
        """Legacy str acceptance_criteria → candidate receives it unchanged."""
        project = make_project()
        task = make_task(project_id=project.id, planning_level=PLANNING_LEVEL_ATOMIC)
        task.acceptance_criteria = "All endpoints return correct responses."
        db_session.commit()
        db_session.refresh(task)

        candidate = _build_candidate_atomic_task(task, parent_task=None)
        assert candidate.acceptance_criteria == "All endpoints return correct responses."
