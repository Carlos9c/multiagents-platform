"""add_verification_level_to_tasks

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add verification_level column to tasks table."""
    op.add_column(
        "tasks",
        sa.Column(
            "verification_level",
            sa.String(50),
            nullable=False,
            server_default="runtime",
        ),
    )


def downgrade() -> None:
    """Remove verification_level column from tasks table."""
    op.drop_column("tasks", "verification_level")
