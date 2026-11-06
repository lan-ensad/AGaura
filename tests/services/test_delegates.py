"""Delegate links (FR-92): validation, owner isolation, lifetime, audit attribution."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from agaura.models import AuditEntry, Delegate, User, utcnow
from agaura.security import hash_token
from agaura.services import accounts, assemblies, audit, delegates, meeting
from agaura.services.delegates import MAX_DELEGATES, Right
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)
from tests.conftest import World


def create(
    world: World, owner: User, public_id: str, name: str = "Claire", rights: list[str] | None = None
) -> tuple[int, str]:
    with world.database.write() as db:
        delegate, token = delegates.create_delegate(
            db, world.keyring, owner, public_id, name, rights or [Right.TIMER.value]
        )
        return delegate.id, token


def found(world: World, public_id: str, token: str | None) -> Delegate | None:
    with world.database.read() as db:
        return delegates.authenticate(db, public_id, token)


def log(world: World, action: str) -> list[AuditEntry]:
    with world.database.read() as db:
        return list(db.scalars(select(AuditEntry).where(AuditEntry.action == action)))


def test_a_link_opens_its_session_only(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    other = world.assembly(owner)
    delegate_id, token = create(world, owner, public_id, rights=["polls", "notes", "agenda"])

    delegate = found(world, public_id, token)
    assert delegate is not None and delegate.id == delegate_id
    assert delegate.rights == ["agenda", "notes", "polls"]  # display order
    assert found(world, public_id.lower(), token) is not None  # any spelling of the ID
    assert found(world, other, token) is None
    assert found(world, public_id, token + "x") is None
    assert found(world, public_id, "") is None
    assert found(world, public_id, None) is None


def test_only_a_digest_and_an_encrypted_copy_are_stored(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = create(world, owner, public_id)
    with world.database.read() as db:
        delegate = db.get(Delegate, delegate_id)
        assert delegate is not None
        assert delegate.token_hash == hash_token(token)
        assert delegate.token_enc and token not in delegate.token_enc
        assert delegates.link_token(world.keyring, delegate) == token


@pytest.mark.parametrize(
    ("name", "rights", "code"),
    [
        ("", ["timer"], "delegates_name_required"),
        ("   ", ["timer"], "delegates_name_required"),
        ("x" * 101, ["timer"], "delegates_name_too_long"),
        ("Claire", [], "delegates_rights_required"),
        ("Claire", ["timer", "settings"], "delegates_rights_required"),
    ],
)
def test_invalid_links_are_refused(
    world: World, owner: User, name: str, rights: list[str], code: str
) -> None:
    public_id = world.assembly(owner)
    with pytest.raises(InvalidInputError) as raised, world.database.write() as db:
        delegates.create_delegate(db, world.keyring, owner, public_id, name, rights)
    assert raised.value.code == code


def test_the_name_is_trimmed(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    _, token = create(world, owner, public_id, name="  Claire   Martin ")
    delegate = found(world, public_id, token)
    assert delegate is not None and delegate.name == "Claire Martin"


def test_another_account_reaches_no_link(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, _ = create(world, owner, public_id)
    intruder = world.user("intruder@example.org")
    with pytest.raises(NotFoundError), world.database.write() as db:
        delegates.create_delegate(db, world.keyring, intruder, public_id, "X", ["timer"])
    with pytest.raises(NotFoundError), world.database.write() as db:
        delegates.revoke_delegate(db, intruder, public_id, delegate_id)
    with pytest.raises(NotFoundError), world.database.write() as db:
        delegates.update_delegate(db, intruder, public_id, delegate_id, "X", ["polls"])
    with pytest.raises(NotFoundError), world.database.read() as db:
        delegates.list_delegates(db, intruder, public_id)


def test_a_link_of_another_session_is_not_found(world: World, owner: User) -> None:
    first = world.assembly(owner)
    second = world.assembly(owner)
    delegate_id, _ = create(world, owner, first)
    with pytest.raises(NotFoundError), world.database.write() as db:
        delegates.revoke_delegate(db, owner, second, delegate_id)


def test_changing_the_rights_keeps_the_link(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = create(world, owner, public_id)
    with world.database.write() as db:
        delegates.update_delegate(db, owner, public_id, delegate_id, "Paul", ["headcount", "polls"])
    delegate = found(world, public_id, token)
    assert delegate is not None
    assert (delegate.name, delegate.rights) == ("Paul", ["polls", "headcount"])
    with world.database.write() as db:  # unchanged: no entry
        delegates.update_delegate(db, owner, public_id, delegate_id, "Paul", ["polls", "headcount"])
    assert [entry.details for entry in log(world, "delegate.updated")] == [
        {"delegate": delegate_id, "rights": ["polls", "headcount"]}
    ]


def test_revoking_ends_the_link_and_forgets_its_secret(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = create(world, owner, public_id)
    with world.database.write() as db:
        delegates.revoke_delegate(db, owner, public_id, delegate_id)
    assert found(world, public_id, token) is None
    with world.database.read() as db:
        delegate = db.get(Delegate, delegate_id)
        assert delegate is not None
        assert delegate.ended_at is not None
        assert delegate.token_hash is None and delegate.token_enc is None
        assert delegates.list_delegates(db, owner, public_id) == []
        assert delegates.names(db, delegate.assembly) == {delegate_id: "Claire"}  # for the log
        with pytest.raises(NotFoundError):
            delegates.link_token(world.keyring, delegate)
    with pytest.raises(NotFoundError), world.database.write() as db:  # already ended
        delegates.revoke_delegate(db, owner, public_id, delegate_id)


def test_closing_the_session_ends_every_link(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    _, first = create(world, owner, public_id)
    _, second = create(world, owner, public_id, name="Paul")
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    assert found(world, public_id, first) is None
    assert found(world, public_id, second) is None
    with world.database.read() as db:
        rows = db.scalars(select(Delegate)).all()
        assert all(row.ended_at and row.token_hash is None for row in rows)
    with pytest.raises(InvalidStateError), world.database.write() as db:
        delegates.create_delegate(db, world.keyring, owner, public_id, "Late", ["timer"])


def test_links_work_in_preparation(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    _, token = create(world, owner, public_id)
    assert found(world, public_id, token) is not None


def test_a_disabled_account_ends_its_links(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    _, token = create(world, owner, public_id)
    with world.database.write() as db:
        accounts.set_disabled(db, owner.email, True)
    assert found(world, public_id, token) is None


def test_at_most_twenty_links_in_use(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    ids = [create(world, owner, public_id, name=f"P{number}")[0] for number in range(MAX_DELEGATES)]
    with pytest.raises(QuotaExceededError), world.database.write() as db:
        delegates.create_delegate(db, world.keyring, owner, public_id, "One more", ["timer"])
    with world.database.write() as db:
        delegates.revoke_delegate(db, owner, public_id, ids[0])
    create(world, owner, public_id, name="Replacement")


def test_audit_names_the_link_by_id_only(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = create(world, owner, public_id, name="Claire Secret")
    entries = log(world, "delegate.created")
    assert [entry.details for entry in entries] == [{"delegate": delegate_id, "rights": ["timer"]}]
    for entry in entries:
        assert "Claire" not in str(entry.details) and token not in str(entry.details)


def test_actions_through_a_link_are_attributed_to_it(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, _ = create(world, owner, public_id, rights=["agenda", "notes"])
    with world.database.write() as db:
        audit.act_as_delegate(db, delegate_id)
        meeting.set_agenda(db, owner, public_id, ["Rapport moral"])
    with world.database.write() as db:  # the next transaction is the owner's again
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
    first, second = log(world, "meeting.agenda_set")
    assert first.actor == audit.Actor.DELEGATE
    assert first.user_id == owner.id
    assert first.details["delegate"] == delegate_id
    assert second.actor == audit.Actor.USER
    assert "delegate" not in second.details
    with world.database.read() as db:
        assert audit.verify_chain(db).ok


def test_last_use_is_written_at_most_every_five_minutes(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = create(world, owner, public_id)
    delegate = found(world, public_id, token)
    assert delegate is not None and delegates.needs_touch(delegate)
    now = utcnow()
    with world.database.write() as db:
        delegates.touch(db, delegate_id, now=now)
    delegate = found(world, public_id, token)
    assert delegate is not None
    assert not delegates.needs_touch(delegate, now + dt.timedelta(minutes=4))
    assert delegates.needs_touch(delegate, now + dt.timedelta(minutes=5))
