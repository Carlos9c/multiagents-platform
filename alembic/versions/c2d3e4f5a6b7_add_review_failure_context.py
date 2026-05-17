"""add_review_failure_context

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-05-16 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add review_failure_context column for project-level review episodes."""
    op.add_column(
        "conversations",
        sa.Column("review_failure_context", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove review_failure_context column."""
    op.drop_column("conversations", "review_failure_context")
