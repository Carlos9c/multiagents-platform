"""merge_runtime_spec_and_last_planned_at_heads

Revision ID: 21043d216e64
Revises: f1a2b3c4d5e6, a2b3c4d5e6f7
Create Date: 2026-05-10 11:20:30.609330

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '21043d216e64'
down_revision: Union[str, Sequence[str], None] = ('f1a2b3c4d5e6', 'a2b3c4d5e6f7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
