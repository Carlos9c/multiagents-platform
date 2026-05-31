"""add_producer_agent_and_probes

Add producer_agent to qa_findings and probes JSON to qa_sessions so that
supervisor evaluators can attribute individual findings to the specific QA
agent that produced them and read per-agent probe outcomes.

Revision ID: b2c3d4e5f6a7
Revises: f1b2c3d4e5f6
Branch labels: None
Depends on: None

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # qa_findings: attribute each finding to its producing QA agent
    op.add_column(
        "qa_findings",
        sa.Column("producer_agent", sa.String(50), nullable=True),
    )

    # qa_sessions: persist per-agent probe outcome list as JSON
    op.add_column(
        "qa_sessions",
        sa.Column("probes", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qa_sessions", "probes")
    op.drop_column("qa_findings", "producer_agent")
