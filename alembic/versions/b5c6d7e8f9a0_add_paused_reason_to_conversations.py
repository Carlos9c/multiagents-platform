"""add_paused_reason_to_conversations

Add paused_reason TEXT column to conversations to store the project-level
failure reason when execution is paused after a review abandonment.
Used by Aria and ProjectSnapshot to explain to the user why the project is paused.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-06-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("paused_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "paused_reason")
