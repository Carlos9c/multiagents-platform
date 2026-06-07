"""add_review_episode_anchor

Add review_episode_start_message_id to conversations to anchor the review
episode boundary to a specific message ID rather than scanning for the last
system message.

Revision ID: f2a3b4c5d6e7
Revises: e8f9a0b1c2d3
Create Date: 2026-06-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "review_episode_start_message_id",
            sa.Integer(),
            sa.ForeignKey("conversation_messages.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "review_episode_start_message_id")
