"""Complete JSON export (FR-61, FR-63): documented shape, digests recomputable from the file."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import pytest

from agaura.exports import json_export
from agaura.models import Ballot, Poll, User
from agaura.services import access, assemblies, exports, polls, results
from agaura.services.errors import InvalidStateError
from tests.conftest import World
from tests.exports.conftest import Built, build_session

NOW = dt.datetime(2026, 6, 20, 18, 30, tzinfo=dt.UTC)


def session_document(world: World, owner: User, public_id: str) -> dict[str, Any]:
    with world.database.read() as db:
        session = exports.session_record(db, owner, public_id, with_ballots=True)
        document = json_export.document(session, session.finished, now=NOW)
    return json.loads(json_export.dumps(document))


def digest(entry: dict[str, Any]) -> str:
    """The documented canonical form, written out independently of the application."""
    canonical = json.dumps(
        {"poll": entry["definition"], "ballots": entry["ballots"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_document_shape(world: World, owner: User, built: Built) -> None:
    document = session_document(world, owner, built.public_id)
    assert list(document) == ["format", "version", "generated_at", "session", "polls"]
    assert (document["format"], document["version"]) == ("agaura-export", 1)
    assert document["generated_at"] == "2026-06-20T18:30:00+00:00"
    assert document["session"] == {
        "id": built.public_id,
        "title": "Assemblée générale 2026",
        "date": "2026-06-20",
        "place": "Salle des fêtes",
        "status": "closed",
        "access_mode": "password",
        "registered_members": 20,
        "quorum_rule": "1/4",
        "quorum_required": 5,
        "attendees": 6,
        "headcount": None,
        "opened_at": document["session"]["opened_at"],
        "closed_at": document["session"]["closed_at"],
        "tribune": [
            {"name": "Marie Dupont", "role": "Présidente de séance"},
            {"name": "Paul Martin", "role": "Secrétaire"},
            {"name": "Léa Bernard", "role": None},
        ],
        "agenda": [
            {"title": "Approbation du procès-verbal"},
            {"title": "Rapport moral"},
            {"title": "Rapport financier"},
        ],
        "attendance": document["session"]["attendance"],
        # The internal note is left out: this file may be shared.
        "notes": [
            {
                "at": "2026-06-20T11:59:30+00:00",
                "updated_at": None,
                "agenda_item": 1,
                "text": "Le trésorier présente les comptes.\nAucune question.",
            },
            {
                "at": "2026-06-20T12:04:30+00:00",
                "updated_at": None,
                "agenda_item": None,
                "text": "Motion déposée en séance par Léa Bernard.",
            },
        ],
    }
    assert "relancer la mairie" not in json.dumps(document, ensure_ascii=False)
    # Attendance readings: opening, each poll pushed, each poll closed (not the one cancelled
    # while open), closing; aggregated counts only.
    attendance = document["session"]["attendance"]
    assert all(
        list(entry) == ["at", "event", "poll_number", "headcount", "attendees"]
        for entry in attendance
    )
    events = sorted((entry["event"], entry["poll_number"]) for entry in attendance)
    assert events == sorted(
        [("session_opened", None), ("session_closed", None)]
        + [("poll_opened", number) for number in range(1, 7)]
        + [("poll_closed", number) for number in (1, 2, 3, 4, 6)]
    )
    assert [entry["at"] for entry in attendance] == sorted(entry["at"] for entry in attendance)
    closing = attendance[-1]
    assert closing["event"] == "session_closed"
    assert (closing["attendees"], closing["headcount"]) == (6, None)
    assert closing["at"] == document["session"]["closed_at"]
    # Every finished poll in session order; the draft is left out.
    titles = [entry["definition"]["title"] for entry in document["polls"]]
    assert titles == [
        "Approbation des comptes",
        "Élection du conseil (2 sièges)",
        "Projets de quartier",
        "Cotisation 2027",
        "Modification des statuts",
        "Motion non publiée",
    ]
    assert [entry["number"] for entry in document["polls"]] == [1, 2, 3, 4, 5, 6]
    first = document["polls"][0]
    assert list(first) == [
        "number",
        "id",
        "status",
        "opened_at",
        "closed_at",
        "cancel_reason",
        "turnout",
        "definition",
        "ballots",
        "result",
        "agenda_item",
    ]
    assert [entry["agenda_item"] for entry in document["polls"]] == [1, 2, 2, 3, None, None]
    assert first["opened_at"] == "2026-06-20T12:00:00+00:00"
    assert first["definition"] == {
        "title": "Approbation des comptes",
        "description": None,
        "method": "single_choice",
        "params": {"adopt_option": 1},
        "blank_label": "Abstention",
        "options": [
            {"id": 1, "label": "Pour", "description": None},
            {"id": 2, "label": "Contre", "description": None},
        ],
    }
    assert list(first["result"]) == ["ballots_sha256", "computed_at", "published_at", "data"]
    assert first["result"]["data"]["outcome"]["adopted"] is True


def test_empty_tribune_and_agenda(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    session = session_document(world, owner, public_id)["session"]
    assert (session["tribune"], session["agenda"]) == ([], [])
    (opening,) = session["attendance"]
    assert opening == {
        "at": session["opened_at"],
        "event": "session_opened",
        "poll_number": None,
        "headcount": None,
        "attendees": 0,
    }


def test_attendance_with_headcounts(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 40)  # in preparation: no connection yet
        assemblies.open_assembly(db, owner, public_id)
    world.voters(owner, public_id, 3)
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 42)
    poll_id = world.poll(owner, public_id)
    world.voters(owner, public_id, 1)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    readings = [
        (entry["event"], entry["poll_number"], entry["headcount"], entry["attendees"])
        for entry in session_document(world, owner, public_id)["session"]["attendance"]
    ]
    assert readings == [
        ("headcount_set", None, 40, 0),
        ("session_opened", None, 40, 0),
        ("headcount_set", None, 42, 3),
        ("poll_opened", 1, 42, 3),
        ("poll_closed", 1, 42, 4),
    ]


def test_ballots_are_anonymous_and_sorted(world: World, owner: User, built: Built) -> None:
    document = session_document(world, owner, built.public_id)
    for entry in document["polls"]:
        ids = [ballot["id"] for ballot in entry["ballots"]]
        assert ids == sorted(ids) and len(ids) == entry["turnout"]
        for ballot in entry["ballots"]:
            assert set(ballot) == {"id", "is_blank", "content"}
            assert ballot["is_blank"] == (ballot["content"] is None)
    text = json.dumps(document, ensure_ascii=False)
    for forbidden in (
        "credential",
        "token",
        "code_hmac",
        "owner",
        world.password(owner, built.public_id),
    ):
        assert forbidden not in text


def test_digests_are_recomputable_from_the_file(world: World, owner: User, built: Built) -> None:
    document = session_document(world, owner, built.public_id)
    with world.database.read() as db:
        for entry in document["polls"]:
            poll = db.get(Poll, entry["id"])
            assert poll is not None
            ballots = db.query(Ballot).filter(Ballot.poll_id == poll.id).all()
            assert digest(entry) == results.ballots_sha256(poll, ballots)
            if poll.result is None:
                assert entry["status"] == "cancelled" and entry["result"] is None
            else:
                assert entry["result"]["ballots_sha256"] == digest(entry)
                assert entry["result"]["data"] == poll.result.data


def test_cancelled_polls_keep_their_reason(world: World, owner: User, built: Built) -> None:
    entries = {
        entry["id"]: entry for entry in session_document(world, owner, built.public_id)["polls"]
    }
    closed_then_cancelled = entries[built.polls["cancelled_closed"]]
    assert closed_then_cancelled["cancel_reason"] == "Vote à refaire"
    assert closed_then_cancelled["result"] is not None  # tallied at closing, never announced
    cancelled_while_open = entries[built.polls["cancelled_open"]]
    assert cancelled_while_open["cancel_reason"] == "Erreur dans l'énoncé"
    assert cancelled_while_open["result"] is None and len(cancelled_while_open["ballots"]) == 3


def test_open_polls_and_drafts_are_never_exported(world: World, owner: User) -> None:
    built = build_session(world, owner, close=False)
    open_id = world.poll(owner, built.public_id)
    world.vote(built.public_id, world.voters(owner, built.public_id, 1)[0], open_id, None)
    document = session_document(world, owner, built.public_id)
    ids = {entry["id"] for entry in document["polls"]}
    assert open_id not in ids and built.polls["draft"] not in ids
    with world.database.read() as db:
        for poll_id in (open_id, built.polls["draft"]):
            with pytest.raises(InvalidStateError) as raised:
                exports.poll_record(db, owner, built.public_id, poll_id)
            assert raised.value.code == "export_poll_not_finished"
        session = exports.session_record(db, owner, built.public_id, with_ballots=True)
    open_record = next(record for record in session.polls if record.poll.id == open_id)
    assert open_record.ballots is None and open_record.turnout == 1  # counted, never handed out


def test_poll_document(world: World, owner: User, built: Built) -> None:
    with world.database.read() as db:
        session, item = exports.poll_record(db, owner, built.public_id, built.polls["approval"])
        document = json_export.document(session, [item], now=NOW)
    (entry,) = document["polls"]
    assert entry["number"] == 2 and len(entry["ballots"]) == 6
    assert entry["result"]["data"]["outcome"]["tied"] == [2, 3]
    assert json_export.dumps(document).endswith(b"}\n")
