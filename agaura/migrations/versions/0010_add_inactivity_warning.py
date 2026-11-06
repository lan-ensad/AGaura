"""Add the time an inactive account was warned of its deletion

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-05 20:00:00.000000

Retention (D12): an account inactive for 3 years is deleted 30 days after an email warning;
``user.inactivity_warned_at`` records when that warning was queued. A warning older than the
account's latest activity no longer counts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agaura.models.base

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN in place: the batch mode alters the table without copying it.
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("inactivity_warned_at", agaura.models.base.UTCDateTime(), nullable=True)
        )


def downgrade() -> None:
    # Native DROP COLUMN (SQLite 3.35+), not a batch copy of the table: rebuilding ``user`` drops
    # it, and with foreign keys on that deletes every session in cascade.
    op.execute('ALTER TABLE "user" DROP COLUMN inactivity_warned_at')
