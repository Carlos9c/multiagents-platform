"""WebSocket message protocol.

All messages exchanged over the WebSocket are typed JSON objects.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# ── Client → Server ───────────────────────────────────────────────────────────


class IncomingMessage(BaseModel):
    type: Literal["user_message", "ping"]
    content: str = ""


# ── Server → Client ───────────────────────────────────────────────────────────


class MessageRecord(BaseModel):
    """Single message from conversation history."""

    role: str
    content: str
    task_id: int | None = None


class OutgoingMessage(BaseModel):
    """Base outgoing envelope — serialised to JSON and sent over the wire."""

    type: str
    conversation_id: int | None = None
    phase: str | None = None

    # Populated for assistant_message and review_required
    content: str | None = None
    event: str | None = None

    # Populated for review_required
    task_id: int | None = None

    # Populated for project_started
    project_id: int | None = None

    # Populated for conversation_state (initial sync on connect)
    messages: list[MessageRecord] | None = None
    requirements_draft: str | None = None

    # Populated for execution_event
    task_title: str | None = None
    status: str | None = None


def assistant_message(
    *,
    content: str,
    event: str,
    phase: str,
    conversation_id: int,
    project_id: int | None = None,
    task_id: int | None = None,
    requirements_draft: str | None = None,
) -> dict[str, Any]:
    return OutgoingMessage(
        type="assistant_message",
        content=content,
        event=event,
        phase=phase,
        conversation_id=conversation_id,
        project_id=project_id,
        task_id=task_id,
        requirements_draft=requirements_draft,
    ).model_dump(exclude_none=True)


def conversation_state(
    *,
    conversation_id: int,
    phase: str,
    messages: list[MessageRecord],
    requirements_draft: str | None,
) -> dict[str, Any]:
    return OutgoingMessage(
        type="conversation_state",
        conversation_id=conversation_id,
        phase=phase,
        messages=messages,
        requirements_draft=requirements_draft,
    ).model_dump(exclude_none=True)


def review_required(
    *,
    content: str,
    task_id: int | None,
    conversation_id: int,
) -> dict[str, Any]:
    return OutgoingMessage(
        type="review_required",
        content=content,
        task_id=task_id,
        conversation_id=conversation_id,
        phase="awaiting_review",
    ).model_dump(exclude_none=True)


def execution_event(
    *,
    task_id: int,
    task_title: str,
    status: str,
    project_id: int,
) -> dict[str, Any]:
    return OutgoingMessage(
        type="execution_event",
        task_id=task_id,
        task_title=task_title,
        status=status,
        project_id=project_id,
    ).model_dump(exclude_none=True)


def workflow_paused(*, conversation_id: int, project_id: int) -> dict[str, Any]:
    return OutgoingMessage(
        type="workflow_paused",
        conversation_id=conversation_id,
        project_id=project_id,
        phase="paused",
        content="La ejecución ha sido pausada. Puedes reanudarla cuando quieras.",
    ).model_dump(exclude_none=True)


def error_message(detail: str) -> dict[str, Any]:
    return {"type": "error", "detail": detail}


def pong() -> dict[str, Any]:
    return {"type": "pong"}
