"""normalize task_type 'test' to 'testing'

Revision ID: a1b2c3d4e5f6
Revises: dcc5be4a4ee5
Create Date: 2026-05-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "dcc5be4a4ee5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE tasks SET task_type = 'testing' WHERE task_type = 'test'")


def downgrade() -> None:
    op.execute("UPDATE tasks SET task_type = 'test' WHERE task_type = 'testing'")
