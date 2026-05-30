"""
Tests for the supervisor synthesizer.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.supervisor.supervisor_synthesizer import synthesize

_MODULE = "app.services.supervisor.supervisor_synthesizer"

_SYNTHESIS_TEXT = (
    "## Overall Verdict\n\n"
    "The project is generally healthy with minor issues in the execution layer.\n\n"
    "## Healthy Agents\n\nAll planning agents performed well."
)

_AGENT_RESULTS_MIXED = [
    {
        "agent_name": "planner",
        "verdict": "healthy",
        "findings": "Planning was well-scoped.",
        "issues": [],
        "suggestions": [],
    },
    {
        "agent_name": "orchestrator",
        "verdict": "degraded",
        "findings": "Repeated budget exhaustion detected.",
        "issues": ["Budget exhausted in 4/5 runs"],
        "suggestions": ["Increase max_steps"],
    },
    {
        "agent_name": "context_selection_agent",
        "verdict": "needs_attention",
        "findings": "Context selection was sometimes irrelevant.",
        "issues": ["Selected stale context in 2 runs"],
        "suggestions": ["Improve context scoring"],
    },
    {
        "agent_name": "environment_manager_agent",
        "verdict": None,
        "findings": None,
        "issues": [],
        "suggestions": [],
    },
]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_synthesize_returns_string():
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {"synthesis": _SYNTHESIS_TEXT}

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        result = synthesize(
            project_id=1,
            project_name="Test Project",
            project_description="A test project for unit tests.",
            requirements_draft="Build a REST API.",
            overall_verdict="needs_attention",
            agent_results=_AGENT_RESULTS_MIXED,
        )

    assert result == _SYNTHESIS_TEXT


def test_synthesize_calls_llm_once_on_success():
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {"synthesis": _SYNTHESIS_TEXT}

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        synthesize(
            project_id=1,
            project_name="Test Project",
            project_description="Description.",
            requirements_draft="Requirements.",
            overall_verdict="healthy",
            agent_results=_AGENT_RESULTS_MIXED,
        )

    mock_provider.generate_structured.assert_called_once()


def test_synthesize_user_prompt_includes_project_context():
    mock_provider = MagicMock()
    captured: list[str] = []

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return {"synthesis": _SYNTHESIS_TEXT}

    mock_provider.generate_structured.side_effect = capture

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        synthesize(
            project_id=42,
            project_name="My API Project",
            project_description="REST API with auth module.",
            requirements_draft="User needs CRUD endpoints.",
            overall_verdict="degraded",
            agent_results=_AGENT_RESULTS_MIXED,
        )

    assert len(captured) == 1
    prompt = captured[0]
    assert "42" in prompt
    assert "My API Project" in prompt
    assert "REST API with auth module." in prompt
    assert "User needs CRUD endpoints." in prompt
    assert "degraded" in prompt


def test_synthesize_prompt_separates_agents_by_severity():
    mock_provider = MagicMock()
    captured: list[str] = []

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return {"synthesis": _SYNTHESIS_TEXT}

    mock_provider.generate_structured.side_effect = capture

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        synthesize(
            project_id=1,
            project_name="Project",
            project_description="Description.",
            requirements_draft="",
            overall_verdict="degraded",
            agent_results=_AGENT_RESULTS_MIXED,
        )

    prompt = captured[0]
    # Degraded agents section should mention orchestrator
    assert "orchestrator" in prompt
    # Not-supervised agents should appear
    assert "environment_manager_agent" in prompt


def test_synthesize_empty_agent_results():
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "synthesis": (
            "## Overall Verdict\n\nNo agents were evaluated for this project. "
            "This is unusual and may indicate the project has not yet been executed. "
            "No issues or recommendations can be generated without evaluation data."
        )
    }

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        result = synthesize(
            project_id=1,
            project_name="Empty Project",
            project_description="No activity.",
            requirements_draft="",
            overall_verdict="healthy",
            agent_results=[],
        )

    assert isinstance(result, str)
    assert len(result) > 0


def test_synthesize_all_healthy():
    healthy_results = [
        {
            "agent_name": f"agent_{i}",
            "verdict": "healthy",
            "findings": f"Agent {i} performed well.",
            "issues": [],
            "suggestions": [],
        }
        for i in range(5)
    ]

    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {"synthesis": _SYNTHESIS_TEXT}

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        result = synthesize(
            project_id=1,
            project_name="All-healthy project",
            project_description="Everything works.",
            requirements_draft="Build a system.",
            overall_verdict="healthy",
            agent_results=healthy_results,
        )

    assert result == _SYNTHESIS_TEXT


def test_synthesize_strips_whitespace_from_result():
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = {
        "synthesis": "  \n" + _SYNTHESIS_TEXT + "\n  "
    }

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        result = synthesize(
            project_id=1,
            project_name="Whitespace test",
            project_description="Description.",
            requirements_draft="",
            overall_verdict="healthy",
            agent_results=[],
        )

    assert result == _SYNTHESIS_TEXT
    assert not result.startswith(" ")
    assert not result.endswith(" ")


def test_synthesize_requirements_draft_not_available_fallback():
    mock_provider = MagicMock()
    captured: list[str] = []

    def capture(**kwargs):
        captured.append(kwargs.get("user_prompt", ""))
        return {"synthesis": _SYNTHESIS_TEXT}

    mock_provider.generate_structured.side_effect = capture

    with patch(f"{_MODULE}.get_llm_provider", return_value=mock_provider):
        synthesize(
            project_id=1,
            project_name="No draft project",
            project_description="Description.",
            requirements_draft="",
            overall_verdict="healthy",
            agent_results=[],
        )

    assert "(not available)" in captured[0]
