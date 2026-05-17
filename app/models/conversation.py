from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
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

VALID_MESSAGE_ROLES = {
    MESSAGE_ROLE_USER,
    MESSAGE_ROLE_ASSISTANT,
}

CONVERSATION_PHASE_GATHERING = "gathering_requirements"
CONVERSATION_PHASE_READY = "ready_to_start"
CONVERSATION_PHASE_EXECUTING = "executing"
CONVERSATION_PHASE_AWAITING_REVIEW = "awaiting_review"
CONVERSATION_PHASE_PAUSED = "paused"
CONVERSATION_PHASE_COMPLETED = "completed"

VALID_CONVERSATION_PHASES = {
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_READY,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_AWAITING_REVIEW,
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_PHASE_COMPLETED,
}

# Sub-phases within AWAITING_REVIEW
REVIEW_SUBPHASE_GATHERING = "gathering"
REVIEW_SUBPHASE_AWAITING_CONFIRMATION = "awaiting_confirmation"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )

    # Task under manual_review that triggered the current review episode (nullable)
    review_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"),
        nullable=True,
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

    # Accumulated turn count within the current review episode (informational only).
    review_episode_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Sub-phase within AWAITING_REVIEW: "gathering" | "awaiting_confirmation"
    review_subphase: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Clarification text stored when the review evaluator reaches "ready_to_confirm".
    # Passed to resume_after_review() once the user confirms the proposed plan.
    pending_clarification_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # For project-level review episodes (review_task_id=None): stores the original failure
    # reason (env error, iteration limit, etc.) that anchors the review. Persists across
    # gathering → confirmation round-trips, unlike pending_clarification_summary which is
    # overwritten when the proposed plan is stored.
    review_failure_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Living requirements document built during GATHERING_REQUIREMENTS phase.
    # Becomes project.description when the agent decides it has enough context.
    requirements_draft: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    review_task = relationship("Task", foreign_keys=[review_task_id])
    messages: Mapped[list["ConversationMessage"]] = relationship(
        "ConversationMessage",
        back_populates="conversation",
        order_by="ConversationMessage.id",
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

    # Optional link to the task that triggered this message (for review episodes)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    conversation: Mapped["Conversation"] = relationship(
        "Conversation",
        back_populates="messages",
    )
    task = relationship("Task", foreign_keys=[task_id])
