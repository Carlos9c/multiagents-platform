"""acceptance_criteria_json_column

Convert tasks.acceptance_criteria from TEXT to JSON to support structured
list[str] acceptance criteria produced by planner and atomic_task_generator
agents (Phase 3). Existing plain-text values are migrated as single-element
JSON arrays so no criteria are lost.

Revision ID: c4d5e6f7a8b9
Revises: f1b2c3d4e5f6
Create Date: 2026-05-31 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "f1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new JSON column alongside the existing TEXT column
    op.add_column(
        "tasks",
        sa.Column("acceptance_criteria_json", sa.JSON(), nullable=True),
    )

    # Migrate existing TEXT data into the new JSON column as single-element arrays
    op.execute("""
        UPDATE tasks
        SET acceptance_criteria_json = json_build_array(acceptance_criteria)
        WHERE acceptance_criteria IS NOT NULL
        """)

    # Drop the old TEXT column
    op.drop_column("tasks", "acceptance_criteria")

    # Rename the new column to the canonical name
    op.alter_column(
        "tasks",
        "acceptance_criteria_json",
        new_column_name="acceptance_criteria",
    )


def downgrade() -> None:
    # Add a temporary TEXT column
    op.add_column(
        "tasks",
        sa.Column("acceptance_criteria_text", sa.Text(), nullable=True),
    )

    # Extract the first element of the JSON array as plain text
    op.execute("""
        UPDATE tasks
        SET acceptance_criteria_text = acceptance_criteria->>0
        WHERE acceptance_criteria IS NOT NULL
        """)

    # Drop the JSON column
    op.drop_column("tasks", "acceptance_criteria")

    # Rename the TEXT column back
    op.alter_column(
        "tasks",
        "acceptance_criteria_text",
        new_column_name="acceptance_criteria",
    )
