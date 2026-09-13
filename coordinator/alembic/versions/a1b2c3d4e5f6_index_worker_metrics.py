"""index worker_metrics(worker_id, captured_at)

Revision ID: a1b2c3d4e5f6
Revises: 675d005ad046
Create Date: 2026-09-13

Speeds up the "latest metric per worker" lookup (was a growing Seq Scan).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "675d005ad046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_worker_metrics_worker_captured",
        "worker_metrics",
        ["worker_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_metrics_worker_captured", table_name="worker_metrics")
