"""range_chunks.url (direct download URL for link-based work, e.g. YouTube)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("range_chunks", sa.Column("url", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("range_chunks", "url")
