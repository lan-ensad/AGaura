"""Add session notes

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03 10:10:37.240555

Notes written by the tribune during the assembly, for the minutes (``agaura/models/note.py``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agaura.models.base

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("assembly_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("internal", sa.Boolean(), nullable=False),
        sa.Column("item_position", sa.Integer(), nullable=True),
        sa.Column("item_title", sa.String(length=200), nullable=True),
        sa.Column("created_at", agaura.models.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", agaura.models.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["assembly_id"],
            ["assembly.id"],
            name=op.f("fk_note_assembly_id_assembly"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_note")),
    )
    with op.batch_alter_table("note", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_note_assembly_id"), ["assembly_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("note", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_note_assembly_id"))

    op.drop_table("note")
