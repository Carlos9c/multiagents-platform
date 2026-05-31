"""
Tests for runtime_context injection in planning agent prompts (Phase 1).

Verifies that when a RuntimeSpec-derived context block is provided, it
appears in the user prompts of the planner, technical_task_refiner, and
atomic_task_generator clients.  Also tests _format_runtime_context.
"""

from __future__ import annotations

from app.services.atomic_task_generator_client import build_atomic_user_prompt
from app.services.planner_client import (
    build_evolutionary_planner_user_prompt,
    build_planner_user_prompt,
)
from app.services.technical_task_refiner_client import build_refiner_user_prompt

_RUNTIME_CONTEXT = (
    "Runtime context:\n"
    "- runtime_type: python_venv\n"
    "- image: python:3.12-slim\n"
    "- key dependencies: fastapi==0.115.0, sqlalchemy==2.0.30, pytest==8.2.0"
)

# ---------------------------------------------------------------------------
# _format_runtime_context
# ---------------------------------------------------------------------------


def test_format_runtime_context_standard():
    from unittest.mock import MagicMock

    from app.services.project_workflow_service import _format_runtime_context

    dep = MagicMock()
    dep.name = "fastapi"
    dep.version = "0.115.0"

    spec = MagicMock()
    spec.runtime_type = "python_venv"
    spec.image = "python:3.12-slim"
    spec.dependencies = [dep]

    result = _format_runtime_context(spec)
    assert "Runtime context:" in result
    assert "python_venv" in result
    assert "python:3.12-slim" in result
    assert "fastapi==0.115.0" in result


def test_format_runtime_context_no_dependencies():
    from unittest.mock import MagicMock

    from app.services.project_workflow_service import _format_runtime_context

    spec = MagicMock()
    spec.runtime_type = "android_gradle"
    spec.image = "reactnative:latest"
    spec.dependencies = []

    result = _format_runtime_context(spec)
    assert "build-system managed" in result


def test_format_runtime_context_truncates_at_12_deps():
    from unittest.mock import MagicMock

    from app.services.project_workflow_service import _format_runtime_context

    def _dep(name: str) -> MagicMock:
        d = MagicMock()
        d.name = name
        d.version = "1.0.0"
        return d

    spec = MagicMock()
    spec.runtime_type = "python_venv"
    spec.image = "python:3.12-slim"
    spec.dependencies = [_dep(f"pkg{i}") for i in range(15)]

    result = _format_runtime_context(spec)
    assert "15 total" in result
    assert "pkg12" not in result  # 13th dep should not appear


# ---------------------------------------------------------------------------
# build_planner_user_prompt
# ---------------------------------------------------------------------------


def test_planner_prompt_includes_runtime_context_when_provided():
    prompt = build_planner_user_prompt(
        project_name="API Project",
        project_description="Build a REST API",
        runtime_context=_RUNTIME_CONTEXT,
    )
    assert "Runtime context:" in prompt
    assert "python_venv" in prompt
    assert "fastapi==0.115.0" in prompt


def test_planner_prompt_omits_runtime_context_when_none():
    prompt = build_planner_user_prompt(
        project_name="API Project",
        project_description="Build a REST API",
        runtime_context=None,
    )
    assert "Runtime context:" not in prompt


def test_planner_prompt_still_contains_project_info():
    prompt = build_planner_user_prompt(
        project_name="My Project",
        project_description="Build something",
        runtime_context=_RUNTIME_CONTEXT,
    )
    assert "My Project" in prompt
    assert "Build something" in prompt


# ---------------------------------------------------------------------------
# build_evolutionary_planner_user_prompt
# ---------------------------------------------------------------------------


def test_evolutionary_planner_prompt_includes_runtime_context():
    from unittest.mock import MagicMock

    analysis = MagicMock()
    analysis.project_summary = "Existing API"
    analysis.main_technologies = ["Python", "FastAPI"]
    analysis.entry_points = ["main.py"]
    analysis.total_files = 10
    analysis.files = []

    prompt = build_evolutionary_planner_user_prompt(
        project_name="Extend API",
        project_description="Add new endpoints",
        analysis=analysis,
        runtime_context=_RUNTIME_CONTEXT,
    )
    assert "Runtime context:" in prompt
    assert "python_venv" in prompt


def test_evolutionary_planner_prompt_omits_runtime_context_when_none():
    from unittest.mock import MagicMock

    analysis = MagicMock()
    analysis.project_summary = "Existing API"
    analysis.main_technologies = ["Python"]
    analysis.entry_points = []
    analysis.total_files = 5
    analysis.files = []

    prompt = build_evolutionary_planner_user_prompt(
        project_name="Extend API",
        project_description="Add new endpoints",
        analysis=analysis,
        runtime_context=None,
    )
    assert "Runtime context:" not in prompt


# ---------------------------------------------------------------------------
# build_refiner_user_prompt
# ---------------------------------------------------------------------------

_REFINER_DEFAULTS = dict(
    project_name="My Project",
    project_description="A project",
    parent_task_title="Build the API",
    parent_task_description="Create REST endpoints",
    parent_task_summary="API implementation",
    parent_task_objective="Deliver endpoints",
    parent_task_type="implementation",
    parent_task_implementation_notes="Use FastAPI and SQLAlchemy",
    parent_task_acceptance_criteria="All endpoints return 200",
    parent_task_technical_constraints="Python 3.12",
    parent_task_out_of_scope="Authentication",
)


def test_refiner_prompt_includes_runtime_context_when_provided():
    prompt = build_refiner_user_prompt(
        **_REFINER_DEFAULTS,
        runtime_context=_RUNTIME_CONTEXT,
    )
    assert "Runtime context:" in prompt
    assert "python_venv" in prompt
    assert "fastapi==0.115.0" in prompt


def test_refiner_prompt_omits_runtime_context_when_none():
    prompt = build_refiner_user_prompt(
        **_REFINER_DEFAULTS,
        runtime_context=None,
    )
    assert "Runtime context:" not in prompt


# ---------------------------------------------------------------------------
# build_atomic_user_prompt
# ---------------------------------------------------------------------------

_ATOMIC_DEFAULTS = dict(
    project_name="My Project",
    project_description="A project",
    parent_task_title="Implement user endpoint",
    parent_task_description="Create user CRUD",
    parent_task_summary="User CRUD",
    parent_task_objective="Deliver working endpoints",
    parent_task_type="implementation",
    parent_task_planning_level="refined",
    parent_task_proposed_solution="Use FastAPI Router",
    parent_task_implementation_steps="- Step 1\n- Step 2",
    parent_task_acceptance_criteria="Endpoints return correct status",
    parent_task_tests_required="- Test each endpoint",
    parent_task_technical_constraints="Python 3.12",
    parent_task_out_of_scope="Auth",
    available_executors=["execution_engine"],
)


def test_atomic_prompt_includes_runtime_context_when_provided():
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        runtime_context=_RUNTIME_CONTEXT,
    )
    assert "Runtime context:" in prompt
    assert "python_venv" in prompt
    assert "fastapi==0.115.0" in prompt


def test_atomic_prompt_omits_runtime_context_when_none():
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        runtime_context=None,
    )
    assert "Runtime context:" not in prompt


def test_atomic_prompt_runtime_context_appears_before_parent_task():
    """runtime_context block must appear near the top, before the parent task section."""
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        runtime_context=_RUNTIME_CONTEXT,
    )
    runtime_pos = prompt.index("Runtime context:")
    parent_pos = prompt.index("Parent task to atomize:")
    assert runtime_pos < parent_pos


# ---------------------------------------------------------------------------
# build_atomic_user_prompt — sibling_atomic_summary (Phase 2)
# ---------------------------------------------------------------------------

_SIBLINGS = [
    {"title": "Set up database models", "task_type": "implementation"},
    {"title": "Write tests for database models", "task_type": "testing"},
]


def test_atomic_prompt_includes_sibling_section_when_provided():
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        sibling_atomic_summary=_SIBLINGS,
    )
    assert "Already-generated atomic tasks" in prompt
    assert "Set up database models" in prompt
    assert "Write tests for database models" in prompt
    assert "(implementation)" in prompt
    assert "(testing)" in prompt


def test_atomic_prompt_omits_sibling_section_when_none():
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        sibling_atomic_summary=None,
    )
    assert "Already-generated atomic tasks" not in prompt


def test_atomic_prompt_omits_sibling_section_when_empty_list():
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        sibling_atomic_summary=[],
    )
    assert "Already-generated atomic tasks" not in prompt


def test_atomic_prompt_sibling_section_appears_before_parent_task():
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        sibling_atomic_summary=_SIBLINGS,
    )
    sibling_pos = prompt.index("Already-generated atomic tasks")
    parent_pos = prompt.index("Parent task to atomize:")
    assert sibling_pos < parent_pos


def test_atomic_prompt_runtime_context_appears_before_sibling_section():
    """When both are present, runtime_context must appear before sibling_atomic_summary."""
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        runtime_context=_RUNTIME_CONTEXT,
        sibling_atomic_summary=_SIBLINGS,
    )
    runtime_pos = prompt.index("Runtime context:")
    sibling_pos = prompt.index("Already-generated atomic tasks")
    assert runtime_pos < sibling_pos


def test_atomic_prompt_sibling_section_no_runtime_context():
    """Sibling section appears even without runtime_context."""
    prompt = build_atomic_user_prompt(
        **_ATOMIC_DEFAULTS,
        runtime_context=None,
        sibling_atomic_summary=_SIBLINGS,
    )
    assert "Already-generated atomic tasks" in prompt
    assert "Runtime context:" not in prompt
