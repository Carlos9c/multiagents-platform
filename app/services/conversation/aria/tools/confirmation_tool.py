"""ConfirmationTool — wraps evaluate_confirmation()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationMessage
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.confirmation_evaluator import (
    ConfirmationEvaluatorInput,
    ConversationTurn,
    evaluate_confirmation,
)
from app.services.conversation.language_utils import detect_language


class ConfirmationTool:
    """Interprets whether the user confirmed or rejected the proposed plan.

    Reads proposed_plan from the conversation record; the user's latest
    message is provided via the hint parameter. Also loads the full review
    episode history so the evaluator can resolve contextual references
    (e.g. "yes, but change what you said about the endpoint").
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

        episode_history = self._load_episode_history(db, conversation)
        detected_language = detect_language(episode_history)

        result = evaluate_confirmation(
            ConfirmationEvaluatorInput(
                action_summary=proposed_plan,
                user_response=user_response,
                episode_history=episode_history,
                language=detected_language,
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

    def _load_episode_history(
        self,
        db: Session,
        conversation: Conversation,
    ) -> list[ConversationTurn]:
        """Load messages relevant to the current review episode.

        Uses review_episode_start_message_id as anchor when available (same
        strategy as ReviewTool). Falls back to scanning for the last system
        message for pre-migration rows.
        """
        anchor_id = getattr(conversation, "review_episode_start_message_id", None)

        if anchor_id is not None:
            messages = (
                db.query(ConversationMessage)
                .filter(
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.id >= anchor_id,
                )
                .order_by(ConversationMessage.id.asc())
                .all()
            )
        else:
            # Fallback: scan for last system message
            all_messages = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == conversation.id)
                .order_by(ConversationMessage.id.asc())
                .all()
            )
            last_system_idx = -1
            for i, m in enumerate(all_messages):
                if m.role == "system":
                    last_system_idx = i
            messages = all_messages[last_system_idx:] if last_system_idx >= 0 else all_messages

        return [
            ConversationTurn(role=m.role, content=m.content)  # type: ignore[arg-type]
            for m in messages
            if m.role in ("user", "assistant", "system")
        ]

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
