"""Add the demo flags of accounts and sessions

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05 10:00:00.000000

Demo (FR-09a): ``user.is_demo`` marks an account opened from ``/demo``, deleted with its
sessions once its login ends; ``assembly.is_demo`` marks the sessions of such an account. Existing
accounts and sessions are real ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN in place: the batch mode alters the table without copying it.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_demo", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )
    with op.batch_alter_table("assembly", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_demo", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )


def downgrade() -> None:
    # Native DROP COLUMN (SQLite 3.35+), not a batch copy of the table: rebuilding ``user`` drops
    # it, and with foreign keys on that deletes every session in cascade.
    op.execute('ALTER TABLE "assembly" DROP COLUMN is_demo')
    op.execute('ALTER TABLE "user" DROP COLUMN is_demo')
