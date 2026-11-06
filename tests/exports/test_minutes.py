"""PDF minutes (FR-62): content of the HTML given to WeasyPrint, and the PDF itself."""

from __future__ import annotations

import datetime as dt
import re
from html import unescape

import pytest
from sqlalchemy import select
from weasyprint import HTML

from agaura.exports import minutes
from agaura.models import AccessMode, AuditEntry, User
from agaura.pdf import OfflineFetcher
from agaura.services import access, audit, exports, meeting, notes
from tests.conftest import World
from tests.exports.conftest import CONDORCET, CONDORCET_VOTES, Built, build_session, vote_all

NOW = dt.datetime(2026, 6, 20, 18, 30, tzinfo=dt.UTC)


def minutes_html(world: World, owner: User, public_id: str) -> str:
    with world.database.read() as db:
        session = exports.session_record(db, owner, public_id)
    return minutes.html(session, now=NOW)


def text_of(html: str) -> str:
    """Visible text of the body, whitespace collapsed."""
    body = html.split("<body>", 1)[1]
    text = " ".join(re.findall(r"[^ \t\n\r\f\v]+", unescape(re.sub(r"<[^>]+>", " ", body))))
    # A tag before a no-break space ("</strong>\u00a0:") leaves no gap in the page.
    return re.sub(r" ?\u00a0 ?", "\u00a0", text)


def test_full_session(world: World, owner: User, built: Built) -> None:
    html = minutes_html(world, owner, built.public_id)
    text = text_of(html)
    expected = [
        "Procès-verbal des votes",
        "Assemblée générale 2026",
        "20 juin 2026 · Salle des fêtes",
        built.public_id,
        "Généré par AGaura le 20 juin 2026, 20:30 (heure de Paris)",
        "Mot de passe de la session",
        "6 (appareils ayant rejoint la session)",
        "5 présents requis (1/4 des membres inscrits)\u00a0: atteint",
        "6 votes lancés, dont 2 annulés",
        # Resolution, with its blank ballot and threshold
        "N°\u00a01 · Approbation des comptes",
        "lancé à 14:00",
        # The blank ballots by the poll's blank label ("Abstention"), as on the ballot.
        "Bulletins déposés\u00a0: 6 Abstention\u00a0: 1 Suffrages exprimés\u00a0: 5",
        "Participation\u00a0: 6 / 6 (100,0 %)",
        "Requis\u00a0: plus de la moitié des suffrages exprimés.",
        "Adoptée",
        # Approval with a tie for the last seat
        "N°\u00a02 · Élection du conseil (2 sièges)",
        "Égalité entre Bruno, Chloé\u00a0: appliquez vos statuts",
        "Élus\u00a0: Alice",
        "1 siège reste à attribuer entre les candidat·es à égalité.",
        # Majority Judgment: merit profile as a table, tie reported
        "N°\u00a03 · Projets de quartier",
        "Évaluez chaque projet.",
        "Égalité entre Jardin, Fresque\u00a0: appliquez vos statuts",
        "Mention majoritaire",
        "À rejeter",
        # Cancellations, with their reason
        "N°\u00a04 · Cotisation 2027",
        "Annulé\u00a0: Vote à refaire",
        "N°\u00a05 · Modification des statuts",
        "Annulé\u00a0: Erreur dans l'énoncé",
        "3 bulletins conservés, non dépouillés.",
        # Closed but never published: still in the minutes
        "N°\u00a06 · Motion non publiée",
        "Empreinte SHA-256 des bulletins",
    ]
    for snippet in expected:
        assert snippet in text, snippet
    assert "Brouillon secret" not in text
    # The tribune in its order, then the agenda, before the polls; no signature lines.
    tribune = "Tribune Marie Dupont — Présidente de séance Paul Martin — Secrétaire Léa Bernard "
    assert tribune in text
    agenda = "Ordre du jour Approbation du procès-verbal Rapport moral Rapport financier"
    assert agenda + " Déroulé de la séance" in text
    # Course of the session: notes and polls by agenda item, in time order; the internal note
    # is left out; then what happened outside the agenda.
    course = text.split("Déroulé de la séance", 1)[1].split(" Votes N°\u00a01", 1)[0]
    expected_course = [
        "1. Approbation du procès-verbal",
        "13:59 Le trésorier présente les comptes. Aucune question.",
        "14:00 N°\u00a01 · Approbation des comptes Adoptée Détail\u00a0: vote n° 1",
        "2. Rapport moral",
        "14:01 N°\u00a02 · Élection du conseil (2 sièges)",
        "14:02 N°\u00a03 · Projets de quartier",
        "3. Rapport financier",
        "14:03 N°\u00a04 · Cotisation 2027 Annulé\u00a0: Vote à refaire",
        "Hors ordre du jour",
        "14:04 N°\u00a05 · Modification des statuts Annulé\u00a0: Erreur dans l'énoncé",
        "14:04 Motion déposée en séance par Léa Bernard.",
        "14:05 N°\u00a06 · Motion non publiée Adoptée",
    ]
    positions = [course.index(snippet) for snippet in expected_course]
    assert positions == sorted(positions)
    assert "relancer la mairie" not in html
    assert 'href="#poll-1"' in html and '<section class="poll" id="poll-1">' in html
    assert "Le trésorier présente les comptes.\nAucune question." in html  # line break kept
    assert '<ol class="agenda">' in html
    for footer in ("Président·e de séance", "Secrétaire de séance", "signature"):
        assert footer not in html, footer
    # Attendance record, in time order, after the session facts.
    assert text.index("Suivi des présences") < text.index("Ordre du jour")
    assert "Heure Événement Effectif compté Connexions" in text
    for snippet in ("Vote n° 1 lancé", "Vote n° 1 clos", "Session ouverte", "Session terminée"):
        assert snippet in text.split("Suivi des présences", 1)[1], snippet
    assert "Vote n° 5 clos" not in text  # cancelled while open: never closed
    assert (
        text.count("Empreinte SHA-256 des bulletins") == 4
    )  # results announced or not, cancellations aside
    assert html.count('<svg class="merit"') == 2  # merit profiles of Majority Judgment
    assert "vector-effect" not in html  # not supported by WeasyPrint


def test_digests_are_printed(world: World, owner: User, built: Built) -> None:
    html = minutes_html(world, owner, built.public_id)
    with world.database.read() as db:
        session = exports.session_record(db, owner, built.public_id)
        digests = [
            r.poll.result.ballots_sha256
            for r in session.polls
            if r.poll.result and r.poll.status.value != "cancelled"
        ]
    assert len(digests) == 4  # the cancelled one is not printed
    for digest in digests:
        assert digest in html


def test_log_anchor_is_printed(world: World, owner: User, built: Built) -> None:
    """The hash of the session's last log entry, shortened, anchors the log (EXPORTS.md)."""
    with world.database.read() as db:
        session = exports.session_record(db, owner, built.public_id)
        last = db.scalars(
            select(AuditEntry)
            .where(AuditEntry.assembly_id == session.assembly.id)
            .order_by(AuditEntry.id.desc())
        ).first()
    assert last is not None and session.log_anchor == last.hash[: audit.ANCHOR_LENGTH]
    text = text_of(minutes.html(session, now=NOW))
    assert (
        "Empreinte du journal de la session à l'édition de ce document"
        f"\u00a0: {session.log_anchor}." in text
    )
    assert text.count(session.log_anchor) == 1


def test_pdf(world: World, owner: User, built: Built) -> None:
    with world.database.read() as db:
        session = exports.session_record(db, owner, built.public_id)
    pdf = minutes.render(session, now=NOW)
    assert pdf.startswith(b"%PDF-")
    document = HTML(string=minutes.html(session, now=NOW), url_fetcher=OfflineFetcher()).render()
    assert len(document.pages) >= 2


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "aucun"),
        (
            {"quorum_rule": "50", "registered_count": 120},
            "50 présents requis\u00a0: non atteint",
        ),
        ({"quorum_rule": "1/3"}, "vérification impossible sans le nombre de membres inscrits"),
        ({"quorum_rule": "1", "registered_count": 3}, "1 présent requis\u00a0: non atteint"),
    ],
)
def test_quorum(world: World, owner: User, fields: dict, expected: str) -> None:
    public_id = world.assembly(owner, in_session=False, **fields)
    text = text_of(minutes_html(world, owner, public_id))
    assert expected in text
    assert "Aucun vote n'a été lancé pendant cette session." in text
    assert "0 vote lancé" in text


def test_codes_mode_headcount_and_open_poll(world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    world.codes(owner, public_id, 3)
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 12)
    world.poll(owner, public_id)
    text = text_of(minutes_html(world, owner, public_id))
    for snippet in (
        "Codes individuels",
        "un code imprimé par personne",
        "0 (codes activés)",
        "12 personnes comptées dans la salle",
        "Le vote était encore ouvert lors de la génération de ce procès-verbal",
        "En cours",
    ):
        assert snippet in text, snippet


def test_condorcet_ranking_and_duels(world: World, owner: User) -> None:
    from agaura.services import polls

    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, CONDORCET)
    tokens = world.voters(owner, public_id, len(CONDORCET_VOTES))
    vote_all(world, public_id, tokens, poll_id, CONDORCET_VOTES)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    html = minutes_html(world, owner, public_id)
    text = text_of(html)
    for snippet in (
        "Méthode Condorcet",
        "Règle\u00a0: l'option préférée à chacune des autres, en duel, l'emporte",
        "Résultat\u00a0: Écho",
        "Aucune option ne gagne tous ses duels",
        "Rang Option Duels gagnés Duels perdus 1 Écho 1 1 2 Gazette 1 1 3 Lien 1 1",
        "Duels un contre un 1 2 3 1 Écho \u2013 6 4 2 Gazette 3 \u2013 7 3 Lien 5 2 \u2013",
        "Chaînes de duels gagnés les plus fortes",
    ):
        assert snippet in text, snippet
    assert '<td class="num is-won"><strong>6</strong></td>' in html
    assert "visually-hidden" not in html.split("<body>", 1)[1]  # no stylesheet of the site


def test_session_in_preparation(world: World, owner: User) -> None:
    built = build_session(world, owner, close=False)
    text = text_of(minutes_html(world, owner, built.public_id))
    assert "Session terminée" not in text and "Session ouverte" in text


def test_no_tribune_no_agenda_no_signature_lines(world: World, owner: User) -> None:
    """Without a tribune or an agenda, neither heading appears; never any signature line."""
    public_id = world.assembly(owner)
    html = minutes_html(world, owner, public_id)
    text = text_of(html)
    for absent in ("Tribune", "Ordre du jour", "Président·e de séance", "Secrétaire de séance"):
        assert absent not in text, absent
    assert "signature" not in html and '<ol class="agenda">' not in html
    assert "Suivi des présences" not in text  # only the opening reading: nothing to report


def test_attendance_record_with_a_headcount(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 35)
    text = text_of(minutes_html(world, owner, public_id))
    assert "Suivi des présences" in text
    assert "Effectif saisi 35 0" in text
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, None)  # removed: shown as a blank
    assert "Effectif saisi \u2013 0" in text_of(minutes_html(world, owner, public_id))


def test_course_without_agenda_is_one_feed(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        notes.add_note(db, owner, public_id, "Ouverture par la présidente.")
        notes.add_note(db, owner, public_id, "Réservé à la tribune.", internal=True)
    world.poll(owner, public_id)  # pushed just after, still open
    text = text_of(minutes_html(world, owner, public_id))
    course = text.split("Déroulé de la séance", 1)[1].split(" Votes N°\u00a01", 1)[0]
    assert re.match(r" \d\d:\d\d Ouverture par la présidente\. \d\d:\d\d N°\u00a01", course)
    assert "N°\u00a01 · Approbation des comptes Le vote était encore ouvert" in course
    assert "Hors ordre du jour" not in text and "Réservé à la tribune" not in text


def test_no_course_without_notes_or_agenda(world: World, owner: User) -> None:
    built = build_session(world, owner)
    assert "Déroulé de la séance" not in text_of(minutes_html(world, owner, built.public_id))


def test_course_with_everything_on_the_agenda(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Budget"])
        meeting.set_current_item(db, owner, public_id, 1)
        notes.add_note(db, owner, public_id, "La séance est ouverte.")
    text = text_of(minutes_html(world, owner, public_id))
    course = text.split("Déroulé de la séance", 1)[1]
    assert "1. Ouverture" in course and "La séance est ouverte." in course
    assert "2. Budget Ni note ni vote pour ce point." in course
    assert "Hors ordre du jour" not in course


def test_a_resolution_is_named_as_in_the_editor(world: World, owner: User, built: Built) -> None:
    """A single choice that adopts its first option is a "Résolution" (FR-31), not a "Choix
    unique", wherever the poll is described."""
    text = text_of(minutes_html(world, owner, built.public_id))
    assert "N°\u00a01 · Approbation des comptes Résolution · lancé à" in text
    assert "N°\u00a03 · Projets de quartier Jugement majoritaire · lancé à" in text
