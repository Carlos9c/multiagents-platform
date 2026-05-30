"""
Tests for _conversation_evaluation_helpers.

Covers: requirements_evaluation artifact loading, review_episode artifact loading,
project_query artifact loading, and aria_conversation_context assembly.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_COMPLETED,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.services.supervisor.evaluators._conversation_evaluation_helpers import (
    load_aria_conversation_context,
    load_project_query_artifacts,
    load_requirements_evaluation_artifacts,
    load_review_episode_artifacts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_requirements_payload(
    call_index: int = 0,
    status: str = "needs_more",
) -> dict:
    return {
        "call_index": call_index,
        "status": status,
        "next_question": "What tech stack do you prefer?" if status == "needs_more" else None,
        "reasoning": "Need more details.",
        "draft_snapshot": "User wants to build a web app.",
        "conversation_turn_count": call_index * 2 + 1,
    }


def _make_review_episode_payload(
    episode_index: int = 0,
    outcome: str = "confirmed",
    impact_scope: str | None = "moderate",
) -> dict:
    return {
        "episode_index": episode_index,
        "is_project_level": False,
        "blocked_task_id": 10 + episode_index,
        "blocked_task_title": f"Task {episode_index}",
        "review_attempts": 3,
        "proposed_plan": "Split the task into smaller pieces.",
        "outcome": outcome,
        "impact_scope": impact_scope,
        "tasks_modified_count": 1 if impact_scope == "moderate" else 0,
        "tasks_added_count": 0,
        "tasks_superseded_count": 0,
        "impact_reasoning": "Moderate scope: only task 10 needs revision.",
    }


def _make_project_query_payload(query_index: int = 0) -> dict:
    return {
        "query_index": query_index,
        "conversation_phase": "paused",
        "user_question": "What has been completed?",
        "aria_response": "We have completed 3 tasks so far.",
        "task_counts": {"completed": 3, "pending": 5, "failed": 0, "running": 0},
    }


def _make_conversation(
    db: Session, project_id: int, phase: str = CONVERSATION_PHASE_GATHERING
) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=phase,
        review_episode_attempts=2,
        requirements_ready=False,
        requirements_draft="Draft: build a web application.",
    )
    db.add(conv)
    db.flush()
    return conv


def _add_message(db: Session, conv_id: int, role: str, content: str) -> ConversationMessage:
    msg = ConversationMessage(conversation_id=conv_id, role=role, content=content)
    db.add(msg)
    db.flush()
    return msg


# ---------------------------------------------------------------------------
# load_requirements_evaluation_artifacts
# ---------------------------------------------------------------------------


def test_requirements_artifacts_empty_when_none(db_session: Session, make_project):
    proj = make_project(name="No req artifacts")
    result = load_requirements_evaluation_artifacts(db_session, proj.id)
    assert result == []


def test_requirements_artifacts_returns_parsed_content(
    db_session: Session, make_project, make_artifact
):
    proj = make_project(name="Req artifacts project")
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="requirements_evaluation",
        content=json.dumps(_make_requirements_payload(call_index=0, status="needs_more")),
    )
    result = load_requirements_evaluation_artifacts(db_session, proj.id)
    assert len(result) == 1
    assert result[0]["status"] == "needs_more"
    assert result[0]["call_index"] == 0
    assert "artifact_id" in result[0]


def test_requirements_artifacts_ordered_oldest_first(
    db_session: Session, make_project, make_artifact
):
    proj = make_project(name="Ordered req artifacts")
    for i in range(3):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="requirements_evaluation",
            content=json.dumps(_make_requirements_payload(call_index=i)),
        )
    result = load_requirements_evaluation_artifacts(db_session, proj.id)
    assert len(result) == 3
    assert result[0]["call_index"] == 0
    assert result[2]["call_index"] == 2


def test_requirements_artifacts_capped_at_max(db_session: Session, make_project, make_artifact):
    proj = make_project(name="Many req artifacts")
    for i in range(12):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="requirements_evaluation",
            content=json.dumps(_make_requirements_payload(call_index=i)),
        )
    result = load_requirements_evaluation_artifacts(db_session, proj.id, max_artifacts=5)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# load_review_episode_artifacts
# ---------------------------------------------------------------------------


def test_review_episodes_empty_when_none(db_session: Session, make_project):
    proj = make_project(name="No review episodes")
    result = load_review_episode_artifacts(db_session, proj.id)
    assert result == []


def test_review_episodes_returns_parsed_content(db_session: Session, make_project, make_artifact):
    proj = make_project(name="Review episodes project")
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="review_episode",
        content=json.dumps(_make_review_episode_payload(episode_index=0, outcome="confirmed")),
    )
    result = load_review_episode_artifacts(db_session, proj.id)
    assert len(result) == 1
    assert result[0]["outcome"] == "confirmed"
    assert result[0]["episode_index"] == 0
    assert "artifact_id" in result[0]


def test_review_episodes_includes_abandoned_outcomes(
    db_session: Session, make_project, make_artifact
):
    proj = make_project(name="Abandoned episode project")
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="review_episode",
        content=json.dumps(_make_review_episode_payload(outcome="abandoned", impact_scope=None)),
    )
    result = load_review_episode_artifacts(db_session, proj.id)
    assert result[0]["outcome"] == "abandoned"
    assert result[0]["impact_scope"] is None


def test_review_episodes_capped_at_max(db_session: Session, make_project, make_artifact):
    proj = make_project(name="Many review episodes")
    for i in range(10):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="review_episode",
            content=json.dumps(_make_review_episode_payload(episode_index=i)),
        )
    result = load_review_episode_artifacts(db_session, proj.id, max_artifacts=4)
    assert len(result) == 4


# ---------------------------------------------------------------------------
# load_project_query_artifacts
# ---------------------------------------------------------------------------


def test_project_query_empty_when_none(db_session: Session, make_project):
    proj = make_project(name="No queries")
    result = load_project_query_artifacts(db_session, proj.id)
    assert result == []


def test_project_query_returns_parsed_content(db_session: Session, make_project, make_artifact):
    proj = make_project(name="Query artifacts project")
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="project_query",
        content=json.dumps(_make_project_query_payload(query_index=0)),
    )
    result = load_project_query_artifacts(db_session, proj.id)
    assert len(result) == 1
    assert result[0]["query_index"] == 0
    assert result[0]["task_counts"]["completed"] == 3
    assert "artifact_id" in result[0]


def test_project_query_capped_at_max(db_session: Session, make_project, make_artifact):
    proj = make_project(name="Many queries")
    for i in range(15):
        make_artifact(
            project_id=proj.id,
            task_id=None,
            artifact_type="project_query",
            content=json.dumps(_make_project_query_payload(query_index=i)),
        )
    result = load_project_query_artifacts(db_session, proj.id, max_artifacts=6)
    assert len(result) == 6


# ---------------------------------------------------------------------------
# load_aria_conversation_context
# ---------------------------------------------------------------------------


def test_aria_context_returns_none_when_no_conversation(db_session: Session, make_project):
    proj = make_project(name="No conversation")
    result = load_aria_conversation_context(db_session, proj.id)
    assert result is None


def test_aria_context_includes_conversation_fields(db_session: Session, make_project):
    proj = make_project(name="Context project")
    conv = _make_conversation(db_session, proj.id, phase=CONVERSATION_PHASE_COMPLETED)
    _add_message(db_session, conv.id, "user", "Hello, I want a web app.")
    _add_message(db_session, conv.id, "assistant", "Great! Tell me more.")

    result = load_aria_conversation_context(db_session, proj.id)

    assert result is not None
    assert result["final_phase"] == CONVERSATION_PHASE_COMPLETED
    assert result["review_episode_count"] == 2
    assert result["requirements_draft_preview"] is not None
    assert "web app" in result["requirements_draft_preview"]


def test_aria_context_includes_messages(db_session: Session, make_project):
    proj = make_project(name="Messages project")
    conv = _make_conversation(db_session, proj.id)
    _add_message(db_session, conv.id, "user", "Build a blog.")
    _add_message(db_session, conv.id, "assistant", "What language?")
    _add_message(db_session, conv.id, "user", "Python.")

    result = load_aria_conversation_context(db_session, proj.id)

    assert result is not None
    assert result["message_count"] == 3
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_aria_context_includes_project_queries(db_session: Session, make_project, make_artifact):
    proj = make_project(name="Queries context project")
    _make_conversation(db_session, proj.id)
    make_artifact(
        project_id=proj.id,
        task_id=None,
        artifact_type="project_query",
        content=json.dumps(_make_project_query_payload(query_index=0)),
    )

    result = load_aria_conversation_context(db_session, proj.id)

    assert result is not None
    assert len(result["project_queries"]) == 1
    assert result["project_queries"][0]["query_index"] == 0


def test_aria_context_caps_messages(db_session: Session, make_project):
    proj = make_project(name="Many messages project")
    conv = _make_conversation(db_session, proj.id)
    for i in range(80):
        _add_message(db_session, conv.id, "user", f"Message {i}")

    result = load_aria_conversation_context(db_session, proj.id, max_messages=30)

    assert result is not None
    assert result["message_count"] == 30
