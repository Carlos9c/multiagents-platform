"""
Unit tests for atomic_task_generator_client — Phase 5 separation rules.

These tests verify that the generator's system prompt and user prompt carry the
implementation vs. testing separation guidance introduced in Phase 5.  They are
structural (prompt-content) tests: they check that the right *concepts* are
present so that regressions to the guidance text are caught early.
"""

from __future__ import annotations

from app.services.atomic_task_generator_client import (
    ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT,
    build_atomic_user_prompt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_prompt(**overrides) -> str:
    defaults = dict(
        project_name="My Project",
        project_description="A sample project.",
        parent_task_title="Implement and test user service",
        parent_task_description="Build the user service and write unit tests for it.",
        parent_task_summary="User service with tests.",
        parent_task_objective="Deliver a working user service.",
        parent_task_type="implementation",
        parent_task_planning_level="refined",
        parent_task_proposed_solution="Use FastAPI.",
        parent_task_implementation_steps="- Step 1\n- Step 2",
        parent_task_acceptance_criteria="Service responds correctly.",
        parent_task_tests_required="- Unit test each endpoint",
        parent_task_technical_constraints="Python 3.12",
        parent_task_out_of_scope="Auth",
        available_executors=["execution_engine"],
    )
    defaults.update(overrides)
    return build_atomic_user_prompt(**defaults)


# ---------------------------------------------------------------------------
# System prompt — separation rule present
# ---------------------------------------------------------------------------


def test_system_prompt_contains_separation_section():
    """The system prompt must contain the impl vs. testing separation rule section."""
    assert "Implementation vs. testing separation rule" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT


def test_system_prompt_impl_tasks_must_not_write_test_files():
    """The system prompt must state that implementation tasks must NOT produce test files."""
    # Check the concept is there
    assert "must NOT produce test files" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT


def test_system_prompt_testing_tasks_must_not_produce_source_code():
    """The system prompt must state that testing tasks must NOT produce implementation files."""
    assert (
        "must NOT produce implementation or source code files"
        in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT
    )


def test_system_prompt_split_guidance_for_mixed_parent_tasks():
    """The prompt must tell the LLM to split tasks that mix source and test deliverables."""
    prompt = ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT
    assert "split them into separate atomic tasks" in prompt
    # Both the impl side and test side of the split should be mentioned
    assert 'task_type="implementation"' in prompt
    assert 'task_type="testing"' in prompt


def test_system_prompt_testing_task_must_identify_what_it_tests():
    """A testing task must clearly name what modules/behaviors it tests."""
    assert "clearly identify" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT


def test_system_prompt_verification_level_runtime_for_tests_that_must_pass():
    """'runtime' must be prescribed for testing tasks where tests must execute and pass."""
    # The separation section links runtime to executing and producing a passing result.
    # The phrase spans two lines in the source so we test the key fragments independently.
    assert "passing result" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT
    assert (
        'Assign "runtime" when the acceptance_criteria require tests to execute'
        in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT
    )


def test_system_prompt_verification_level_none_for_scaffolding_only():
    """'none' must be scoped to scaffolding / files-only test tasks."""
    assert "scaffolding" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# System prompt — task type assignment rules still present
# ---------------------------------------------------------------------------


def test_system_prompt_task_type_testing_still_defined():
    """The task_type assignment rules section must still define 'testing'."""
    assert "testing: primary output is test files" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT


def test_system_prompt_task_type_implementation_still_defined():
    """The task_type assignment rules section must still define 'implementation'."""
    assert "implementation:" in ATOMIC_TASK_GENERATOR_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# User prompt — separation guidance reinforced
# ---------------------------------------------------------------------------


def test_user_prompt_reinforces_impl_test_separation():
    """The user prompt must explicitly tell the LLM to split mixed impl+test parent tasks."""
    prompt = _user_prompt()
    assert "test file" in prompt.lower() or "testing tasks" in prompt.lower()
    assert "split" in prompt.lower()


def test_user_prompt_includes_executor_capabilities():
    """The user prompt must embed the executor capability catalog, which includes
    test_builder_agent limits (does not write source code) and code_change_agent
    limits (does not write test files)."""
    prompt = _user_prompt()
    assert "test_builder_agent" in prompt
    assert "code_change_agent" in prompt


def test_user_prompt_impl_tasks_must_not_write_test_files():
    """The user prompt explicitly restates the impl / testing separation constraint."""
    prompt = _user_prompt()
    # The added line in build_atomic_user_prompt
    assert "implementation tasks must not write test files" in prompt.lower()


def test_user_prompt_split_when_parent_mixes_both():
    """The user prompt's split-when list still contains the mixed-scope case."""
    prompt = _user_prompt()
    # Generic split guidance must still be present
    assert "split when" in prompt.lower()
