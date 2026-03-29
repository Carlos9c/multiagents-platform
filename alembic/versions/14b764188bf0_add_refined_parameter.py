"""add refined parameter

Revision ID: 14b764188bf0
Revises: bef2798eaab3
Create Date: 2026-03-25 21:07:45.433970

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "14b764188bf0"
down_revision: Union[str, Sequence[str], None] = "bef2798eaab3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "enable_technical_refinement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "projects",
        "enable_technical_refinement",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("projects", "enable_technical_refinement")
