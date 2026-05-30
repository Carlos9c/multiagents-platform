"""add_aggregate_report

Revision ID: e7f8a9b0c1d2
Revises: c489e0b5821d
Create Date: 2026-05-30 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "c489e0b5821d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "aggregate_reports",
        sa.Column("aggregate_report_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filter_params", sa.JSON(), nullable=True),
        sa.Column("supervisor_report_ids", sa.JSON(), nullable=True),
        sa.Column("project_ids_analyzed", sa.JSON(), nullable=True),
        sa.Column("dirty_projects_excluded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agent_frequency_table", sa.JSON(), nullable=True),
        sa.Column("synthesis", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("aggregate_report_id"),
    )
    op.create_index(
        op.f("ix_aggregate_reports_aggregate_report_id"),
        "aggregate_reports",
        ["aggregate_report_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_aggregate_reports_aggregate_report_id"), table_name="aggregate_reports")
    op.drop_table("aggregate_reports")
