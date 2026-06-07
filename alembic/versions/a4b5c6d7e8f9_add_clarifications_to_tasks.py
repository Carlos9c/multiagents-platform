"""add_clarifications_to_tasks

Add a structured JSON column tasks.clarifications to store per-episode
user clarifications alongside the free-text embedding in task.description.

Each entry: {"text": str, "created_at": ISO-8601 str, "episode_index": int}.

Revision ID: a4b5c6d7e8f9
Revises: f2a3b4c5d6e7
Create Date: 2026-06-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("clarifications", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "clarifications")
