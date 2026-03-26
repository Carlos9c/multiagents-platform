"""add plan_version to projects

Revision ID: e6cbc04ce087
Revises: dcc5be4a4ee5
Create Date: 2026-03-26 16:13:15.482876
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e6cbc04ce087"
down_revision: Union[str, Sequence[str], None] = "dcc5be4a4ee5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects",
        sa.Column(
            "plan_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column("projects", "plan_version", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "plan_version")