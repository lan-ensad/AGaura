"""Add the blocks of the projection screen the tribune hides

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05 12:00:00.000000

"Ce que la salle voit" (FR-70) hides or shows each block of the screen: ``assembly.
screen_agenda_hidden`` hides the agenda, ``assembly.screen_join_hidden`` the way to join while
entries are open. Existing sessions show both, as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD COLUMN in place: the batch mode alters the table without copying it.
    with op.batch_alter_table("assembly", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "screen_agenda_hidden", sa.Boolean(), server_default=sa.text("0"), nullable=False
            )
        )
        batch_op.add_column(
            sa.Column(
                "screen_join_hidden", sa.Boolean(), server_default=sa.text("0"), nullable=False
            )
        )


def downgrade() -> None:
    # Native DROP COLUMN (SQLite 3.35+), not a batch copy of the table: rebuilding ``assembly``
    # drops it, and with foreign keys on that deletes every poll and ballot in cascade.
    op.execute('ALTER TABLE "assembly" DROP COLUMN screen_join_hidden')
    op.execute('ALTER TABLE "assembly" DROP COLUMN screen_agenda_hidden')
