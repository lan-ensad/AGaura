"""Prepare the local database for a manual test: a verified account, a session with drafts and,
the first time, a past session already voted on (history, minutes, exports, copy of polls), plus
a super-admin that invites and deletes accounts at ``/superadmin`` and a delegate link of the new
session (session notes, chronometer) to open without an account.

    uv run python scripts/dev_seed.py
    uv run python scripts/dev_seed.py --email me@example.org --password "my long password"

Uses the application settings (``data/agaura.db`` by default). Running it again reuses the
accounts and adds a new session with drafts. Development only: refuses to run in production.
"""

from __future__ import annotations

import argparse

from sqlalchemy import func, select

from agaura.config import get_settings
from agaura.db import Database
from agaura.models import Assembly, AssemblyStatus, Poll, PollStatus, User
from agaura.security import Keyring
from agaura.services import accounts, demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default="demo@agaura.local")
    parser.add_argument("--password", default="demo-password-2026")
    parser.add_argument("--superadmin-email", default="superadmin@agaura.local")
    parser.add_argument("--superadmin-password", default="superadmin-password-2026")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.is_production:
        parser.error("development only")
    database = Database(settings.sqlalchemy_url, audit_key=settings.audit_key)
    database.upgrade()
    keyring = Keyring(settings.secret_key.get_secret_value())
    with database.write() as db:
        owner = db.scalar(select(User).where(User.email == args.email.lower()))
        if owner is None:
            owner = accounts.create_user(
                db, args.email, args.password, display_name="Démo", verified=True
            )
        public_id, delegate_token = demo.preparation_session(db, keyring, owner)
        drafts = db.scalar(
            select(func.count())
            .select_from(Poll)
            .join(Assembly, Poll.assembly_id == Assembly.id)
            .where(Assembly.public_id == public_id, Poll.status == PollStatus.DRAFT)
        )
        if db.scalar(select(User.id).where(User.email == args.superadmin_email.lower())) is None:
            accounts.create_user_by_operator(
                db, args.superadmin_email, args.superadmin_password, superadmin=True
            )
        has_past = db.scalar(
            select(Assembly.id).where(
                Assembly.owner_id == owner.id, Assembly.status == AssemblyStatus.CLOSED
            )
        )
        past_id = None if has_past else demo.past_session(db, keyring, owner)
    database.dispose()
    print(f"Account : {args.email} / {args.password}")
    print(f"Super   : {args.superadmin_email} / {args.superadmin_password} -> /superadmin")
    print(f"Session : {public_id} ({drafts} drafts) -> /manage/{public_id}")
    print(f"Delegate: notes, chronometer -> /manage/{public_id}/delegate#k={delegate_token}")
    if past_id:
        print(f"Past    : {past_id} (closed, {demo.PAST_VOTERS} voters) -> /manage/{past_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
