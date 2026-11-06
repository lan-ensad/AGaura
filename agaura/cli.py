"""Operator command line: ``uv run agaura --help``.

Commands read the same ``AGAURA_`` settings as the application. Operator actions are written to
the audit log with the actor ``operator``; its chain is keyed with ``AGAURA_SECRET_KEY``, which
``agaura audit verify`` therefore needs too.

Super-admins (who invite and delete accounts on the web, and see nothing of sessions) are
created here only: ``agaura user create EMAIL --superadmin``. The role is fixed at creation; a
super-admin is removed with ``agaura user delete EMAIL``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from alembic.util import CommandError as MigrationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from agaura import backups
from agaura.config import Settings, get_settings
from agaura.db import Database, compact_after_commit
from agaura.models import Assembly, AssemblyStatus, Ballot, Poll, User, UserSession, utcnow
from agaura.services import accounts, assemblies, audit, outbox
from agaura.services.audit import Actor
from agaura.services.errors import NotFoundError, ServiceError


class CommandError(Exception):
    """Reported on standard error with exit status 1."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agaura", description="AGaura operator commands.")
    commands = parser.add_subparsers(dest="command", required=True)

    db = commands.add_parser("db", help="database maintenance")
    db_actions = db.add_subparsers(dest="action", required=True)
    db_actions.add_parser("upgrade", help="apply pending migrations")
    backup = db_actions.add_parser(
        "backup", help="write a compacted copy of the database (refused while a poll is open)"
    )
    backup.add_argument("path", metavar="PATH", type=Path, help="new file to write")
    backup.add_argument(
        "--force", action="store_true", help="copy even while a poll is open (emergency only)"
    )
    db_actions.add_parser("backups", help="list the automatic backups (AGAURA_BACKUP_DIR)")
    check = db_actions.add_parser(
        "check",
        help="check a database copy before restoring it: integrity, migrations, audit log "
        "(keyed with AGAURA_SECRET_KEY); the file is not changed",
    )
    check.add_argument("path", metavar="PATH", type=Path, help="database copy to check")

    audit_parser = commands.add_parser("audit", help="audit log")
    audit_actions = audit_parser.add_subparsers(dest="action", required=True)
    verify = audit_actions.add_parser(
        "verify", help="check the hash chain of the audit log (keyed with AGAURA_SECRET_KEY)"
    )
    verify.add_argument(
        "--anchor",
        metavar="HASH",
        help="also check that the log still holds the entry whose hash a PDF minutes document "
        f"prints (its first {audit.ANCHOR_LENGTH} hexadecimal digits or more)",
    )

    user = commands.add_parser("user", help="accounts")
    user_actions = user.add_subparsers(dest="action", required=True)
    create = user_actions.add_parser("create", help="create an account")
    create.add_argument("email")
    create.add_argument("--name", help="display name")
    create.add_argument(
        "--verified", action="store_true", help="mark the email address as confirmed"
    )
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of standard input",
    )
    create.add_argument(
        "--superadmin",
        action="store_true",
        help="super-admin: invites and deletes accounts on the web, never sees sessions "
        "(email address marked as confirmed)",
    )
    user_actions.add_parser(
        "delete", help="delete an account and all its sessions (refused while in session)"
    ).add_argument("email")
    for action, text in (("disable", "disable an account"), ("enable", "enable an account")):
        user_actions.add_parser(action, help=text).add_argument("email")
    quota = user_actions.add_parser("quota", help="show or change the quotas of an account")
    quota.add_argument("email")
    for name in accounts.QUOTA_NAMES:
        quota.add_argument(f"--{name.replace('_', '-')}", type=int, metavar="N", dest=name)
    quota.add_argument("--reset", action="store_true", help="restore the default quotas first")

    email = commands.add_parser("email", help="emails of the outbox")
    email_actions = email.add_subparsers(dest="action", required=True)
    email_actions.add_parser("failed", help="list the emails given up (kept 90 days)")
    retry = email_actions.add_parser(
        "retry", help="send given-up emails again, once the cause is fixed"
    )
    retry.add_argument("ids", metavar="ID", type=int, nargs="*", help="email numbers")
    retry.add_argument("--all", action="store_true", help="every email given up")

    session = commands.add_parser("session", help="assembly sessions")
    session_actions = session.add_subparsers(dest="action", required=True)
    delete = session_actions.add_parser("delete", help="delete a session and all its data")
    delete.add_argument("public_id", metavar="PUBLIC_ID")
    delete.add_argument("--force", action="store_true", help="delete even while in session")

    commands.add_parser("stats", help="platform statistics (counts only)")
    return parser


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Password again: ") != password:
        raise CommandError("The passwords do not match.")
    return password


def _user(database: Database, args: argparse.Namespace) -> int:
    if args.action == "create":
        password = _read_password(args.password_stdin)
        with database.write() as db:
            user = accounts.create_user_by_operator(
                db,
                args.email,
                password,
                display_name=args.name,
                verified=args.verified,
                superadmin=args.superadmin,
            )
        if user.is_superadmin:
            print(f"Super-admin {user.email} created: it manages accounts at /superadmin.")
        else:
            state = "email confirmed" if args.verified else "email not confirmed"
            print(f"Account {user.email} created ({state}).")
    elif args.action == "delete":
        with database.write() as db:
            count = accounts.delete_account_by_operator(db, args.email)
        print(f"Account {args.email.strip().lower()} deleted with its {count} session(s).")
    elif args.action in {"disable", "enable"}:
        with database.write() as db:
            user = accounts.set_disabled(db, args.email, args.action == "disable")
        print(f"Account {user.email} {args.action}d.")
    else:
        changes = {
            name: getattr(args, name)
            for name in accounts.QUOTA_NAMES
            if getattr(args, name) is not None
        }
        if changes or args.reset:
            with database.write() as db:
                limits = accounts.set_quotas(db, args.email, changes, reset=args.reset)
        else:
            with database.read() as db:
                limits = accounts.quotas(accounts.find_user(db, args.email))
        for name in accounts.QUOTA_NAMES:
            print(f"{name}: {getattr(limits, name)}")
    return 0


def _delete_session(db: Session, public_id: str, *, force: bool) -> Assembly:
    assembly = assemblies.find_assembly(db, public_id)
    if assembly is None:
        raise NotFoundError("assembly")
    if assembly.status is AssemblyStatus.IN_SESSION and not force:
        raise CommandError(
            f"Session {assembly.public_id} is in session; add --force to delete it anyway."
        )
    audit.record(
        db,
        "assembly.deleted",
        actor=Actor.OPERATOR,
        user_id=assembly.owner_id,
        assembly_id=assembly.id,
        details={"public_id": assembly.public_id, "status": assembly.status.value},
    )
    db.delete(assembly)
    db.flush()
    compact_after_commit(db)
    return assembly


def _count(db: Session, model: type, *conditions: object) -> int:
    query = select(func.count()).select_from(model)
    if conditions:
        query = query.where(*conditions)  # type: ignore[arg-type]
    return db.scalar(query) or 0


def _stats(db: Session) -> list[str]:
    # Demo sessions (FR-09a) are counted apart: "in_session" is the deployment guard
    # (deploy/update.sh, SPEC §10), like /health, and a demo never holds a deployment back.
    by_status = dict(
        db.execute(
            select(Assembly.status, func.count())
            .where(Assembly.is_demo.is_(False))
            .group_by(Assembly.status)
        ).all()
    )
    return [
        f"accounts: {_count(db, User)}",
        f"  verified: {_count(db, User, User.email_verified_at.is_not(None))}",
        f"  disabled: {_count(db, User, User.disabled_at.is_not(None))}",
        f"  invitations pending: {_count(db, User, User.password_hash.is_(None))}",
        f"  super-admins: {_count(db, User, User.is_superadmin)}",
        f"  demo: {_count(db, User, User.is_demo)}",
        f"  login sessions: {_count(db, UserSession)}",
        f"sessions: {sum(by_status.values())}",
        *(f"  {status.value}: {by_status.get(status, 0)}" for status in AssemblyStatus),
        f"demo sessions: {_count(db, Assembly, Assembly.is_demo)}",
        f"polls: {_count(db, Poll)}",
        f"ballots: {_count(db, Ballot)}",
    ]


BACKUP_DURING_VOTE = (
    "A poll is open: copies taken during a vote, compared with each other, show which ballots "
    "and which participations arrived in between. Back up once no poll is open, or add --force "
    "for an emergency copy."
)


def _backup(database: Database, path: Path, *, force: bool) -> int:
    """Compacted copy (``agaura.backups.write_copy``); refused while a poll is open unless
    forced."""
    if not database.has_file:
        raise CommandError("Backups need a SQLite database file.")
    if path.exists():
        raise CommandError(f"{path} already exists; choose a new file name.")
    try:
        open_polls = backups.write_copy(database, path, force=force)
    except backups.BackupRefusedError as error:
        raise CommandError(BACKUP_DURING_VOTE) from error
    except (DBAPIError, sqlite3.Error) as error:
        raise CommandError(f"Backup failed: {getattr(error, 'orig', None) or error}.") from error
    if open_polls:
        print(f"Warning: the copy contains {open_polls} open poll(s).", file=sys.stderr)
    print(f"Backup written to {path}.")
    return 0


def _size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MiB" if size >= 1024 * 1024 else f"{size / 1024:.0f} KiB"


def _age(delta: dt.timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return f"{max(int(delta.total_seconds() // 60), 0)} min ago"
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def _list_backups(settings: Settings) -> int:
    """``agaura db backups``: the copies of the background job, oldest first."""
    directory = settings.backup_dir
    if directory is None:
        print("Automatic backups are off: AGAURA_BACKUP_DIR is not set.")
        return 0
    try:
        found = backups.list_backups(directory) if directory.exists() else []
    except OSError as error:
        raise CommandError(f"Cannot read {directory}: {error}.") from error
    print(
        f"Automatic backups in {directory}: once a day and after each poll closing, "
        f"kept {settings.backup_keep_days} days."
    )
    for backup in found:
        print(f"  {backup.path.name}  {backup.taken_at:%Y-%m-%d %H:%M} UTC  {_size(backup.size)}")
    if not found:
        print("No backup yet: the first one is written within a minute of the start.")
    else:
        print(f"Latest: {_age(utcnow() - found[-1].taken_at)}.")
    return 0


def _revision(path: Path) -> str | None:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise CommandError(f"{path} is damaged (PRAGMA integrity_check).")
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError as error:
        raise CommandError(f"{path} is not an AGaura database: {error}.") from error
    finally:
        connection.close()
    return row[0] if row else None


def _check_copy(path: Path, settings: Settings) -> int:
    """``agaura db check PATH``: what a restore would bring back, checked on a temporary copy
    migrated as the application would at start; ``PATH`` itself is only read."""
    if not path.is_file():
        raise CommandError(f"No file {path}.")
    with path.open("rb") as file:
        if file.read(16) != b"SQLite format 3\0":
            raise CommandError(f"{path} is not an SQLite database.")
    revision = _revision(path)
    open_polls = backups.open_polls_in_file(path)
    with tempfile.TemporaryDirectory(prefix="agaura-check-") as scratch:
        copy = Path(scratch) / "check.db"
        shutil.copyfile(path, copy)
        database = Database(f"sqlite:///{copy}", audit_key=settings.audit_key)
        try:
            try:
                database.upgrade()
            except MigrationError as error:
                raise CommandError(
                    f"{path} comes from a newer version of AGaura (schema {revision}): "
                    "restore it with that version."
                ) from error
            with database.read() as db:
                report = audit.verify_chain(db)
                counts = _stats(db)
                closing = backups.last_closing(db)
        finally:
            database.dispose()
    print(f"{path}: intact, schema {revision}.")
    print("\n".join(counts))
    if closing is not None:
        print(f"latest poll closing: {closing:%Y-%m-%d %H:%M} UTC")
    if open_polls:
        print(
            f"Warning: the copy contains {open_polls} open poll(s): taken with --force during "
            "a vote. Restored, they stay open.",
            file=sys.stderr,
        )
    if not report.ok:
        print(
            f"Audit log BROKEN at entry {report.broken_at} "
            f"({report.checked} entries verified before it).",
            file=sys.stderr,
        )
        print(BROKEN_CHAIN_HINT, file=sys.stderr)
        return 1
    print(f"Audit log OK: {report.checked} entries, chain intact.")
    return 0


def _email(database: Database, args: argparse.Namespace) -> int:
    """``agaura email failed`` and ``agaura email retry``: addresses are shown to the operator
    here, never written to the logs."""
    if args.action == "retry":
        if args.all == bool(args.ids):
            raise CommandError("Give the numbers of the emails to retry, or --all.")
        with database.write() as db:
            count = outbox.retry(db, None if args.all else args.ids)
        print(f"{count} email(s) queued again.")
        return 0
    with database.read() as db:
        failed = outbox.failed_emails(db)
    if not failed:
        print("No email given up.")
        return 0
    print(f"{len(failed)} email(s) given up (kept 90 days):")
    for email in failed:
        when = f"{email.failed_at:%Y-%m-%d %H:%M} UTC" if email.failed_at else "?"
        print(
            f"  #{email.id}  {when}  {email.attempts} attempt(s)  to {email.to_address}  "
            f"{email.subject!r}"
        )
        print(f"      {email.last_error or 'no error recorded'}")
    print("Once the cause is fixed: agaura email retry --all (or the numbers).")
    return 0


def _run(database: Database, args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "db":
        if args.action == "backup":
            return _backup(database, args.path, force=args.force)
        if args.action == "backups":
            return _list_backups(settings)
        if args.action == "check":
            return _check_copy(args.path, settings)
        database.upgrade()
        print("Database is up to date.")
        return 0
    if args.command == "user":
        return _user(database, args)
    if args.command == "email":
        return _email(database, args)
    if args.command == "session":
        with database.write() as db:
            assembly = _delete_session(db, args.public_id, force=args.force)
        print(f"Session {assembly.public_id} deleted.")
        return 0
    if args.command == "stats":
        with database.read() as db:
            print("\n".join(_stats(db)))
        return 0
    return _verify(database, args.anchor)


DAMAGED_HINT = (
    'If the database is damaged ("malformed"), restore a backup: deploy/restore.sh --force '
    "(docs/RUNBOOK.md)."
)
BROKEN_CHAIN_HINT = (
    "Either the log was altered, or AGAURA_SECRET_KEY is not the key in use when these entries "
    "were written: the chain is keyed with it, so a changed key fails from the first entry."
)
MISSING_ANCHOR = (
    "Anchor {anchor} NOT FOUND: no entry of the log has this hash. Entries were removed or "
    "rewritten since the document printing it was produced, or it comes from another server."
)


def _verify(database: Database, anchor: str | None) -> int:
    """``agaura audit verify``: the keyed chain, then the anchor of a PDF minutes document."""
    if anchor is not None:
        anchor = anchor.strip().lower()
        if not audit.is_anchor(anchor):
            raise CommandError(
                f"The anchor is the hash printed in PDF minutes: {audit.ANCHOR_LENGTH} to 64 "
                "hexadecimal digits."
            )
    with database.read() as db:
        report = audit.verify_chain(db)
        anchored = audit.find_anchor(db, anchor) if anchor and report.ok else None
    if not report.ok:
        print(
            f"Audit log BROKEN at entry {report.broken_at} "
            f"({report.checked} entries verified before it).",
            file=sys.stderr,
        )
        print(BROKEN_CHAIN_HINT, file=sys.stderr)
        return 1
    print(f"Audit log OK: {report.checked} entries, chain intact.")
    if anchor is None:
        return 0
    if anchored is None:
        print(MISSING_ANCHOR.format(anchor=anchor), file=sys.stderr)
        return 1
    print(f"Anchor {anchor} found: entry {anchored}.")
    return 0


ERROR_TEXTS = {
    "not_found": "No such {what}.",
    "invalid_email": "Invalid email address.",
    "email_taken": "An account already uses this email address.",
    "password_too_short": f"The password needs at least {accounts.MIN_PASSWORD_LENGTH} characters.",
    "password_too_long": "The password is too long.",
    "too_long": "The display name is too long.",
    "invalid_quota": "Invalid quota value (must be zero or more).",
    "account_in_session": (
        "A session of this account is in session; close it, or delete it with "
        "'agaura session delete PUBLIC_ID --force', first."
    ),
}


def _describe(error: ServiceError) -> str:
    text = ERROR_TEXTS.get(error.code, error.code)
    return text.format(what=error.detail or "item") if error.code == "not_found" else text


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    database = Database(settings.sqlalchemy_url, audit_key=settings.audit_key)
    try:
        return _run(database, args, settings)
    except (ServiceError, CommandError) as error:
        message = _describe(error) if isinstance(error, ServiceError) else str(error)
        print(f"Error: {message}", file=sys.stderr)
        return 1
    except (DBAPIError, sqlite3.Error) as error:
        # A damaged or locked database: SQLite's own words, never the statement's parameters
        # (``hide_parameters``), instead of a traceback.
        print(f"Error: database: {getattr(error, 'orig', None) or error}.", file=sys.stderr)
        print(DAMAGED_HINT, file=sys.stderr)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
