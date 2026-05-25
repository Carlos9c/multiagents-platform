"""aria_orchestrator_model_changes

Replace legacy project_assistant state fields with Aria orchestrator fields.

Removed:
  - conversations.review_task_id (FK to tasks)
  - conversations.review_failure_context
  - conversations.review_subphase
  - conversations.pending_clarification_summary

Added:
  - conversations.review_context      (JSON blob: TaskReviewContext | ProjectReviewContext)
  - conversations.proposed_plan       (clarification awaiting user confirmation)
  - conversations.requirements_ready  (flag: frontend can show Start button)

Also migrates any rows in phase 'ready_to_start' to 'gathering_requirements'
and adds MESSAGE_ROLE_SYSTEM support (no schema change needed — existing Text column).

Revision ID: e1f2a3b4c5d6
Revises: d3e4f5a6b7c8
Create Date: 2026-05-25 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Remove legacy columns ────────────────────────────────────────────────

    # Drop FK constraint on review_task_id before dropping the column.
    # The constraint name is database-dependent; use batch_alter_table for
    # SQLite compatibility (used in tests) and PostgreSQL in production.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("review_task_id")
        batch_op.drop_column("review_failure_context")
        batch_op.drop_column("review_subphase")
        batch_op.drop_column("pending_clarification_summary")

    # ── Add new columns ──────────────────────────────────────────────────────

    op.add_column(
        "conversations",
        sa.Column("review_context", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("proposed_plan", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "requirements_ready",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ── Migrate stale phase values ───────────────────────────────────────────
    op.execute(
        "UPDATE conversations SET phase = 'gathering_requirements' WHERE phase = 'ready_to_start'"
    )
    op.execute("UPDATE conversations SET phase = 'reviewing' WHERE phase = 'awaiting_review'")


def downgrade() -> None:
    # ── Remove new columns ───────────────────────────────────────────────────
    op.drop_column("conversations", "requirements_ready")
    op.drop_column("conversations", "proposed_plan")
    op.drop_column("conversations", "review_context")

    # ── Restore legacy columns ───────────────────────────────────────────────
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("review_task_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("review_failure_context", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("review_subphase", sa.String(50), nullable=True))
        batch_op.add_column(sa.Column("pending_clarification_summary", sa.Text(), nullable=True))

    # Restore migrated phase values
    op.execute("UPDATE conversations SET phase = 'awaiting_review' WHERE phase = 'reviewing'")
