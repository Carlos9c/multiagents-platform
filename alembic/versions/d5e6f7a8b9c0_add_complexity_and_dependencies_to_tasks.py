"""add_complexity_and_dependencies_to_tasks

Add estimated_complexity (VARCHAR 10) and depends_on_task_titles (JSON) to the
tasks table.  These fields are populated by the atomic_task_generator (Phase 5B/5C)
and consumed by the execution_sequencer for batch balancing and ground-truth
dependency resolution.  Both are nullable so existing rows are unaffected.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-05-31 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("estimated_complexity", sa.String(10), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("depends_on_task_titles", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "depends_on_task_titles")
    op.drop_column("tasks", "estimated_complexity")
