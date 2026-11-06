import pytest

from agaura.db import Database
from agaura.models import User
from agaura.security import verify_password
from agaura.services import accounts
from agaura.services.errors import InvalidInputError


def test_create_user(database: Database) -> None:
    with database.write() as db:
        user = accounts.create_user(
            db, "  Alice@Example.ORG ", "correct horse battery", display_name=" Alice "
        )
    assert (user.email, user.display_name, user.email_verified_at) == (
        "alice@example.org",
        "Alice",
        None,
    )
    assert verify_password(user.password_hash, "correct horse battery")


@pytest.mark.parametrize(
    ("email", "password", "code"),
    [
        ("not-an-email", "correct horse battery", "invalid_email"),
        ("a@localhost", "correct horse battery", "invalid_email"),
        ("a b@example.org", "correct horse battery", "invalid_email"),
        ("a@example.org", "too short", "password_too_short"),
    ],
)
def test_invalid_accounts(database: Database, email: str, password: str, code: str) -> None:
    with pytest.raises(InvalidInputError, match=code), database.write() as db:
        accounts.create_user(db, email, password)


@pytest.mark.parametrize(
    "email",
    [
        "x\n@y.org",  # a line break in the To header stopped every delivery
        "x\r@y.org",
        "a\t@x.org",
        "a@x.org\x00",
        "a@x.org,b@y.org",  # several recipients
        "a@x.org;b@y.org",
        "<a@x.org>",
        "Name <a@x.org>",
        '"a b"@x.org',  # quoted local part
        '"a"@x.org',
        "a(comment)@x.org",
        "a\\b@x.org",
        "a@[192.0.2.1]",
        "a@b@x.org",
        "a:b@x.org",
        "@x.org",
        "a@",
        "a@.org",
        "a@x..org",
        "a@x.org.",
        "a\u00a0b@x.org",  # no-break space
        "a\u200bb@x.org",  # zero-width space (category Cf)
        "a\u202eb@x.org",  # right-to-left override
        "a\u2028@x.org",  # line separator
        "a\x85b@x.org",  # next line (C1 control)
        "a" * 250 + "@x.org",
    ],
)
def test_normalize_email_refuses(email: str) -> None:
    with pytest.raises(InvalidInputError, match="invalid_email"):
        accounts.normalize_email(email)


@pytest.mark.parametrize(
    ("email", "normalized"),
    [
        ("  Alice@Example.ORG\n", "alice@example.org"),
        ("first.last+tag@mail.example.co.uk", "first.last+tag@mail.example.co.uk"),
        ("o'brien@example.org", "o'brien@example.org"),
        ("a_b-c@sub-domain.example.org", "a_b-c@sub-domain.example.org"),
        ("Élise@exemple.fr", "élise@exemple.fr"),
        ("a@xn--bcher-kva.example", "a@xn--bcher-kva.example"),
    ],
)
def test_normalize_email_accepts(email: str, normalized: str) -> None:
    assert accounts.normalize_email(email) == normalized


def test_email_is_unique(database: Database) -> None:
    with database.write() as db:
        accounts.create_user(db, "a@example.org", "correct horse battery", verified=True)
    with pytest.raises(InvalidInputError, match="email_taken"), database.write() as db:
        accounts.create_user(db, "A@example.org", "correct horse battery")


def test_quota_overrides() -> None:
    user = User(quota_overrides={"open_assemblies": 3, "unknown": 9, "codes_per_assembly": True})
    assert accounts.quotas(user) == accounts.Quotas(open_assemblies=3)
    assert accounts.quotas(User(quota_overrides=None)) == accounts.Quotas()
