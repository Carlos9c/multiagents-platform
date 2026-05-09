from __future__ import annotations

from unittest.mock import MagicMock

from app.execution_engine.subagents.context_selection_agent import (
    _build_historical_task_selection_base_prompt,
    _build_historical_task_selection_user_prompt,
)


def make_task_mock(task_id: int = 1) -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = "Implement the feature module"
    task.description = "Build the feature."
    task.summary = "Feature module."
    task.objective = "Have the feature working."
    task.proposed_solution = None
    task.implementation_notes = "Use the existing patterns."
    task.implementation_steps = None
    task.acceptance_criteria = "Tests pass."
    task.tests_required = None
    task.technical_constraints = "Must use SQLAlchemy."
    task.out_of_scope = "Frontend."
    task.task_type = "implementation"
    task.executor_type = "execution_engine"
    return task


# ── codebase section present ─────────────────────────────────────────────────


def test_prompt_includes_codebase_section_when_excerpt_provided():
    task = make_task_mock()
    prompt = _build_historical_task_selection_base_prompt(
        current_task=task,
        catalog=[],
        project_name="MyProject",
        project_description="A test project.",
        codebase_analysis_excerpt="Project summary: A test project.\nMain technologies: Python",
    )
    assert "Codebase structure" in prompt
    assert "Project summary: A test project." in prompt
    assert "Main technologies: Python" in prompt


def test_prompt_uses_fallback_when_no_analysis_excerpt():
    task = make_task_mock()
    prompt = _build_historical_task_selection_base_prompt(
        current_task=task,
        catalog=[],
        project_name="MyProject",
        project_description="A test project.",
        codebase_analysis_excerpt=None,
    )
    assert "No static analysis available" in prompt


def test_codebase_section_appears_before_current_task():
    task = make_task_mock()
    prompt = _build_historical_task_selection_base_prompt(
        current_task=task,
        catalog=[],
        project_name="MyProject",
        project_description="A test project.",
        codebase_analysis_excerpt="Main technologies: Python",
    )
    codebase_pos = prompt.index("Codebase structure")
    task_pos = prompt.index("Current atomic task")
    assert codebase_pos < task_pos


def test_project_context_excerpt_still_rendered():
    task = make_task_mock()
    prompt = _build_historical_task_selection_base_prompt(
        current_task=task,
        catalog=[],
        project_name="MyProject",
        project_description="A test project.",
        project_context_excerpt="Recent: deployed auth module.",
        codebase_analysis_excerpt="Main technologies: Python",
    )
    assert "Recent: deployed auth module." in prompt


def test_project_context_excerpt_none_shows_none():
    task = make_task_mock()
    prompt = _build_historical_task_selection_base_prompt(
        current_task=task,
        catalog=[],
        project_name="MyProject",
        project_description="A test project.",
        project_context_excerpt=None,
        codebase_analysis_excerpt=None,
    )
    assert "Project context excerpt:\nNone" in prompt


# ── user-prompt wrapper ───────────────────────────────────────────────────────


def test_user_prompt_passes_analysis_excerpt_through():
    task = make_task_mock()
    prompt = _build_historical_task_selection_user_prompt(
        current_task=task,
        catalog=[],
        project_name="MyProject",
        project_description="A test project.",
        codebase_analysis_excerpt="FastAPI, SQLAlchemy",
    )
    assert "FastAPI, SQLAlchemy" in prompt
