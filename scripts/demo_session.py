"""Run two complete assembly sessions through the services and print their results.

    uv run python scripts/demo_session.py                 # throwaway database
    uv run python scripts/demo_session.py --database data/demo.db

Session 1 uses the session password (40 phones, entries locked after joining) and five polls,
one per voting method of the creation page (FR-31). Session 2 uses printed individual codes.
Votes are random but reproducible (``--seed``).
"""

from __future__ import annotations

import argparse
import random
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agaura.db import Database
from agaura.models import AccessMode, Poll, User
from agaura.security import Keyring
from agaura.services import access, accounts, assemblies, audit, ballots, polls
from agaura.services.assemblies import AssemblyInput
from agaura.services.errors import AlreadyVotedError, InvalidInputError
from agaura.services.polls import PollInput

GRADES = ("Very good", "Good", "Fairly good", "Passable", "Insufficient", "Reject")
Ballot = Mapping[str, Any] | None


def resolution(rng: random.Random) -> Ballot:
    return rng.choices([{"choice": 1}, {"choice": 2}, None], weights=[70, 20, 10])[0]


def choose_one(rng: random.Random) -> Ballot:
    return {"choice": rng.choices([1, 2, 3], weights=[45, 35, 20])[0]}


def elect(rng: random.Random) -> Ballot:
    popularity = {1: 0.8, 2: 0.6, 3: 0.55, 4: 0.3, 5: 0.2}
    approved = [option for option, p in popularity.items() if rng.random() < p][:3]
    return {"approved": approved} if approved else None


def grade(rng: random.Random) -> Ballot:
    centres = {1: 1.2, 2: 2.0, 3: 3.5, 4: 2.1}
    return {
        "grades": {
            option: min(5, max(0, round(rng.gauss(centre, 1.1))))
            for option, centre in centres.items()
        }
    }


def rank(rng: random.Random) -> Ballot:
    """Orders of preference, best first; one voter in ten ranks only a first choice."""
    orders = [(1, 2, 3), (2, 3, 1), (3, 1, 2), (1, 3, 2), (2, 1, 3)]
    order = rng.choices(orders, weights=[30, 25, 20, 15, 10])[0]
    if rng.random() < 0.1:
        order = order[:1]
    return {"ranks": {option: place for place, option in enumerate(order, start=1)}}


DEMO_POLLS: list[tuple[PollInput, Callable[[random.Random], Ballot]]] = [
    (
        PollInput(
            title="Approbation des comptes 2025",
            method="single_choice",
            options=["Pour", "Contre"],
            params={"adopt_option": 1},
            blank_label="Abstention",
        ),
        resolution,
    ),
    (
        PollInput(
            title="Lieu de la fête annuelle",
            method="single_choice",
            options=["Salle des fêtes", "Parc municipal", "Ferme du Moulin"],
        ),
        choose_one,
    ),
    (
        PollInput(
            title="Élection du conseil d'administration (3 sièges)",
            method="approval",
            options=["Alice Martin", "Bruno Petit", "Chloé Durand", "David Leroy", "Emma Roux"],
            params={"seats": 3, "max_choices": 3},
        ),
        elect,
    ),
    (
        PollInput(
            title="Projets à financer en 2026",
            method="majority_judgment",
            options=["Jardin partagé", "Fresque murale", "Navette", "Atelier vélo"],
        ),
        grade,
    ),
    (
        PollInput(
            title="Nom de la lettre d'information",
            method="condorcet",
            options=["La Gazette", "L'Écho du quartier", "Le Lien"],
        ),
        rank,
    ),
]


def describe(poll: Poll) -> list[str]:
    """Human-readable summary of a stored result."""
    if poll.result is None:
        return [f"  {poll.title}: {poll.status.value}"]
    data = poll.result.data
    counts, outcome = data["counts"], data["outcome"]
    labels = {option.position: option.label for option in poll.options}
    lines = [
        f"  {poll.title}",
        f"    ballots {counts['cast']} (blank {counts['blank']}, valid {counts['valid']}),"
        f" attendees {data['attendees']}",
    ]
    if poll.method == "single_choice":
        for row in outcome["options"]:
            lines.append(f"    {labels[row['option']]:<24} {row['votes']:>3}")
        if "adopt_option" in poll.params:
            verdict = {True: "ADOPTED", False: "REJECTED", None: "UNDECIDED"}[outcome["adopted"]]
            lines.append(f"    → {verdict}")
        else:
            lines.append(f"    → {outcome['status']}: {labels.get(outcome['winner'], '-')}")
    elif poll.method == "approval":
        for row in outcome["options"]:
            lines.append(f"    {labels[row['option']]:<24} {row['approvals']:>3}")
        elected = ", ".join(labels[option] for option in outcome["elected"])
        lines.append(f"    → {outcome['status']}: elected {elected}")
    else:
        for row in outcome["options"]:
            if poll.method == "condorcet":
                duels = f"won {row['wins']}, lost {row['losses']}"
                lines.append(f"    {labels[row['option']]:<24} {duels}")
            else:
                lines.append(f"    {labels[row['option']]:<24} {GRADES[row['majority_grade']]}")
        ranking = " > ".join(" = ".join(labels[o] for o in group) for group in outcome["ranking"])
        lines.append(f"    → {outcome['status']}: {ranking}")
        if poll.method == "condorcet":
            winner = outcome["condorcet_winner"]
            lines.append(f"    Condorcet winner: {labels[winner] if winner else 'none'}")
    lines.append(f"    sha256 {poll.result.ballots_sha256}")
    return lines


def vote_all(
    database: Database,
    public_id: str,
    tokens: list[str],
    poll_id: int,
    make_ballot: Callable[[random.Random], Ballot],
    rng: random.Random,
) -> None:
    for token in tokens:
        with database.write() as db:
            ballots.cast_vote(db, public_id, token, poll_id, make_ballot(rng))


def password_session(database: Database, keyring: Keyring, owner: User, rng: random.Random) -> str:
    with database.write() as db:
        assembly = assemblies.create_assembly(
            db,
            keyring,
            owner,
            AssemblyInput(
                title="Assemblée générale 2026",
                place="Maison des associations",
                registered_count=120,
                quorum_rule="1/4",
            ),
        )
        public_id = assembly.public_id
        poll_ids = [polls.create_poll(db, owner, public_id, data).id for data, _ in DEMO_POLLS]
        assemblies.open_assembly(db, owner, public_id)
        password = access.session_password(db, keyring, owner, public_id)
    print(f"Session 1 · agaura.org/{public_id} · session password {password}")

    tokens = []
    for _ in range(40):
        typed = f"{password[:3]} {password[3:]}"  # spaces are ignored (FR-24)
        with database.write() as db:
            tokens.append(access.join(db, keyring, public_id, typed).token)
    with database.write() as db:
        access.set_entries_locked(db, owner, public_id, True)
        access.set_headcount(db, owner, public_id, 40)
        assembly = assemblies.get_assembly(db, owner, public_id)
        present = access.attendees(db, assembly)
        print(f"  {present} phones joined, quorum {assemblies.quorum_required(assembly)}")

    for poll_id, (_, make_ballot) in zip(poll_ids, DEMO_POLLS, strict=True):
        with database.write() as db:
            polls.open_poll(db, owner, poll_id)
        vote_all(database, public_id, tokens, poll_id, make_ballot, rng)
        if poll_id == poll_ids[0]:
            try:
                with database.write() as db:
                    ballots.cast_vote(db, public_id, tokens[0], poll_id, {"choice": 1})
            except AlreadyVotedError:
                print("  second ballot from the same phone refused: already_voted")
        with database.write() as db:
            polls.close_poll(db, owner, poll_id)
            polls.publish_result(db, owner, poll_id, voters=True)

    with database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    with database.read() as db:
        for poll in polls.list_polls(db, owner, public_id):
            print("\n".join(describe(poll)))
    return public_id


def codes_session(database: Database, keyring: Keyring, owner: User, rng: random.Random) -> str:
    with database.write() as db:
        assembly = assemblies.create_assembly(
            db,
            keyring,
            owner,
            AssemblyInput(title="Assemblée générale extraordinaire"),
            access_mode=AccessMode.CODES,
        )
        public_id = assembly.public_id
        codes = access.create_code_batch(db, keyring, owner, public_id, 12).codes
        poll_id = polls.create_poll(
            db,
            owner,
            public_id,
            PollInput(
                title="Modification des statuts",
                method="single_choice",
                options=["Pour", "Contre"],
                params={"adopt_option": 1, "threshold": "two_thirds"},
                blank_label="Abstention",
            ),
        ).id
        assemblies.open_assembly(db, owner, public_id)
    print(f"\nSession 2 · agaura.org/{public_id} · 12 printed codes, e.g. {codes[0]}")

    tokens = []
    for code in codes[:10]:
        with database.write() as db:
            tokens.append(access.join(db, keyring, public_id, code.lower()).token)
    with database.write() as db:
        # Same code on a second phone: the first phone loses access, still one vote.
        tokens[0] = access.join(db, keyring, public_id, codes[0]).token
        print(f"  {access.attendees(db, assemblies.get_assembly(db, owner, public_id))} codes used")
        polls.open_poll(db, owner, poll_id, minutes=5)
    try:
        with database.write() as db:
            ballots.cast_vote(db, public_id, tokens[1], poll_id, {"choice": 7})
    except InvalidInputError as error:
        print(f"  invalid ballot refused: {error.code}")
    vote_all(database, public_id, tokens, poll_id, resolution, rng)
    with database.write() as db:
        polls.close_poll(db, owner, poll_id)
        assemblies.close_assembly(db, owner, public_id)
    with database.read() as db:
        for poll in polls.list_polls(db, owner, public_id):
            print("\n".join(describe(poll)))
    return public_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, help="SQLite file to create or reuse")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as scratch:
        path = args.database or Path(scratch) / "demo.db"
        keyring = Keyring("demo-secret-key-for-local-runs-only")
        database = Database(f"sqlite:///{path}", audit_key=keyring.audit_key)
        database.upgrade()
        rng = random.Random(args.seed)  # noqa: S311 (simulated votes, not security)
        with database.write() as db:
            owner = accounts.create_user(
                db,
                f"demo-{rng.randrange(10**6)}@example.org",
                "correct horse battery",
                verified=True,
            )
        password_session(database, keyring, owner, rng)
        codes_session(database, keyring, owner, rng)
        with database.read() as db:
            report = audit.verify_chain(db)
        print(f"\nAudit log: {report.checked} entries, chain {'intact' if report.ok else 'BROKEN'}")
        database.dispose()
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
