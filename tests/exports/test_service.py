"""Data gathering behind the exports and the public page (agaura.services.exports)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agaura.models import AuditEntry, User
from agaura.services import assemblies, exports
from agaura.services.errors import NotFoundError
from tests.conftest import World
from tests.exports.conftest import Built


def test_session_record(world: World, owner: User, built: Built) -> None:
    with world.database.read() as db:
        session = exports.session_record(db, owner, built.public_id)
    assert (session.attendees, session.quorum, session.quorum_reached) == (6, 5, True)
    ids = [record.poll.id for record in session.polls]
    expected = [
        built.polls[key]
        for key in (
            "resolution",
            "approval",
            "majority_judgment",
            "cancelled_closed",
            "cancelled_open",
            "unpublished",
        )
    ]
    assert ids == expected  # session order, the draft left out
    assert [record.number for record in session.polls] == [1, 2, 3, 4, 5, 6]
    assert [record.turnout for record in session.polls] == [6, 6, 6, 3, 3, 3]
    assert all(record.ballots is None for record in session.polls)
    assert len(session.finished) == 6


def test_quorum_unknown(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    with world.database.read() as db:
        session = exports.session_record(db, owner, public_id)
    assert session.quorum is None and session.quorum_reached is None and session.polls == []


def test_poll_record_is_owner_scoped(world: World, owner: User, built: Built) -> None:
    intruder = world.user("intruder@example.org")
    other = world.assembly(owner)
    with world.database.read() as db:
        with pytest.raises(NotFoundError):
            exports.poll_record(db, intruder, built.public_id, built.polls["resolution"])
        with pytest.raises(NotFoundError):
            exports.poll_record(db, owner, other, built.polls["resolution"])
        with pytest.raises(NotFoundError):
            exports.session_record(db, intruder, built.public_id)
        session, item = exports.poll_record(db, owner, built.public_id, built.polls["approval"])
    assert session.assembly.public_id == built.public_id
    assert item.number == 2 and item.ballots is not None and len(item.ballots) == 6


def test_poll_numbers(world: World, owner: User, built: Built) -> None:
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, built.public_id)
        numbers = exports.poll_numbers(db, assembly)
    assert numbers[built.polls["resolution"]] == 1
    assert numbers[built.polls["unpublished"]] == 6
    assert built.polls["draft"] not in numbers


def test_record_export_keeps_no_content(world: World, owner: User, built: Built) -> None:
    with world.database.read() as db:
        _, item = exports.poll_record(db, owner, built.public_id, built.polls["resolution"])
    with world.database.write() as db:
        exports.record_export(db, owner, built.public_id, "export.ballots_csv", poll=item.poll)
        exports.record_export(db, owner, built.public_id, "export.minutes_pdf")
    with world.database.read() as db:
        entries = db.scalars(
            select(AuditEntry).where(AuditEntry.action.like("export.%")).order_by(AuditEntry.id)
        ).all()
    assert [(entry.action, entry.details) for entry in entries] == [
        (
            "export.ballots_csv",
            {"poll_id": built.polls["resolution"], "title": "Approbation des comptes"},
        ),
        ("export.minutes_pdf", {}),
    ]


def test_public_results(world: World, owner: User, built: Built) -> None:
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, built.public_id)
        shown = [poll.id for poll in exports.public_results(db, assembly)]
    assert shown == [
        built.polls[key]
        for key in (
            "resolution",
            "approval",
            "majority_judgment",
            "cancelled_closed",
            "cancelled_open",
        )
    ]
