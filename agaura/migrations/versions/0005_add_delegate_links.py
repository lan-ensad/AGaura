"""Add delegate links

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 14:22:40.623045

Console rights of one session handed to someone without an account
(``agaura/models/delegate.py``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agaura.models.base

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delegate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("assembly_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("rights", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("token_enc", sa.String(length=500), nullable=True),
        sa.Column("created_at", agaura.models.base.UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", agaura.models.base.UTCDateTime(), nullable=True),
        sa.Column("ended_at", agaura.models.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["assembly_id"],
            ["assembly.id"],
            name=op.f("fk_delegate_assembly_id_assembly"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delegate")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_delegate_token_hash")),
    )
    with op.batch_alter_table("delegate", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_delegate_assembly_id"), ["assembly_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("delegate", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_delegate_assembly_id"))

    op.drop_table("delegate")
