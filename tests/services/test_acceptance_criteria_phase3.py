"""
Phase 3 tests — structured acceptance_criteria (list[str]).

Covers:
- format_acceptance_criteria helper (list / str / None inputs)
- PlannedTask and AtomicTaskOutput schema validation (list required, string rejected)
- _validate_atomic_task_quality per-criterion length gate
- validate_task_quality (planner) list-criteria gate
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.task import (
    EXECUTION_ENGINE,
    PLANNING_LEVEL_ATOMIC,
    PLANNING_LEVEL_HIGH_LEVEL,
    Task,
    format_acceptance_criteria,
)
from app.schemas.atomic_task_generator import AtomicTaskOutput
from app.schemas.planner import PlannedTask
from app.services.atomic_task_generator import (
    AtomicTaskGenerationError,
    _validate_atomic_task_quality,
)
from app.services.planner import validate_task_quality

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_task(**overrides) -> Task:
    """Return an in-memory Task with all fields valid for atomic quality check."""
    defaults = dict(
        project_id=1,
        title="Implement user authentication endpoint",
        description="Build the authentication endpoint with proper validation and error handling.",
        summary="Authentication endpoint implementation",
        objective="Deliver a working authentication endpoint that validates JWT tokens",
        proposed_solution="Use FastAPI with OAuth2 and JWT tokens for the authentication flow.",
        implementation_steps="- Step 1: Define the request/response schema\n- Step 2: Implement the route handler",
        acceptance_criteria=[
            "The endpoint returns HTTP 200 for valid credentials",
            "The endpoint returns HTTP 401 for invalid credentials",
        ],
        estimated_complexity="M",
        depends_on_task_titles=[],
        technical_constraints="Python 3.12, FastAPI 0.100+",
        out_of_scope="Frontend UI components",
        planning_level=PLANNING_LEVEL_ATOMIC,
        executor_type=EXECUTION_ENGINE,
        task_type="implementation",
        status="pending",
        priority="medium",
        verification_level="runtime",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _high_level_task(**overrides) -> Task:
    """Return an in-memory Task valid for planner validate_task_quality."""
    defaults = dict(
        project_id=1,
        title="Implement the REST API layer for user management",
        description="Build all CRUD endpoints for user management.",
        summary="User management REST API",
        objective="Deliver fully functional user management endpoints",
        implementation_notes=(
            "Use FastAPI routers; define request/response schemas with Pydantic; "
            "persist data via SQLAlchemy ORM; return standardized JSON responses."
        ),
        acceptance_criteria=[
            "All CRUD endpoints return the documented response shapes",
            "Invalid input returns HTTP 422 with field-level error details",
        ],
        technical_constraints="Python 3.12, FastAPI, PostgreSQL",
        out_of_scope="Frontend components and authentication tokens",
        planning_level=PLANNING_LEVEL_HIGH_LEVEL,
        task_type="implementation",
        status="pending",
        priority="medium",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _minimal_planned_task(**overrides) -> dict:
    """Return keyword args for a valid PlannedTask."""
    defaults = dict(
        title="Implement the REST API layer",
        description="Build all CRUD endpoints for user management with proper validation.",
        summary="User management REST API with CRUD operations",
        objective="Deliver fully functional user management endpoints",
        implementation_notes=(
            "Use FastAPI routers; define Pydantic schemas; persist with SQLAlchemy ORM."
        ),
        acceptance_criteria=[
            "All CRUD endpoints return documented response shapes",
            "Invalid input returns HTTP 422 with field-level details",
        ],
        technical_constraints="Python 3.12, FastAPI",
        out_of_scope="Frontend components",
        priority="medium",
        task_type="implementation",
    )
    defaults.update(overrides)
    return defaults


def _minimal_atomic_task_output(**overrides) -> dict:
    """Return keyword args for a valid AtomicTaskOutput."""
    defaults = dict(
        title="Implement user authentication JWT endpoint",
        description="Build the JWT-based authentication route with token validation.",
        summary="JWT authentication endpoint",
        objective="Deliver a working authentication endpoint with JWT",
        proposed_solution=(
            "Use FastAPI APIRouter with OAuth2PasswordRequestForm; sign JWTs with python-jose."
        ),
        implementation_steps=[
            "Define the /auth/token route in auth_router.py",
            "Implement token signing in jwt_utils.py",
        ],
        tests_required=[
            "Test valid credentials return a signed JWT",
            "Test invalid credentials return 401",
        ],
        acceptance_criteria=[
            "POST /auth/token with valid credentials returns HTTP 200 and a signed JWT",
            "POST /auth/token with wrong password returns HTTP 401",
        ],
        technical_constraints="FastAPI 0.100+, python-jose 3.x",
        out_of_scope="Refresh token rotation",
        priority="high",
        task_type="implementation",
        verification_level="runtime",
        estimated_complexity="M",
        depends_on_task_titles=[],
    )
    defaults.update(overrides)
    return defaults


# ===========================================================================
# format_acceptance_criteria
# ===========================================================================


class TestFormatAcceptanceCriteria:
    def test_none_returns_empty_string(self):
        assert format_acceptance_criteria(None) == ""

    def test_empty_list_returns_empty_string(self):
        assert format_acceptance_criteria([]) == ""

    def test_list_with_items_returns_bullet_lines(self):
        result = format_acceptance_criteria(["criterion A", "criterion B"])
        assert result == "- criterion A\n- criterion B"

    def test_list_strips_whitespace_from_items(self):
        result = format_acceptance_criteria(["  trimmed  ", "  also trimmed  "])
        assert result == "- trimmed\n- also trimmed"

    def test_list_skips_blank_items(self):
        result = format_acceptance_criteria(["valid criterion", "", "   "])
        assert result == "- valid criterion"

    def test_single_item_list_no_trailing_newline(self):
        result = format_acceptance_criteria(["only one criterion here"])
        assert result == "- only one criterion here"
        assert not result.endswith("\n")

    def test_string_passthrough_unchanged(self):
        result = format_acceptance_criteria("Plain string criteria")
        assert result == "Plain string criteria"

    def test_empty_string_returns_empty(self):
        # empty string is falsy, so it returns ""
        assert format_acceptance_criteria("") == ""


# ===========================================================================
# PlannedTask schema — acceptance_criteria is list[str]
# ===========================================================================


class TestPlannedTaskSchema:
    def test_accepts_valid_list(self):
        task = PlannedTask(**_minimal_planned_task())
        assert task.acceptance_criteria == [
            "All CRUD endpoints return documented response shapes",
            "Invalid input returns HTTP 422 with field-level details",
        ]

    def test_rejects_plain_string(self):
        with pytest.raises(ValidationError):
            PlannedTask(**_minimal_planned_task(acceptance_criteria="single string value"))

    def test_rejects_empty_list(self):
        with pytest.raises(ValidationError):
            PlannedTask(**_minimal_planned_task(acceptance_criteria=[]))

    def test_accepts_single_item_list(self):
        task = PlannedTask(
            **_minimal_planned_task(acceptance_criteria=["One concrete verifiable criterion"])
        )
        assert len(task.acceptance_criteria) == 1

    def test_rejects_none(self):
        with pytest.raises(ValidationError):
            PlannedTask(**_minimal_planned_task(acceptance_criteria=None))


# ===========================================================================
# AtomicTaskOutput schema — acceptance_criteria is list[str]
# ===========================================================================


class TestAtomicTaskOutputSchema:
    def test_accepts_valid_list(self):
        output = AtomicTaskOutput(**_minimal_atomic_task_output())
        assert len(output.acceptance_criteria) == 2

    def test_rejects_plain_string(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(
                **_minimal_atomic_task_output(acceptance_criteria="must return 200 for valid input")
            )

    def test_rejects_empty_list(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**_minimal_atomic_task_output(acceptance_criteria=[]))

    def test_accepts_single_item_list(self):
        output = AtomicTaskOutput(
            **_minimal_atomic_task_output(
                acceptance_criteria=["POST /auth/token returns HTTP 200 and signed JWT"]
            )
        )
        assert len(output.acceptance_criteria) == 1

    def test_rejects_none(self):
        with pytest.raises(ValidationError):
            AtomicTaskOutput(**_minimal_atomic_task_output(acceptance_criteria=None))


# ===========================================================================
# _validate_atomic_task_quality — list acceptance_criteria branch
# ===========================================================================


class TestValidateAtomicTaskQualityListCriteria:
    def test_valid_list_passes(self):
        task = _atomic_task()
        _validate_atomic_task_quality([task])  # no exception

    def test_empty_list_raises(self):
        task = _atomic_task(acceptance_criteria=[])
        with pytest.raises(AtomicTaskGenerationError, match="acceptance_criteria empty"):
            _validate_atomic_task_quality([task])

    def test_criterion_below_15_chars_raises(self):
        task = _atomic_task(acceptance_criteria=["Too short", "Valid criterion with enough length"])
        with pytest.raises(AtomicTaskGenerationError, match="too short or non-verifiable"):
            _validate_atomic_task_quality([task])

    def test_generic_short_phrase_raises(self):
        # "tests pass" is 10 chars — below the 15-char floor
        task = _atomic_task(
            acceptance_criteria=["tests pass", "Another valid criterion with enough length"]
        )
        with pytest.raises(AtomicTaskGenerationError, match="too short or non-verifiable"):
            _validate_atomic_task_quality([task])

    def test_exactly_15_chars_passes(self):
        # 15 chars: "123456789012345"
        task = _atomic_task(
            acceptance_criteria=["123456789012345", "A second criterion that is valid"]
        )
        _validate_atomic_task_quality([task])  # no exception

    def test_multiple_valid_criteria_all_pass(self):
        task = _atomic_task(
            acceptance_criteria=[
                "The endpoint returns HTTP 200 for valid input",
                "Invalid credentials produce HTTP 401 with error body",
                "Response body contains a signed JWT access_token field",
            ]
        )
        _validate_atomic_task_quality([task])  # no exception

    def test_legacy_string_criteria_passes_when_long_enough(self):
        # Technical refiner still produces str; must not break atomic validation
        task = _atomic_task(
            acceptance_criteria="All endpoints return the expected JSON responses and status codes."
        )
        _validate_atomic_task_quality([task])  # no exception

    def test_legacy_string_criteria_too_short_raises(self):
        task = _atomic_task(acceptance_criteria="Short")
        with pytest.raises(AtomicTaskGenerationError, match="acceptance_criteria too short"):
            _validate_atomic_task_quality([task])


# ===========================================================================
# validate_task_quality (planner) — list acceptance_criteria branch
# ===========================================================================


class TestValidateTaskQualityListCriteria:
    def test_valid_list_passes(self):
        task = _high_level_task()
        validate_task_quality([task])  # no exception

    def test_empty_list_raises(self):
        task = _high_level_task(acceptance_criteria=[])
        with pytest.raises(ValueError, match="acceptance_criteria too short"):
            validate_task_quality([task])

    def test_list_total_chars_below_30_raises(self):
        # Two items, each short — total < 30 chars
        task = _high_level_task(acceptance_criteria=["a", "b"])
        with pytest.raises(ValueError, match="acceptance_criteria too short"):
            validate_task_quality([task])

    def test_list_with_sufficient_total_chars_passes(self):
        # Individual items can be short, what matters is sum >= 30
        task = _high_level_task(
            acceptance_criteria=[
                "All endpoints return documented responses",
            ]
        )
        validate_task_quality([task])  # no exception

    def test_legacy_string_passes_when_long_enough(self):
        task = _high_level_task(
            acceptance_criteria="All CRUD endpoints return correct responses and status codes."
        )
        validate_task_quality([task])  # no exception

    def test_legacy_string_too_short_raises(self):
        task = _high_level_task(acceptance_criteria="Too short")
        with pytest.raises(ValueError, match="acceptance_criteria too short"):
            validate_task_quality([task])
