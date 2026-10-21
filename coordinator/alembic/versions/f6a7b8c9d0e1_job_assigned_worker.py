"""jobs.assigned_worker_id (personal jobs for one specific agent)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("assigned_worker_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_jobs_assigned_worker", "workers", ["assigned_worker_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("fk_jobs_assigned_worker", type_="foreignkey")
        batch.drop_column("assigned_worker_id")
