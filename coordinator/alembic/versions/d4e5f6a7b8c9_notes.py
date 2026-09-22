"""notes table (personal timeline markers)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.BigInteger(), nullable=False),
        sa.Column("t_seconds", sa.Float(), nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("color", sa.String(length=20), nullable=False, server_default="#7c9cff"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_notes_user_external", "notes", ["user_id", "external_id"])


def downgrade() -> None:
    op.drop_index("ix_notes_user_external", table_name="notes")
    op.drop_table("notes")
