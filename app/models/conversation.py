from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

CONVERSATION_STATUS_ACTIVE = "active"
CONVERSATION_STATUS_COMPLETED = "completed"
CONVERSATION_STATUS_ABANDONED = "abandoned"

VALID_CONVERSATION_STATUSES = {
    CONVERSATION_STATUS_ACTIVE,
    CONVERSATION_STATUS_COMPLETED,
    CONVERSATION_STATUS_ABANDONED,
}

MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"
MESSAGE_ROLE_SYSTEM = "system"

VALID_MESSAGE_ROLES = {
    MESSAGE_ROLE_USER,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_SYSTEM,
}

CONVERSATION_PHASE_GATHERING = "gathering_requirements"
CONVERSATION_PHASE_EXECUTING = "executing"
CONVERSATION_PHASE_REVIEWING = "reviewing"
CONVERSATION_PHASE_REPLANNING = "replanning"
CONVERSATION_PHASE_PAUSED = "paused"
CONVERSATION_PHASE_COMPLETED = "completed"
CONVERSATION_PHASE_QA_RUNNING = "qa_running"

VALID_CONVERSATION_PHASES = {
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_PHASE_REPLANNING,
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_PHASE_COMPLETED,
    CONVERSATION_PHASE_QA_RUNNING,
}


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=CONVERSATION_STATUS_ACTIVE,
    )

    phase: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=CONVERSATION_PHASE_GATHERING,
    )

    # True once RequirementsTool returns "sufficient" — signals frontend to show Start button.
    requirements_ready: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # JSON-serialized ReviewContext (TaskReviewContext | ProjectReviewContext).
    # Set when phase transitions to REVIEWING. Cleared when review is resolved.
    review_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stores the clarification_summary once ReviewTool returns "ready_to_confirm".
    # Presence signals the orchestrator that we are awaiting user confirmation.
    # Cleared on confirmation (confirmed or rejected).
    proposed_plan: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Accumulated turn count within the current review episode (informational only).
    review_episode_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # ID of the ConversationMessage (role=system) that opened the current review episode.
    # Used by ReviewTool to determine the episode boundary without scanning all messages.
    # NULL for pre-migration rows or when no review is active.
    review_episode_start_message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("conversation_messages.id"),
        nullable=True,
    )

    # Living requirements document built during GATHERING phase.
    # Becomes project.description when the agent decides it has enough context.
    requirements_draft: Mapped[str | None] = mapped_column(Text, nullable=True)

    # QA flow fields
    # True while Aria has made the QA offer and is awaiting yes/no from the user.
    qa_offer_pending: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    # JSON-serialized QAResult stored while Aria awaits remediation confirmation.
    # Cleared when user confirms or declines remediation tasks.
    pending_qa_report: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stores the failure reason when the conversation transitions to PAUSED via a
    # project-level review abandonment. Cleared when execution resumes.
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=datetime.utcnow,
    )

    project = relationship("Project", backref="conversations")
    messages: Mapped[list["ConversationMessage"]] = relationship(
        "ConversationMessage",
        back_populates="conversation",
        order_by="ConversationMessage.id",
        foreign_keys="ConversationMessage.conversation_id",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    conversation: Mapped["Conversation"] = relationship(
        "Conversation",
        back_populates="messages",
        foreign_keys=[conversation_id],
    )
