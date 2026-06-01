"""ConfirmationTool — wraps evaluate_confirmation()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationMessage
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.confirmation_evaluator import (
    ConfirmationEvaluatorInput,
    evaluate_confirmation,
)


class ConfirmationTool:
    """Interprets whether the user confirmed or rejected the proposed plan.

    Reads proposed_plan from the conversation record; the user's latest
    message is provided via the hint parameter.
    """

    @property
    def name(self) -> ToolName:
        return ToolName.CONFIRMATION

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult:
        proposed_plan = conversation.proposed_plan or "(plan details not available)"
        # Prefer the explicit hint forwarded by Aria (should be the user's message).
        # Fall back to loading the most recent user message directly from DB so the
        # evaluator always has real text — prevents false negatives caused by a missing
        # or generic hint.
        user_response = hint or self._last_user_message(db, conversation)

        result = evaluate_confirmation(
            ConfirmationEvaluatorInput(
                action_summary=proposed_plan,
                user_response=user_response,
            )
        )

        return ToolResult(
            tool_name=ToolName.CONFIRMATION,
            data={
                "confirmed": result.confirmed,
                "follow_up": result.follow_up,
                "reasoning": result.reasoning,
            },
        )

    @staticmethod
    def _last_user_message(db: Session, conversation: Conversation) -> str:
        """Return the content of the most recent user message in this conversation."""
        msg = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.conversation_id == conversation.id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.id.desc())
            .first()
        )
        return msg.content if msg else ""
