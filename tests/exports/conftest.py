"""A complete mock session for the export tests: the three methods with blank ballots and ties,
cancelled polls, a draft. ``build_session`` also serves the web tests of the exports."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from agaura.models import User
from agaura.services import assemblies, meeting, notes, polls
from agaura.services.meeting import Member
from agaura.services.polls import PollInput
from tests.conftest import RESOLUTION, World

APPROVAL = PollInput(
    title="Élection du conseil (2 sièges)",
    method="approval",
    options=["Alice", "Bruno", "Chloé", "David"],
    params={"seats": 2, "max_choices": 2},
)
MAJORITY_JUDGMENT = PollInput(
    title="Projets de quartier",
    method="majority_judgment",
    options=["Jardin", "Fresque"],
    description="Évaluez chaque projet.",
)
DRAFT = replace(RESOLUTION, title="Brouillon secret")
INJECTION = PollInput(
    title="=Lieu",
    method="single_choice",
    options=['=HYPERLINK("http://example.org")', "+33 Salle", "-Parc", "@Ferme", "Gare"],
    blank_label="=1+1",
)

TRIBUNE = [
    Member("Marie Dupont", "Présidente de séance"),
    Member("Paul Martin", "Secrétaire"),
    Member("Léa Bernard"),
]
AGENDA = ["Approbation du procès-verbal", "Rapport moral", "Rapport financier"]
# Agenda item under discussion when each poll is pushed (``with_meeting``); others: none.
ITEMS = {"resolution": 1, "approval": 2, "majority_judgment": 2, "cancelled_closed": 3}
# Notes written just before a poll is pushed (``with_meeting``): (text, internal).
NOTES = {
    "resolution": ("Le trésorier présente les comptes.\nAucune question.", False),
    "approval": ("Penser à relancer la mairie.", True),
    "unpublished": ("Motion déposée en séance par Léa Bernard.", False),
}

Content = Mapping[str, Any] | None

# Six voters. Resolution: 3 For, 2 Against, 1 blank (adopted). Approval: Alice elected, Bruno
# and Chloé tied for the second seat. Majority Judgment: identical profiles, a tie.
VOTES: dict[str, list[Content]] = {
    "resolution": [{"choice": 1}, {"choice": 1}, {"choice": 2}, None, {"choice": 1}, {"choice": 2}],
    "approval": [
        {"approved": [1, 2]},
        {"approved": [1, 3]},
        {"approved": [1]},
        None,
        {"approved": [2, 3]},
        {"approved": [4]},
    ],
    "majority_judgment": [
        {"grades": {"1": 0, "2": 1}},
        {"grades": {"1": 1, "2": 0}},
        {"grades": {"1": 2, "2": 2}},
        None,
        {"grades": {"1": 3}},  # Fresque ungraded: "Reject"
        {"grades": {"1": 5, "2": 3}},
    ],
}


@dataclass
class Built:
    public_id: str
    polls: dict[str, int] = field(default_factory=dict)


def vote_all(world: World, public_id: str, tokens: list[str], poll_id: int, votes: list) -> None:
    for token, content in zip(tokens, votes, strict=False):
        world.vote(public_id, token, poll_id, content)


def build_session(
    world: World, owner: User, *, close: bool = True, with_meeting: bool = False
) -> Built:
    """Polls are pushed one minute apart, so their session order is deterministic.

    ``with_meeting`` also fills the tribune (``TRIBUNE``) and the agenda (``AGENDA``), puts the
    item under discussion before each poll (``ITEMS``) and writes notes (``NOTES``, half a
    minute before their poll).
    """
    start = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.UTC)
    public_id = world.assembly(
        owner,
        title="Assemblée générale 2026",
        date=dt.date(2026, 6, 20),
        place="Salle des fêtes",
        registered_count=20,
        quorum_rule="1/4",
    )
    built = Built(public_id)
    tokens = world.voters(owner, public_id, 6)
    definitions = {
        "resolution": RESOLUTION,
        "approval": APPROVAL,
        "majority_judgment": MAJORITY_JUDGMENT,
        "cancelled_closed": replace(RESOLUTION, title="Cotisation 2027"),
        "cancelled_open": replace(RESOLUTION, title="Modification des statuts"),
        "unpublished": replace(RESOLUTION, title="Motion non publiée"),
    }
    if with_meeting:
        with world.database.write() as db:
            meeting.set_tribune(db, owner, public_id, TRIBUNE)
            meeting.set_agenda(db, owner, public_id, AGENDA)
    for minute, (key, data) in enumerate(definitions.items()):
        pushed = start + dt.timedelta(minutes=minute)
        with world.database.write() as db:
            if with_meeting:
                meeting.set_current_item(db, owner, public_id, ITEMS.get(key))
                if key in NOTES:
                    text, internal = NOTES[key]
                    at = pushed - dt.timedelta(seconds=30)
                    notes.add_note(db, owner, public_id, text, internal=internal, now=at)
            poll = polls.create_poll(db, owner, public_id, data)
            polls.open_poll(db, owner, poll.id, now=pushed)
            built.polls[key] = poll.id
        vote_all(world, public_id, tokens, poll.id, VOTES.get(key, VOTES["resolution"][:3]))
        with world.database.write() as db:
            if key == "cancelled_open":
                polls.cancel_poll(db, owner, poll.id, "Erreur dans l'énoncé")
                continue
            polls.close_poll(db, owner, poll.id)
            if key == "cancelled_closed":
                polls.cancel_poll(db, owner, poll.id, "Vote à refaire")
            elif key != "unpublished":
                polls.publish_result(db, owner, poll.id, voters=True)
    built.polls["draft"] = world.poll(owner, public_id, DRAFT, open_it=False)
    if close:
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
    return built


@pytest.fixture
def built(world: World, owner: User) -> Built:
    return build_session(world, owner, with_meeting=True)


CONDORCET = PollInput(
    title="Nom de la lettre",
    method="condorcet",
    options=["Gazette", "Écho", "Lien"],
)
# A cycle, settled by Schulze: Écho beats Gazette 6 to 3, Gazette beats Lien 7 to 2, Lien beats
# Écho 5 to 4; the weakest duel (Lien over Écho) gives way, so Écho > Gazette > Lien. One ballot
# ranks two options only (Gazette last), and one is blank.
CONDORCET_VOTES: list[Content] = (
    [{"ranks": {"2": 1, "1": 2, "3": 3}}] * 4
    + [{"ranks": {"1": 1, "3": 2, "2": 3}}] * 3
    + [{"ranks": {"3": 1, "2": 2, "1": 3}}]
    + [{"ranks": {"3": 1, "2": 2}}]
    + [None]
)
