"""Add the super-admin role and pending invitations

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03 07:40:53.678220

``email_token.kind`` is a plain string column without a CHECK constraint, wide enough for the
new ``invite`` kind: only the downgrade has to care about it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_superadmin", sa.Boolean(), server_default=sa.text("0"), nullable=False)
        )
        # NULL until an invited person chooses a password.
        batch_op.alter_column("password_hash", existing_type=sa.VARCHAR(length=255), nullable=True)


def downgrade() -> None:
    # Pending invitations get an unusable hash (never matches a password); their links go.
    op.execute("DELETE FROM email_token WHERE kind = 'invite'")
    op.execute("UPDATE \"user\" SET password_hash = '!' WHERE password_hash IS NULL")
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.VARCHAR(length=255), nullable=False)
        batch_op.drop_column("is_superadmin")
