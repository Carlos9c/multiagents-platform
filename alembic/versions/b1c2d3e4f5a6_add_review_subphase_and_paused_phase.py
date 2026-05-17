"""add_review_subphase_and_paused_phase

Revision ID: b1c2d3e4f5a6
Revises: 667efb597d86
Create Date: 2026-05-16 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "667efb597d86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add review sub-phase tracking and pending clarification storage."""
    op.add_column(
        "conversations",
        sa.Column("review_subphase", sa.String(50), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("pending_clarification_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove review sub-phase tracking and pending clarification storage."""
    op.drop_column("conversations", "pending_clarification_summary")
    op.drop_column("conversations", "review_subphase")
