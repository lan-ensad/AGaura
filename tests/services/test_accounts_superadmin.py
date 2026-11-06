"""Super-admin: invitations, account list, deletion; pending accounts at login and reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from agaura.db import Database
from agaura.models import Assembly, AuditEntry, EmailToken, EmailTokenKind, OutboxEmail, User
from agaura.security import hash_token, verify_password
from agaura.services import accounts, assemblies, audit, auth, outbox
from agaura.services.accounts import AccountState
from agaura.services.assemblies import AssemblyInput
from agaura.services.errors import InvalidInputError, InvalidStateError, NotFoundError
from tests.conftest import World

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
WEEK = timedelta(days=7)


@pytest.fixture
def admin(database: Database) -> User:
    with database.write() as db:
        return accounts.create_user_by_operator(
            db, "root@example.org", PASSWORD, superadmin=True, now=NOW
        )


def invite(database: Database, admin: User, email: str = "new@example.org") -> tuple[User, str]:
    with database.write() as db:
        return accounts.invite(db, admin, email, display_name=" Camille ", now=NOW)


def reload(database: Database, user_id: int) -> User | None:
    with database.read() as db:
        return db.get(User, user_id)


def entries(database: Database) -> list[AuditEntry]:
    with database.read() as db:
        return list(db.scalars(select(AuditEntry).order_by(AuditEntry.id)))


class TestInvitation:
    def test_creates_an_account_without_password(self, database: Database, admin: User) -> None:
        user, token = invite(database, admin, " New@Example.org ")
        assert (user.email, user.display_name) == ("new@example.org", "Camille")
        assert user.password_hash is None and user.email_verified_at is None
        assert not user.is_superadmin
        assert accounts.account_state(user) is AccountState.PENDING
        with database.read() as db:
            row = db.scalar(select(EmailToken))
        assert row is not None and row.kind is EmailTokenKind.INVITE
        assert row.token_hash == hash_token(token) and row.expires_at == NOW + WEEK
        last = entries(database)[-1]
        assert (last.action, last.actor, last.user_id) == (
            "account.invited",
            accounts.SUPERADMIN_ACTOR,
            user.id,
        )
        assert last.details == {"by": admin.id}

    def test_accept_sets_the_password_and_confirms_the_address(
        self, database: Database, admin: User
    ) -> None:
        user, token = invite(database, admin)
        with database.write() as db:
            accepted = accounts.accept_invitation(db, token, PASSWORD, now=NOW + timedelta(hours=1))
        assert accepted.id == user.id
        stored = reload(database, user.id)
        assert stored is not None and stored.password_hash is not None
        assert verify_password(stored.password_hash, PASSWORD)
        assert stored.email_verified_at == NOW + timedelta(hours=1)
        assert accounts.account_state(stored) is AccountState.ACTIVE
        assert entries(database)[-1].action == "account.invitation_accepted"
        # Single use.
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.accept_invitation(db, token, "another long passphrase", now=NOW)

    def test_valid_for_seven_days(self, database: Database, admin: User) -> None:
        _user, token = invite(database, admin)
        with database.read() as db:
            last_moment = NOW + WEEK - timedelta(seconds=1)
            accounts.check_token(db, EmailTokenKind.INVITE, token, now=last_moment)
        with pytest.raises(InvalidInputError, match="expired_link"), database.write() as db:
            accounts.accept_invitation(db, token, PASSWORD, now=NOW + WEEK)

    def test_password_rules_apply_and_keep_the_link(self, database: Database, admin: User) -> None:
        user, token = invite(database, admin)
        with pytest.raises(InvalidInputError, match="password_too_short"), database.write() as db:
            accounts.accept_invitation(db, token, "short", now=NOW)
        with database.read() as db:
            assert accounts.check_token(db, EmailTokenKind.INVITE, token, now=NOW).id == user.id

    def test_resend_invalidates_the_previous_link(self, database: Database, admin: User) -> None:
        user, first = invite(database, admin)
        with database.write() as db:
            _same, second = accounts.resend_invitation(db, admin, user.id, now=NOW + WEEK)
        assert second != first
        with pytest.raises(InvalidInputError, match="invalid_link"), database.read() as db:
            accounts.check_token(db, EmailTokenKind.INVITE, first, now=NOW + WEEK)
        with database.read() as db:
            accounts.check_token(db, EmailTokenKind.INVITE, second, now=NOW + 2 * WEEK - WEEK / 7)
        last = entries(database)[-1]
        assert last.action == "account.invitation_resent" and last.details == {"by": admin.id}

    def test_resend_only_while_pending(self, database: Database, admin: User) -> None:
        user, token = invite(database, admin)
        with database.write() as db:
            accounts.accept_invitation(db, token, PASSWORD, now=NOW)
        with pytest.raises(InvalidStateError, match="not_pending"), database.write() as db:
            accounts.resend_invitation(db, admin, user.id, now=NOW)
        other, _token = invite(database, admin, "other@example.org")
        with database.write() as db:
            accounts.set_disabled(db, other.email, True, now=NOW)
        with pytest.raises(InvalidStateError, match="not_pending"), database.write() as db:
            accounts.resend_invitation(db, admin, other.id, now=NOW)
        with pytest.raises(InvalidStateError, match="superadmin_protected"), database.write() as db:
            accounts.resend_invitation(db, admin, admin.id, now=NOW)
        with pytest.raises(NotFoundError), database.write() as db:
            accounts.resend_invitation(db, admin, 999, now=NOW)

    def test_taken_or_invalid_address(self, database: Database, admin: User) -> None:
        invite(database, admin)
        for email, code in [("NEW@example.org", "email_taken"), ("nope", "invalid_email")]:
            with pytest.raises(InvalidInputError, match=code), database.write() as db:
                accounts.invite(db, admin, email, now=NOW)

    def test_never_replaces_a_chosen_password(self, database: Database, admin: User) -> None:
        with database.write() as db:
            user = accounts.create_user(db, "active@example.org", PASSWORD, now=NOW)
            token = accounts.issue_token(db, user, EmailTokenKind.INVITE, now=NOW)
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.accept_invitation(db, token, "another long passphrase", now=NOW)
        stored = reload(database, user.id)
        assert stored is not None and stored.password_hash is not None
        assert verify_password(stored.password_hash, PASSWORD)

    def test_disabled_account_link_stops_working(self, database: Database, admin: User) -> None:
        user, token = invite(database, admin)
        with database.write() as db:
            accounts.set_disabled(db, user.email, True, now=NOW)
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.accept_invitation(db, token, PASSWORD, now=NOW)

    def test_audit_entries_hold_no_secret(self, database: Database, admin: User) -> None:
        user, token = invite(database, admin)
        with database.write() as db:
            accounts.resend_invitation(db, admin, user.id, now=NOW)
        with database.write() as db:
            token = accounts.resend_invitation(db, admin, user.id, now=NOW)[1]
            accounts.accept_invitation(db, token, PASSWORD, now=NOW)
        text = " ".join(f"{entry.details}" for entry in entries(database))
        assert token not in text and hash_token(token) not in text and PASSWORD not in text
        assert "new@example.org" not in text
        with database.read() as db:
            assert audit.verify_chain(db).ok


class TestPendingAccounts:
    def test_cannot_log_in(self, database: Database, admin: User) -> None:
        user, _token = invite(database, admin)
        with database.write() as db:
            assert auth.authenticate(db, user.email, "") is None
            assert auth.authenticate(db, user.email, PASSWORD) is None
        last = entries(database)[-1]
        assert last.action == "account.login_failed" and last.details == {"reason": "pending"}

    def test_get_no_reset_link(self, database: Database, admin: User) -> None:
        user, _token = invite(database, admin)
        with database.write() as db:
            assert accounts.request_password_reset(db, user.email, now=NOW) is None
            assert db.scalar(select(func.count()).select_from(EmailToken)) == 1  # the invitation

    def test_password_checks_fail(self, database: Database, admin: User) -> None:
        user, _token = invite(database, admin)
        with pytest.raises(InvalidInputError, match="wrong_password"), database.write() as db:
            accounts.change_password(db, user, "", "another long passphrase")


class TestList:
    def test_states_without_session_data(
        self, database: Database, world: World, admin: User
    ) -> None:
        pending, _token = invite(database, admin, "pending@example.org")
        expired, _token = invite(database, admin, "expired@example.org")
        with database.write() as db:
            active = accounts.create_user(db, "active@example.org", PASSWORD, now=NOW)
            disabled = accounts.create_user(db, "off@example.org", PASSWORD, now=NOW)
            accounts.set_disabled(db, disabled.email, True, now=NOW)
            for token in db.scalars(select(EmailToken).where(EmailToken.user_id == expired.id)):
                token.expires_at = NOW - timedelta(seconds=1)
        world.assembly(active, title="Secret title")
        with database.read() as db:
            rows = {row.email: row for row in accounts.list_accounts(db, admin, now=NOW)}
        assert {email: row.state for email, row in rows.items()} == {
            "root@example.org": AccountState.ACTIVE,
            "pending@example.org": AccountState.PENDING,
            "expired@example.org": AccountState.PENDING,
            "active@example.org": AccountState.ACTIVE,
            "off@example.org": AccountState.DISABLED,
        }
        assert rows["root@example.org"].is_superadmin
        assert rows["pending@example.org"].invitation_valid
        assert not rows["expired@example.org"].invitation_valid
        assert rows["pending@example.org"].display_name == "Camille"
        assert "Secret title" not in repr(rows) and "assembl" not in repr(rows).lower()
        assert pending.id in {row.id for row in rows.values()}

    def test_most_recent_first(self, database: Database, admin: User) -> None:
        with database.write() as db:
            accounts.invite(db, admin, "later@example.org", now=NOW + timedelta(days=1))
            accounts.invite(db, admin, "earlier@example.org", now=NOW - timedelta(days=1))
        with database.read() as db:
            emails = [row.email for row in accounts.list_accounts(db, admin, now=NOW)]
        assert emails == ["later@example.org", "root@example.org", "earlier@example.org"]


class TestOnlyForSuperAdmins:
    def test_account_holders_get_not_found(self, database: Database, admin: User) -> None:
        user, _token = invite(database, admin)
        with database.write() as db:
            holder = accounts.create_user(db, "holder@example.org", PASSWORD, now=NOW)
        calls = [
            lambda db: accounts.list_accounts(db, holder),
            lambda db: accounts.invite(db, holder, "x@example.org"),
            lambda db: accounts.resend_invitation(db, holder, user.id),
            lambda db: accounts.delete_account_by_superadmin(
                db, holder, user.id, user.email, password=PASSWORD
            ),
        ]
        for call in calls:
            with pytest.raises(NotFoundError), database.write() as db:
                call(db)

    def test_disabled_superadmin_gets_not_found(self, database: Database, admin: User) -> None:
        with database.write() as db:
            accounts.set_disabled(db, admin.email, True, now=NOW)
        with pytest.raises(NotFoundError), database.read() as db:
            accounts.list_accounts(db, admin)

    def test_superadmin_cannot_create_sessions(
        self, database: Database, world: World, admin: User
    ) -> None:
        with (
            pytest.raises(InvalidStateError, match="superadmin_no_sessions"),
            database.write() as db,
        ):
            assemblies.create_assembly(db, world.keyring, admin, AssemblyInput(title="AG"))
        with database.read() as db:
            assert db.scalar(select(func.count()).select_from(Assembly)) == 0


class TestDeletion:
    def test_deletes_the_account_and_its_sessions(
        self, database: Database, world: World, admin: User
    ) -> None:
        with database.write() as db:
            target = accounts.create_user(db, "target@example.org", PASSWORD, now=NOW)
            outbox.queue_email(db, target.email, "s", "b", now=NOW)
        public_id = world.assembly(target, in_session=False, title="Secret title")
        closed = world.assembly(target, in_session=True)
        with database.write() as db:
            assemblies.close_assembly(db, target, closed)
        kept = world.assembly(world.user("other@example.org"), in_session=False)
        with database.write() as db:
            accounts.delete_account_by_superadmin(
                db, admin, target.id, " Target@Example.org ", password=PASSWORD, now=NOW
            )
        with database.read() as db:
            assert db.get(User, target.id) is None
            assert assemblies.find_assembly(db, public_id) is None
            assert assemblies.find_assembly(db, kept) is not None
            assert db.scalar(select(func.count()).select_from(OutboxEmail)) == 0
            entry = db.scalar(select(AuditEntry).order_by(AuditEntry.id.desc()))
            assert entry is not None and entry.action == "account.deleted"
            assert (entry.actor, entry.user_id) == (accounts.SUPERADMIN_ACTOR, target.id)
            assert entry.details == {"assemblies": 2, "by": admin.id}
            assert audit.verify_chain(db).ok

    def test_deletes_a_pending_account_and_its_invitation(
        self, database: Database, admin: User
    ) -> None:
        user, _token = invite(database, admin)
        with database.write() as db:
            outbox.queue_email(db, user.email, "invitation", "link", now=NOW)
            accounts.delete_account_by_superadmin(
                db, admin, user.id, user.email, password=PASSWORD, now=NOW
            )
        with database.read() as db:
            assert db.scalar(select(func.count()).select_from(EmailToken)) == 0
            assert db.scalar(select(func.count()).select_from(OutboxEmail)) == 0

    def test_needs_the_address_typed_again(self, database: Database, admin: User) -> None:
        user, _token = invite(database, admin)
        for typed in ["", "other@example.org"]:
            with (
                pytest.raises(InvalidInputError, match="superadmin_confirm_mismatch"),
                database.write() as db,
            ):
                accounts.delete_account_by_superadmin(
                    db, admin, user.id, typed, password=PASSWORD, now=NOW
                )
        assert reload(database, user.id) is not None

    def test_needs_the_superadmin_password(self, database: Database, admin: User) -> None:
        """Checked before anything else: a stolen super-admin login session deletes nothing,
        and tells nothing about the account."""
        user, _token = invite(database, admin)
        for typed, target in [("", user.id), ("wrong password", user.id), ("x", 999)]:
            with (
                pytest.raises(InvalidInputError, match="wrong_password"),
                database.write() as db,
            ):
                accounts.delete_account_by_superadmin(
                    db, admin, target, user.email, password=typed, now=NOW
                )
        assert reload(database, user.id) is not None

    def test_refused_while_in_session(self, database: Database, world: World, admin: User) -> None:
        with database.write() as db:
            target = accounts.create_user(db, "target@example.org", PASSWORD, now=NOW)
        world.assembly(target, in_session=True)
        with (
            pytest.raises(InvalidStateError, match="account_in_session"),
            database.write() as db,
        ):
            accounts.delete_account_by_superadmin(
                db, admin, target.id, target.email, password=PASSWORD, now=NOW
            )
        assert reload(database, target.id) is not None

    def test_never_a_superadmin(self, database: Database, admin: User) -> None:
        with database.write() as db:
            other = accounts.create_user_by_operator(
                db, "root2@example.org", PASSWORD, superadmin=True, now=NOW
            )
        for target in (admin, other):
            with (
                pytest.raises(InvalidStateError, match="superadmin_protected"),
                database.write() as db,
            ):
                accounts.delete_account_by_superadmin(
                    db, admin, target.id, target.email, password=PASSWORD, now=NOW
                )
            assert reload(database, target.id) is not None
        with pytest.raises(NotFoundError), database.write() as db:
            accounts.delete_account_by_superadmin(
                db, admin, 999, "x@example.org", password=PASSWORD, now=NOW
            )

    def test_superadmin_cannot_delete_itself_from_its_settings(
        self, database: Database, admin: User
    ) -> None:
        with (
            pytest.raises(InvalidStateError, match="superadmin_protected"),
            database.write() as db,
        ):
            accounts.delete_account(db, admin, PASSWORD, now=NOW)
        assert reload(database, admin.id) is not None


class TestOperator:
    def test_superadmin_creation(self, database: Database, admin: User) -> None:
        assert admin.is_superadmin and admin.email_verified_at == NOW
        entry = entries(database)[-1]
        assert (entry.action, entry.actor) == ("account.created", "operator")
        assert entry.details == {"verified": True, "superadmin": True}

    def test_deletes_any_account(self, database: Database, world: World, admin: User) -> None:
        with database.write() as db:
            target = accounts.create_user(db, "target@example.org", PASSWORD, now=NOW)
        world.assembly(target, in_session=False)
        with database.write() as db:
            assert accounts.delete_account_by_operator(db, " Target@example.org", now=NOW) == 1
            assert accounts.delete_account_by_operator(db, admin.email, now=NOW) == 0
        assert reload(database, target.id) is None and reload(database, admin.id) is None
        entry = entries(database)[-1]
        assert (entry.action, entry.actor, entry.details) == (
            "account.deleted",
            "operator",
            {"assemblies": 0},
        )
