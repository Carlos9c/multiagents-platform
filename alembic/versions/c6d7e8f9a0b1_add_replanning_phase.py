"""add_replanning_phase

Add "replanning" to the valid conversation phases.

The phase column is String(50) with no DB-level CHECK constraint — validation is
enforced at the ORM layer via VALID_CONVERSATION_PHASES. This migration is therefore
a no-op for upgrade (the value is accepted by the existing column) and cleans up
any transient "replanning" rows for downgrade safety.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-06-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "b5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No DDL change needed — String(50) already accepts the new value.
    pass


def downgrade() -> None:
    # Clean up any rows left in the transient "replanning" state by moving them
    # back to "reviewing" (the phase they were in before the LLM call started).
    op.execute("UPDATE conversations SET phase = 'reviewing' WHERE phase = 'replanning'")
