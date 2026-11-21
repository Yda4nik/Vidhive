"""agent_deployments.host_key_fp (SSH host key fingerprint, trust on first use)

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "g7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_deployments", sa.Column("host_key_fp", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_deployments", "host_key_fp")
