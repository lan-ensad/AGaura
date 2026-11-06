"""Add the time a result is shown on the screen

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04 14:33:57.198295

Results on the projection screen (FR-75): ``poll.shown_on_screen`` (initial schema) is read
again, and ``poll.screen_shown_at`` orders the results shown, the latest on the stage.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agaura.models.base

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("poll", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("screen_shown_at", agaura.models.base.UTCDateTime(), nullable=True)
        )
    # Before FR-75 the screen never read the flag: nothing may appear on a screen by itself.
    op.execute("UPDATE poll SET shown_on_screen = 0")


def downgrade() -> None:
    with op.batch_alter_table("poll", schema=None) as batch_op:
        batch_op.drop_column("screen_shown_at")
