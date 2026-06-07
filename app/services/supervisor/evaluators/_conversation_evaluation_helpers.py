"""
Shared data-loading helpers for the three conversational-layer supervisor evaluators.

Provides:
  - load_requirements_evaluation_artifacts  → list of requirements_evaluation payloads
  - load_review_episode_artifacts           → list of review_episode payloads
  - load_project_query_artifacts            → list of project_query payloads
  - load_aria_conversation_context          → combined conversation state for AriaConversationEvaluator
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.models.artifact import Artifact

logger = logging.getLogger(__name__)

# Artifact type constants
_REQUIREMENTS_EVALUATION_TYPE = "requirements_evaluation"
_REVIEW_EPISODE_TYPE = "review_episode"
_PROJECT_QUERY_TYPE = "project_query"

# Default caps
_MAX_REQUIREMENTS_ARTIFACTS = 10
_MAX_REVIEW_EPISODE_ARTIFACTS = 8
_MAX_PROJECT_QUERY_ARTIFACTS = 10
_MAX_CONVERSATION_MESSAGES = 60


# ---------------------------------------------------------------------------
# load_requirements_evaluation_artifacts
# ---------------------------------------------------------------------------


def load_requirements_evaluation_artifacts(
    db: Session,
    project_id: int,
    *,
    max_artifacts: int = _MAX_REQUIREMENTS_ARTIFACTS,
) -> list[dict]:
    """Return requirements_evaluation artifacts for a project, oldest-first.

    Each item is the parsed JSON payload enriched with ``artifact_id``.
    Capped at *max_artifacts* most-recent entries (by artifact.id DESC → reversed).
    """
    rows = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.artifact_type == _REQUIREMENTS_EVALUATION_TYPE,
        )
        .order_by(Artifact.id.desc())
        .limit(max_artifacts)
        .all()
    )
    # Reverse so caller receives oldest-first
    rows = list(reversed(rows))

    result: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.content)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "_conversation_helpers: could not parse requirements_evaluation artifact %s",
                row.id,
            )
            continue
        payload["artifact_id"] = row.id
        result.append(payload)

    return result


# ---------------------------------------------------------------------------
# load_review_episode_artifacts
# ---------------------------------------------------------------------------


def load_review_episode_artifacts(
    db: Session,
    project_id: int,
    *,
    max_artifacts: int = _MAX_REVIEW_EPISODE_ARTIFACTS,
) -> list[dict]:
    """Return review_episode artifacts for a project, oldest-first.

    Each item is the parsed JSON payload enriched with ``artifact_id``.
    Capped at *max_artifacts* most-recent entries.
    """
    rows = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.artifact_type == _REVIEW_EPISODE_TYPE,
        )
        .order_by(Artifact.id.desc())
        .limit(max_artifacts)
        .all()
    )
    rows = list(reversed(rows))

    result: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.content)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "_conversation_helpers: could not parse review_episode artifact %s",
                row.id,
            )
            continue
        payload["artifact_id"] = row.id
        result.append(payload)

    return result


# ---------------------------------------------------------------------------
# load_project_query_artifacts
# ---------------------------------------------------------------------------


def load_project_query_artifacts(
    db: Session,
    project_id: int,
    *,
    max_artifacts: int = _MAX_PROJECT_QUERY_ARTIFACTS,
) -> list[dict]:
    """Return project_query artifacts for a project, oldest-first.

    Each item is the parsed JSON payload enriched with ``artifact_id``.
    Capped at *max_artifacts* most-recent entries.
    """
    rows = (
        db.query(Artifact)
        .filter(
            Artifact.project_id == project_id,
            Artifact.artifact_type == _PROJECT_QUERY_TYPE,
        )
        .order_by(Artifact.id.desc())
        .limit(max_artifacts)
        .all()
    )
    rows = list(reversed(rows))

    result: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.content)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "_conversation_helpers: could not parse project_query artifact %s",
                row.id,
            )
            continue
        payload["artifact_id"] = row.id
        result.append(payload)

    return result


# ---------------------------------------------------------------------------
# load_aria_conversation_context
# ---------------------------------------------------------------------------


def load_aria_conversation_context(
    db: Session,
    project_id: int,
    *,
    max_messages: int = _MAX_CONVERSATION_MESSAGES,
) -> dict | None:
    """Return a combined context dict for AriaConversationEvaluator.

    Combines:
    - Conversation record fields (phase, review_episode_attempts, requirements_draft)
    - Last *max_messages* ConversationMessages (role/content, oldest-first)
    - All project_query artifacts (typically few per project)

    Returns ``None`` if no Conversation exists for the project (project never
    had a conversational session).
    """
    from app.models.conversation import (
        CONVERSATION_STATUS_ACTIVE,
        Conversation,
        ConversationMessage,
    )

    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if conversation is None:
        # Try any status — project may be completed
        conversation = (
            db.query(Conversation)
            .filter(Conversation.project_id == project_id)
            .order_by(Conversation.id.desc())
            .first()
        )
    if conversation is None:
        return None

    # Load recent messages (last max_messages, ordered by id asc)
    messages_rows = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.id.desc())
        .limit(max_messages)
        .all()
    )
    messages = [{"role": m.role, "content": m.content} for m in reversed(messages_rows)]

    # Load project_query artifacts (no cap — typically very few)
    query_artifacts = load_project_query_artifacts(db, project_id)

    requirements_draft_preview = (conversation.requirements_draft or "")[:300] or None

    # paused_reason is only present when final_phase == "paused" and the pause was caused
    # by a project-level review abandonment. Helps the evaluator detect whether Aria
    # communicated the reason clearly during the paused episode.
    paused_reason = getattr(conversation, "paused_reason", None)

    return {
        "conversation_id": conversation.id,
        "final_phase": conversation.phase,
        "review_episode_count": conversation.review_episode_attempts or 0,
        "requirements_draft_preview": requirements_draft_preview,
        "message_count": len(messages),
        "messages": messages,
        "project_queries": query_artifacts,
        "paused_reason": paused_reason,
    }
