from alembic import op
import sqlalchemy as sa


revision = "NEW_REVISION_ID"
down_revision = "ede38d0f9acb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("parent_task_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("proposed_solution", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("implementation_steps", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("tests_required", sa.Text(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("planning_level", sa.String(length=50), nullable=False, server_default="high_level"),
    )
    op.add_column(
        "tasks",
        sa.Column("executor_type", sa.String(length=50), nullable=False, server_default="code_executor"),
    )
    op.add_column("tasks", sa.Column("sequence_order", sa.Integer(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("tasks", sa.Column("blocking_reason", sa.Text(), nullable=True))

    op.create_index(op.f("ix_tasks_parent_task_id"), "tasks", ["parent_task_id"], unique=False)
    op.create_foreign_key(
        "fk_tasks_parent_task_id_tasks",
        "tasks",
        "tasks",
        ["parent_task_id"],
        ["id"],
    )

    op.alter_column("tasks", "planning_level", server_default=None)
    op.alter_column("tasks", "executor_type", server_default=None)
    op.alter_column("tasks", "is_blocked", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_tasks_parent_task_id_tasks", "tasks", type_="foreignkey")
    op.drop_index(op.f("ix_tasks_parent_task_id"), table_name="tasks")

    op.drop_column("tasks", "blocking_reason")
    op.drop_column("tasks", "is_blocked")
    op.drop_column("tasks", "sequence_order")
    op.drop_column("tasks", "executor_type")
    op.drop_column("tasks", "planning_level")
    op.drop_column("tasks", "tests_required")
    op.drop_column("tasks", "implementation_steps")
    op.drop_column("tasks", "proposed_solution")
    op.drop_column("tasks", "parent_task_id")