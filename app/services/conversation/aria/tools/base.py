"""Base protocol for all Aria tools."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.services.conversation.aria.contracts import ToolName, ToolResult


@runtime_checkable
class AriaTool(Protocol):
    """Each tool wraps one or more existing evaluators/services.

    Tools are responsible for fetching their own context from DB — Aria only
    passes a lightweight `hint` string drawn from its reasoning about what
    the user needs.
    """

    @property
    def name(self) -> ToolName: ...

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult: ...
