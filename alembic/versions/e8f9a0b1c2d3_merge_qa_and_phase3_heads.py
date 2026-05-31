"""merge_qa_and_phase3_heads

Merge migration reconciling two branches:
- b2c3d4e5f6a7 (add_producer_agent_and_probes)
- d5e6f7a8b9c0 (add_complexity_and_dependencies_to_tasks, via Phase 3 acceptance_criteria branch)

Revision ID: e8f9a0b1c2d3
Revises: b2c3d4e5f6a7, d5e6f7a8b9c0
Create Date: 2026-05-31 00:00:00.000000

"""

from typing import Sequence, Union

revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = ("b2c3d4e5f6a7", "d5e6f7a8b9c0")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
