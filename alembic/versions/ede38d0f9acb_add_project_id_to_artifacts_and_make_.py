"""add project_id to artifacts and make task_id optional

Revision ID: ede38d0f9acb
Revises: 5bcd9435eed5
Create Date: 2026-03-21 02:03:53.674470

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ede38d0f9acb'
down_revision: Union[str, Sequence[str], None] = '5bcd9435eed5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Añadir project_id como nullable al principio
    op.add_column("artifacts", sa.Column("project_id", sa.Integer(), nullable=True))

    # 2. Rellenar project_id usando la relación artifacts.task_id -> tasks.project_id
    op.execute("""
        UPDATE artifacts AS a
        SET project_id = t.project_id
        FROM tasks AS t
        WHERE a.task_id = t.id
    """)

    # 3. Convertir task_id en nullable
    op.alter_column("artifacts", "task_id", existing_type=sa.INTEGER(), nullable=True)

    # 4. Convertir project_id en NOT NULL
    op.alter_column("artifacts", "project_id", existing_type=sa.INTEGER(), nullable=False)

    # 5. Crear FK e índice para project_id
    op.create_foreign_key(
        "fk_artifacts_project_id_projects",
        "artifacts",
        "projects",
        ["project_id"],
        ["id"],
    )
    op.create_index(op.f("ix_artifacts_project_id"), "artifacts", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_artifacts_project_id"), table_name="artifacts")
    op.drop_constraint("fk_artifacts_project_id_projects", "artifacts", type_="foreignkey")
    op.alter_column("artifacts", "task_id", existing_type=sa.INTEGER(), nullable=False)
    op.drop_column("artifacts", "project_id")
