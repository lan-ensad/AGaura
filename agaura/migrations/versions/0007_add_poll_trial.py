"""Add the trial flag of a poll

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04 15:37:29.903359

Trial vote of the preparation checklist (D-I): ``poll.trial`` marks a poll run only to test the
room's network, left out of the minutes, the session export and the public results. Existing
polls are real ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("poll", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("trial", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("poll", schema=None) as batch_op:
        batch_op.drop_column("trial")
