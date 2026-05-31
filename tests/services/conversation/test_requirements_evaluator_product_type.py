"""Tests for product_type detection — Phase 2 of the QA Engine."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationMessage
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.requirements_tool import RequirementsTool
from app.services.conversation.requirements_evaluator import (
    ConversationTurn,
    RequirementsEvaluatorInput,
    RequirementsEvaluatorResult,
    evaluate_requirements,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _raw_needs_more(product_type: str | None = "web_app") -> dict:
    return {
        "status": "needs_more",
        "next_question": "What authentication method would you like?",
        "updated_draft": "The system is a web application that...",
        "reasoning": "Missing auth info.",
        "product_type": product_type,
    }


def _raw_sufficient(product_type: str | None = "rest_api") -> dict:
    return {
        "status": "sufficient",
        "next_question": None,
        "updated_draft": "The system must expose a REST API with JWT auth...",
        "reasoning": "All four quadrants covered.",
        "product_type": product_type,
    }


def _make_input(history: list[tuple[str, str]] | None = None) -> RequirementsEvaluatorInput:
    turns = [
        ConversationTurn(role=r, content=c)
        for r, c in (history or [("user", "I want to build a web app")])
    ]
    return RequirementsEvaluatorInput(
        project_name="Test Project",
        history=turns,
        current_draft=None,
    )


def _patch_llm(raw: dict):
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = raw
    return patch(
        "app.services.conversation.requirements_evaluator.get_llm_provider",
        return_value=mock_provider,
    )


# ── evaluate_requirements: product_type propagation ───────────────────────────


def test_product_type_propagated_when_needs_more():
    with _patch_llm(_raw_needs_more("web_app")):
        result = evaluate_requirements(_make_input())

    assert isinstance(result, RequirementsEvaluatorResult)
    assert result.product_type == "web_app"
    assert result.status == "needs_more"


def test_product_type_propagated_when_sufficient():
    with _patch_llm(_raw_sufficient("rest_api")):
        result = evaluate_requirements(_make_input())

    assert result.product_type == "rest_api"
    assert result.status == "sufficient"


def test_product_type_none_when_llm_returns_null():
    raw = _raw_needs_more(product_type=None)
    with _patch_llm(raw):
        result = evaluate_requirements(_make_input())

    assert result.product_type is None


@pytest.mark.parametrize(
    "product_type",
    [
        "web_app",
        "rest_api",
        "graphql_api",
        "cli_tool",
        "mobile_android",
        "library",
        "desktop_app",
        "written_content",
        "music",
        "visual_content",
        "data_product",
        "game_assets",
    ],
)
def test_all_valid_product_types_accepted(product_type: str):
    raw = _raw_sufficient(product_type=product_type)
    with _patch_llm(raw):
        result = evaluate_requirements(_make_input())

    assert result.product_type == product_type


# ── RequirementsTool: product_type in ToolResult.data ─────────────────────────


def _make_conversation(db: Session, project_id: int) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status="active",
        phase="gathering",
    )
    db.add(conv)
    db.flush()
    return conv


def test_requirements_tool_includes_product_type_in_result(db_session: Session, make_project):
    project = make_project(name="QA Test Project")
    conv = _make_conversation(db_session, project.id)

    msg = ConversationMessage(
        conversation_id=conv.id,
        role="user",
        content="I want to build a Django REST API",
    )
    db_session.add(msg)
    db_session.flush()

    raw = _raw_sufficient("rest_api")
    with _patch_llm(raw):
        tool = RequirementsTool()
        result = tool.execute(db=db_session, project_id=project.id, conversation=conv)

    assert result.tool_name == ToolName.REQUIREMENTS
    assert result.data["product_type"] == "rest_api"


def test_requirements_tool_product_type_none_when_llm_returns_null(
    db_session: Session, make_project
):
    project = make_project(name="Unclear Project")
    conv = _make_conversation(db_session, project.id)

    msg = ConversationMessage(
        conversation_id=conv.id,
        role="user",
        content="I want to build something",
    )
    db_session.add(msg)
    db_session.flush()

    raw = _raw_needs_more(product_type=None)
    with _patch_llm(raw):
        tool = RequirementsTool()
        result = tool.execute(db=db_session, project_id=project.id, conversation=conv)

    assert result.data["product_type"] is None


# ── _apply_phase_transitions: product_type written to project ─────────────────


def test_phase_transition_writes_product_type_to_project_when_sufficient(
    db_session: Session, make_project
):
    from app.services.conversation.aria.contracts import AriaStep, ToolResult
    from app.services.conversation.aria.orchestrator import _apply_phase_transitions

    project = make_project()
    assert project.product_type is None

    conv = _make_conversation(db_session, project.id)
    db_session.commit()

    aria_step = AriaStep(
        action="respond",
        response="Great, your requirements are complete.",
        reasoning="All info gathered.",
    )
    tool_results = [
        ToolResult(
            tool_name=ToolName.REQUIREMENTS,
            data={
                "status": "sufficient",
                "next_question": None,
                "updated_draft": "The system must provide a REST API...",
                "reasoning": "All info present.",
                "product_type": "rest_api",
            },
        )
    ]

    _apply_phase_transitions(db_session, conv, aria_step, tool_results)
    db_session.flush()
    db_session.refresh(project)

    assert project.product_type == "rest_api"


def test_phase_transition_skips_product_type_write_when_needs_more(
    db_session: Session, make_project
):
    from app.services.conversation.aria.contracts import AriaStep, ToolResult
    from app.services.conversation.aria.orchestrator import _apply_phase_transitions

    project = make_project()
    conv = _make_conversation(db_session, project.id)
    db_session.commit()

    aria_step = AriaStep(
        action="respond", response="What tech stack will you use?", reasoning="Missing stack info."
    )
    tool_results = [
        ToolResult(
            tool_name=ToolName.REQUIREMENTS,
            data={
                "status": "needs_more",
                "next_question": "What tech stack?",
                "updated_draft": "Partial draft...",
                "reasoning": "Missing stack info.",
                "product_type": "web_app",
            },
        )
    ]

    _apply_phase_transitions(db_session, conv, aria_step, tool_results)
    db_session.flush()
    db_session.refresh(project)

    # product_type is NOT written to project — only on sufficient
    assert project.product_type is None


def test_phase_transition_no_product_type_when_none(db_session: Session, make_project):
    from app.services.conversation.aria.contracts import AriaStep, ToolResult
    from app.services.conversation.aria.orchestrator import _apply_phase_transitions

    project = make_project()
    conv = _make_conversation(db_session, project.id)
    db_session.commit()

    aria_step = AriaStep(action="respond", response="Requirements complete!", reasoning="Complete.")
    tool_results = [
        ToolResult(
            tool_name=ToolName.REQUIREMENTS,
            data={
                "status": "sufficient",
                "next_question": None,
                "updated_draft": "The system must...",
                "reasoning": "Complete.",
                "product_type": None,
            },
        )
    ]

    _apply_phase_transitions(db_session, conv, aria_step, tool_results)
    db_session.flush()
    db_session.refresh(project)

    # null product_type → nothing written
    assert project.product_type is None


def test_phase_transition_overwrites_existing_product_type(db_session: Session, make_project):
    from app.services.conversation.aria.contracts import AriaStep, ToolResult
    from app.services.conversation.aria.orchestrator import _apply_phase_transitions

    project = make_project()
    project.product_type = "cli_tool"
    db_session.add(project)

    conv = _make_conversation(db_session, project.id)
    db_session.commit()

    aria_step = AriaStep(action="respond", response="Done.", reasoning="All info gathered.")
    tool_results = [
        ToolResult(
            tool_name=ToolName.REQUIREMENTS,
            data={
                "status": "sufficient",
                "next_question": None,
                "updated_draft": "The application must...",
                "reasoning": "Complete.",
                "product_type": "desktop_app",
            },
        )
    ]

    _apply_phase_transitions(db_session, conv, aria_step, tool_results)
    db_session.flush()
    db_session.refresh(project)

    assert project.product_type == "desktop_app"
