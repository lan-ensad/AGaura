"""Email links, account settings, deletion and operator actions (FR-02 to FR-06, FR-81)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from agaura.db import Database
from agaura.models import (
    Assembly,
    AuditEntry,
    EmailToken,
    EmailTokenKind,
    OutboxEmail,
    User,
    UserSession,
)
from agaura.security import hash_token, verify_password
from agaura.services import accounts, assemblies, audit, auth, outbox
from agaura.services.errors import InvalidInputError, InvalidStateError, NotFoundError
from tests.conftest import World

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"


@pytest.fixture
def user(database: Database) -> User:
    with database.write() as db:
        return accounts.create_user(db, "camille@example.org", PASSWORD, now=NOW)


def log_actions(database: Database, user: User) -> list[str]:
    with database.read() as db:
        return [entry.action for entry in reversed(accounts.account_log(db, user))]


def count(database: Database, model: type) -> int:
    with database.read() as db:
        return db.scalar(select(func.count()).select_from(model)) or 0


def reload(database: Database, user: User) -> User:
    with database.read() as db:
        found = db.get(User, user.id)
        assert found is not None
        return found


class TestTokens:
    def test_only_the_hash_is_stored(self, database: Database, user: User) -> None:
        with database.write() as db:
            token = accounts.issue_token(db, user, EmailTokenKind.VERIFY, now=NOW)
        with database.read() as db:
            row = db.scalar(select(EmailToken))
        assert (
            row is not None and row.token_hash == hash_token(token) and token not in str(vars(row))
        )
        assert row.expires_at == NOW + timedelta(hours=24) and row.used_at is None

    @pytest.mark.parametrize(
        ("kind", "lifetime"),
        [
            (EmailTokenKind.VERIFY, timedelta(hours=24)),
            (EmailTokenKind.RESET, timedelta(hours=1)),
            (EmailTokenKind.CHANGE_EMAIL, timedelta(hours=24)),
            (EmailTokenKind.INVITE, timedelta(days=7)),
        ],
    )
    def test_lifetimes(
        self, database: Database, user: User, kind: EmailTokenKind, lifetime: timedelta
    ) -> None:
        with database.write() as db:
            token = accounts.issue_token(db, user, kind, now=NOW)
        with database.read() as db:
            accounts.check_token(db, kind, token, now=NOW + lifetime - timedelta(seconds=1))
            with pytest.raises(InvalidInputError, match="expired_link"):
                accounts.check_token(db, kind, token, now=NOW + lifetime)

    def test_check_does_not_use_up_the_link(self, database: Database, user: User) -> None:
        with database.write() as db:
            token = accounts.issue_token(db, user, EmailTokenKind.RESET, now=NOW)
        with database.read() as db:
            assert accounts.check_token(db, EmailTokenKind.RESET, token, now=NOW).id == user.id
            assert accounts.check_token(db, EmailTokenKind.RESET, token, now=NOW).id == user.id

    def test_kinds_do_not_mix(self, database: Database, user: User) -> None:
        with database.write() as db:
            token = accounts.issue_token(db, user, EmailTokenKind.RESET, now=NOW)
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.verify_email(db, token, now=NOW)

    @pytest.mark.parametrize("token", ["", "unknown", "x" * 500])
    def test_unknown_tokens(self, database: Database, token: str) -> None:
        with pytest.raises(InvalidInputError, match="invalid_link"), database.read() as db:
            accounts.check_token(db, EmailTokenKind.VERIFY, token, now=NOW)

    def test_disabled_accounts_links_stop_working(self, database: Database, user: User) -> None:
        with database.write() as db:
            token = accounts.issue_token(db, user, EmailTokenKind.RESET, now=NOW)
            accounts.set_disabled(db, user.email, True, now=NOW)
        with pytest.raises(InvalidInputError, match="invalid_link"), database.read() as db:
            accounts.check_token(db, EmailTokenKind.RESET, token, now=NOW)

    def test_pending_email_change(self, database: Database, user: User) -> None:
        with database.write() as db:
            accounts.request_email_change(db, user, PASSWORD, "new@example.org", now=NOW)
        with database.read() as db:
            assert accounts.pending_email_change(db, user, now=NOW) == "new@example.org"
            later = NOW + timedelta(days=2)
            assert accounts.pending_email_change(db, user, now=later) is None


class TestSignupAndVerification:
    def test_register_then_verify(self, database: Database) -> None:
        with database.write() as db:
            user, token = accounts.register(
                db, "New@Example.org", PASSWORD, display_name="  Camille  ", now=NOW
            )
        assert user.display_name == "Camille" and user.email_verified_at is None
        later = NOW + timedelta(hours=2)
        with database.write() as db:
            verified = accounts.verify_email(db, token, now=later)
        assert verified.email_verified_at == later
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.verify_email(db, token, now=later)
        assert log_actions(database, user) == ["account.created", "account.email_verified"]

    def test_expired_verification(self, database: Database) -> None:
        with database.write() as db:
            user, token = accounts.register(db, "new@example.org", PASSWORD, now=NOW)
        with pytest.raises(InvalidInputError, match="expired_link"), database.write() as db:
            accounts.verify_email(db, token, now=NOW + timedelta(hours=25))
        assert reload(database, user).email_verified_at is None

    def test_new_link_replaces_the_old_one(self, database: Database) -> None:
        with database.write() as db:
            user, first = accounts.register(db, "new@example.org", PASSWORD, now=NOW)
        with database.write() as db:
            found = accounts.request_verification(db, user, now=NOW)
        assert found is not None
        _account, second = found
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.verify_email(db, first, now=NOW)
        with database.write() as db:
            accounts.verify_email(db, second, now=NOW)
        with database.write() as db:
            assert accounts.request_verification(db, user, now=NOW) is None

    def test_password_limits(self, database: Database) -> None:
        with pytest.raises(InvalidInputError, match="password_too_long"), database.write() as db:
            accounts.create_user(db, "a@example.org", "x" * 257)
        with pytest.raises(InvalidInputError, match="too_long"), database.write() as db:
            accounts.create_user(db, "a@example.org", PASSWORD, display_name="x" * 101)


class TestPasswordReset:
    def test_unknown_or_disabled_address(self, database: Database, user: User) -> None:
        with database.write() as db:
            assert accounts.request_password_reset(db, "nobody@example.org", now=NOW) is None
            accounts.set_disabled(db, user.email, True, now=NOW)
            assert accounts.request_password_reset(db, user.email, now=NOW) is None
        with pytest.raises(InvalidInputError, match="invalid_email"), database.write() as db:
            accounts.request_password_reset(db, "nope", now=NOW)

    def test_reset(self, database: Database, user: User) -> None:
        with database.write() as db:
            auth.start_session(db, user, now=NOW)
            found = accounts.request_password_reset(db, " CAMILLE@example.org", now=NOW)
        assert found is not None
        _account, token = found
        with pytest.raises(InvalidInputError, match="password_too_short"), database.write() as db:
            accounts.reset_password(db, token, "short", now=NOW)
        with database.write() as db:
            accounts.reset_password(db, token, NEW_PASSWORD, now=NOW + timedelta(minutes=5))
        updated = reload(database, user)
        assert verify_password(updated.password_hash, NEW_PASSWORD)
        assert updated.email_verified_at is not None  # the link proved the address
        assert count(database, UserSession) == 0
        assert log_actions(database, user) == [
            "account.password_reset_requested",
            "account.password_reset",
            "account.logged_out_everywhere",
        ]
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.reset_password(db, token, NEW_PASSWORD, now=NOW)

    def test_expired_reset(self, database: Database, user: User) -> None:
        with database.write() as db:
            found = accounts.request_password_reset(db, user.email, now=NOW)
        assert found is not None
        with pytest.raises(InvalidInputError, match="expired_link"), database.write() as db:
            accounts.reset_password(db, found[1], NEW_PASSWORD, now=NOW + timedelta(hours=1))


class TestSettings:
    def test_display_name(self, database: Database, user: User) -> None:
        with database.write() as db:
            accounts.update_display_name(db, user, "  Camille   M. ", now=NOW)
            accounts.update_display_name(db, user, "Camille M.", now=NOW)  # unchanged
        assert reload(database, user).display_name == "Camille M."
        assert log_actions(database, user) == ["account.display_name_changed"]

    def test_change_password_renews_the_current_session(
        self, database: Database, user: User
    ) -> None:
        with database.write() as db:
            current = auth.start_session(db, user, now=NOW)
            other = auth.start_session(db, user, now=NOW)
            accounts.issue_token(db, user, EmailTokenKind.RESET, now=NOW)
        with pytest.raises(InvalidInputError, match="wrong_password"), database.write() as db:
            accounts.change_password(db, user, "wrong", NEW_PASSWORD)
        with database.write() as db:
            renewed = accounts.change_password(
                db, user, PASSWORD, NEW_PASSWORD, user_agent="Firefox", now=NOW
            )
        with database.read() as db:
            # Every previous token stops working, the current device's included (a stolen
            # copy of its cookie): the device goes on with the new one.
            assert auth.lookup(db, current, now=NOW) is None
            assert auth.lookup(db, other, now=NOW) is None
            found = auth.lookup(db, renewed, now=NOW)
            assert found is not None and found.user.id == user.id
            assert db.scalar(select(UserSession.user_agent)) == "Firefox"
            assert db.scalar(select(EmailToken)) is None  # reset links are void
        assert renewed not in (current, other)
        assert count(database, UserSession) == 1
        assert verify_password(reload(database, user).password_hash, NEW_PASSWORD)
        assert log_actions(database, user) == ["account.password_changed"]

    def test_email_change(self, database: Database, user: User) -> None:
        with database.write() as db:
            accounts.issue_token(db, user, EmailTokenKind.VERIFY, now=NOW)
            address, token = accounts.request_email_change(
                db, user, PASSWORD, " New@Example.org", now=NOW
            )
        assert address == "new@example.org"
        assert reload(database, user).email == "camille@example.org"
        later = NOW + timedelta(hours=1)
        with database.write() as db:
            changed, previous = accounts.change_email(db, token, now=later)
        assert (changed.email, previous, changed.email_verified_at) == (
            "new@example.org",
            "camille@example.org",
            later,
        )
        with database.read() as db:
            remaining = list(db.scalars(select(EmailToken.kind)))
        assert remaining == [EmailTokenKind.CHANGE_EMAIL]  # the verify link is void
        assert log_actions(database, user) == [
            "account.email_change_requested",
            "account.email_changed",
        ]

    @pytest.mark.parametrize(
        ("password", "address", "code"),
        [
            ("wrong", "new@example.org", "wrong_password"),
            (PASSWORD, "CAMILLE@example.org", "same_email"),
            (PASSWORD, "nope", "invalid_email"),
        ],
    )
    def test_email_change_refused(
        self, database: Database, user: User, password: str, address: str, code: str
    ) -> None:
        with database.write() as db:
            accounts.create_user(db, "taken@example.org", PASSWORD)
        with pytest.raises(InvalidInputError, match=code), database.write() as db:
            accounts.request_email_change(db, user, password, address, now=NOW)
        assert count(database, OutboxEmail) == 0

    def test_email_change_to_a_taken_address(self, database: Database, user: User) -> None:
        """Same outcome for the account as a free address (pending change, notice, log): only
        the email to the new address differs, so the form reveals nothing."""
        with database.write() as db:
            accounts.create_user(db, "taken@example.org", PASSWORD, display_name="Dominique")
            address, token = accounts.request_email_change(
                db, user, PASSWORD, "Taken@example.org", now=NOW
            )
        assert (address, token) == ("taken@example.org", None)
        with database.read() as db:
            assert accounts.pending_email_change(db, user, now=NOW) == "taken@example.org"
            notice, told = db.scalars(select(OutboxEmail).order_by(OutboxEmail.id))
        assert notice.to_address == "camille@example.org"
        assert notice.subject == "Demande de changement de votre adresse e-mail"
        assert told.to_address == "taken@example.org"
        assert told.subject == "Votre adresse e-mail a été indiquée pour un autre compte AGaura"
        assert told.body_text.startswith("Bonjour Dominique,")
        assert "c'est déjà l'adresse de votre compte AGaura" in told.body_text
        assert "http" not in told.body_text  # no link to confirm anything
        assert log_actions(database, user) == ["account.email_change_requested"]
        assert reload(database, user).email == "camille@example.org"

    def test_email_change_tells_the_current_address(self, database: Database, user: User) -> None:
        with database.write() as db:
            accounts.request_email_change(db, user, PASSWORD, "new@example.org", now=NOW)
        with database.read() as db:
            (notice,) = db.scalars(select(OutboxEmail))
        assert notice.to_address == "camille@example.org" and notice.created_at == NOW
        assert notice.subject == "Demande de changement de votre adresse e-mail"
        assert "la nouvelle adresse serait new@example.org" in notice.body_text
        assert "«\u00a0Mot de passe oublié\u00a0?\u00a0»" in notice.body_text

    def test_email_taken_before_confirmation(self, database: Database, user: User) -> None:
        with database.write() as db:
            _address, token = accounts.request_email_change(
                db, user, PASSWORD, "new@example.org", now=NOW
            )
            accounts.create_user(db, "new@example.org", PASSWORD)
        with pytest.raises(InvalidInputError, match="email_unavailable"), database.write() as db:
            accounts.change_email(db, token, now=NOW)

    def test_account_log_holds_only_account_events(
        self, database: Database, world: World, user: User
    ) -> None:
        with database.write() as db:
            auth.authenticate(db, user.email, "wrong", now=NOW)
            auth.authenticate(db, user.email, PASSWORD, now=NOW)
        world.assembly(user, in_session=False)
        with database.read() as db:
            log = accounts.account_log(db, user)
            assert [entry.action for entry in log] == ["account.login", "account.login_failed"]
            assert accounts.account_log(db, user, limit=1)[0].action == "account.login"


class TestTakeoverLinks:
    """Someone who once learned the password asks for an email change to their own mailbox:
    changing or resetting the password, or logging out everywhere, voids that link."""

    def request_change(self, database: Database, user: User) -> str:
        with database.write() as db:
            _address, token = accounts.request_email_change(
                db, user, PASSWORD, "intruder@example.org", now=NOW
            )
        return token

    def assert_change_void(self, database: Database, user: User, token: str) -> None:
        with database.read() as db:
            assert accounts.pending_email_change(db, user, now=NOW) is None
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.change_email(db, token, now=NOW)
        assert reload(database, user).email == "camille@example.org"

    def test_password_change(self, database: Database, user: User) -> None:
        token = self.request_change(database, user)
        with database.write() as db:
            verify = accounts.issue_token(db, user, EmailTokenKind.VERIFY, now=NOW)
            accounts.change_password(db, user, PASSWORD, NEW_PASSWORD, now=NOW)
        self.assert_change_void(database, user, token)
        with database.write() as db:  # a verification link only confirms the current address
            accounts.verify_email(db, verify, now=NOW)

    def test_password_reset(self, database: Database, user: User) -> None:
        token = self.request_change(database, user)
        with database.write() as db:
            found = accounts.request_password_reset(db, user.email, now=NOW)
        assert found is not None
        with database.write() as db:
            accounts.reset_password(db, found[1], NEW_PASSWORD, now=NOW)
        self.assert_change_void(database, user, token)
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.reset_password(db, found[1], "yet another passphrase", now=NOW)

    def test_log_out_everywhere(self, database: Database, user: User) -> None:
        token = self.request_change(database, user)
        with database.write() as db:
            reset = accounts.issue_token(db, user, EmailTokenKind.RESET, now=NOW)
            verify = accounts.issue_token(db, user, EmailTokenKind.VERIFY, now=NOW)
            auth.start_session(db, user, now=NOW)
            auth.end_all_sessions(db, user, now=NOW)
        assert count(database, UserSession) == 0
        self.assert_change_void(database, user, token)
        with pytest.raises(InvalidInputError, match="invalid_link"), database.write() as db:
            accounts.reset_password(db, reset, NEW_PASSWORD, now=NOW)
        with database.write() as db:
            accounts.verify_email(db, verify, now=NOW)

    def test_other_accounts_keep_their_links(self, database: Database, user: User) -> None:
        with database.write() as db:
            other = accounts.create_user(db, "dominique@example.org", PASSWORD, now=NOW)
            _address, token = accounts.request_email_change(
                db, other, PASSWORD, "dominique@example.net", now=NOW
            )
            auth.end_all_sessions(db, user, now=NOW)
        with database.write() as db:
            changed, _previous = accounts.change_email(db, token, now=NOW)
        assert changed.email == "dominique@example.net"


class TestDeletion:
    def test_deletes_the_account_and_its_sessions(
        self, database: Database, world: World, user: User
    ) -> None:
        public_id = world.assembly(user, in_session=False, title="AG 2026")
        world.assembly(world.user("other@example.org"), in_session=False)
        with database.write() as db:
            auth.start_session(db, user, now=NOW)
            accounts.request_email_change(db, user, PASSWORD, "new@example.org", now=NOW)
            outbox.queue_email(db, "new@example.org", "s", "b", now=NOW)
            outbox.queue_email(db, user.email, "s", "b", now=NOW)
            outbox.queue_email(db, "other@example.org", "s", "b", now=NOW)
        with pytest.raises(InvalidInputError, match="wrong_password"), database.write() as db:
            accounts.delete_account(db, user, "wrong", now=NOW)
        with database.write() as db:
            accounts.delete_account(db, user, PASSWORD, now=NOW)
        with database.read() as db:
            assert db.get(User, user.id) is None
            assert assemblies.find_assembly(db, public_id) is None
            assert db.scalar(select(func.count()).select_from(Assembly)) == 1
            assert db.scalar(select(func.count()).select_from(EmailToken)) == 0
            assert list(db.scalars(select(OutboxEmail.to_address))) == ["other@example.org"]
            entry = db.scalar(select(AuditEntry).order_by(AuditEntry.id.desc()))
            assert entry is not None and entry.action == "account.deleted"
            assert entry.details == {"assemblies": 1} and entry.user_id == user.id
            assert audit.verify_chain(db).ok
        assert count(database, UserSession) == 0

    def test_refused_while_in_session(self, database: Database, world: World, user: User) -> None:
        world.assembly(user, in_session=True)
        with pytest.raises(InvalidStateError, match="account_in_session"), database.write() as db:
            accounts.delete_account(db, user, PASSWORD, now=NOW)
        assert reload(database, user).id == user.id

    def test_already_deleted(self, database: Database, user: User) -> None:
        with database.write() as db:
            accounts.delete_account(db, user, PASSWORD, now=NOW)
        with pytest.raises(NotFoundError), database.write() as db:
            accounts.update_display_name(db, user, "x")


class TestOperator:
    def test_create_by_operator(self, database: Database) -> None:
        with database.write() as db:
            user = accounts.create_user_by_operator(
                db, "ops@example.org", PASSWORD, verified=True, now=NOW
            )
        assert user.email_verified_at == NOW
        with database.read() as db:
            entry = db.scalar(select(AuditEntry))
        assert entry is not None and (entry.actor, entry.action) == ("operator", "account.created")

    def test_disable_twice_keeps_the_first_date(self, database: Database, user: User) -> None:
        with database.write() as db:
            accounts.set_disabled(db, user.email, True, now=NOW)
            accounts.set_disabled(db, user.email, True, now=NOW + timedelta(days=1))
        assert reload(database, user).disabled_at == NOW

    def test_quotas(self, database: Database, user: User) -> None:
        with database.write() as db:
            limits = accounts.set_quotas(db, user.email, {"open_assemblies": 2}, now=NOW)
        assert limits == accounts.Quotas(open_assemblies=2)
        with database.write() as db:
            limits = accounts.set_quotas(db, user.email, {"codes_per_assembly": 10}, now=NOW)
        assert limits == accounts.Quotas(open_assemblies=2, codes_per_assembly=10)
        with database.write() as db:
            assert accounts.set_quotas(db, user.email, {}, reset=True) == accounts.Quotas()
        for changes in ({"unknown": 1}, {"open_assemblies": -1}):
            with pytest.raises(InvalidInputError, match="invalid_quota"), database.write() as db:
                accounts.set_quotas(db, user.email, changes)

    def test_unknown_account(self, database: Database) -> None:
        with pytest.raises(NotFoundError), database.read() as db:
            accounts.find_user(db, "nobody@example.org")
