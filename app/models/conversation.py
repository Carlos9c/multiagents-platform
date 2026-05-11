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
CONVERSATION_PHASE_COMPLETED = "completed"

VALID_CONVERSATION_PHASES = {
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_READY,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_AWAITING_REVIEW,
    CONVERSATION_PHASE_COMPLETED,
}


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

    # Accumulated attempts within the current review episode (max 3 for AWAITING_REVIEW)
    review_episode_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

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
