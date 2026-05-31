"""qa_engine_foundation

Add QA engine tables and fields:
  - projects.product_type
  - conversations.qa_offer_pending
  - conversations.pending_qa_report
  - qa_sessions table
  - qa_findings table

Revision ID: f1b2c3d4e5f6
Revises: e7f8a9b0c1d2
Create Date: 2026-05-31 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── projects: product_type ───────────────────────────────────────────────
    op.add_column(
        "projects",
        sa.Column("product_type", sa.String(50), nullable=True),
    )

    # ── conversations: QA flow fields ────────────────────────────────────────
    op.add_column(
        "conversations",
        sa.Column(
            "qa_offer_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("pending_qa_report", sa.Text(), nullable=True),
    )

    # ── qa_sessions ──────────────────────────────────────────────────────────
    op.create_table(
        "qa_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("triggered_by", sa.String(20), nullable=False, server_default="user"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("product_type", sa.String(50), nullable=True),
        sa.Column("strategy_used", sa.String(50), nullable=True),
        sa.Column("verdict", sa.String(30), nullable=True),
        sa.Column("agents_called", sa.JSON(), nullable=True),
        sa.Column("findings_summary", sa.JSON(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_sessions_id"), "qa_sessions", ["id"], unique=False)
    op.create_index(op.f("ix_qa_sessions_project_id"), "qa_sessions", ["project_id"], unique=False)

    # ── qa_findings ──────────────────────────────────────────────────────────
    op.create_table(
        "qa_findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("qa_session_id", sa.Integer(), nullable=False),
        sa.Column("finding_id", sa.String(36), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reproduction_steps", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("affected_component", sa.String(255), nullable=True),
        sa.Column("auto_remediable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("remediation_hint", sa.Text(), nullable=True),
        sa.Column("remediation_task_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["qa_session_id"], ["qa_sessions.id"]),
        sa.ForeignKeyConstraint(["remediation_task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_findings_id"), "qa_findings", ["id"], unique=False)
    op.create_index(
        op.f("ix_qa_findings_qa_session_id"),
        "qa_findings",
        ["qa_session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_qa_findings_qa_session_id"), table_name="qa_findings")
    op.drop_index(op.f("ix_qa_findings_id"), table_name="qa_findings")
    op.drop_table("qa_findings")

    op.drop_index(op.f("ix_qa_sessions_project_id"), table_name="qa_sessions")
    op.drop_index(op.f("ix_qa_sessions_id"), table_name="qa_sessions")
    op.drop_table("qa_sessions")

    op.drop_column("conversations", "pending_qa_report")
    op.drop_column("conversations", "qa_offer_pending")
    op.drop_column("projects", "product_type")
