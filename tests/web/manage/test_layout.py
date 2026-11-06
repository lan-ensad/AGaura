"""Layout of the session console: rubrics, Steering by state of the session (checklist, commands,
summary), each card in its rubric in every state, and the status bar refreshed with the live
panel."""

from __future__ import annotations

import dataclasses
import re
from html import unescape

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import AccessMode, User
from agaura.services import assemblies, polls
from agaura.services.assemblies import AssemblyInput
from agaura.services.polls import PollInput
from agaura.web.manage.editor import EditorFields, fields_from_kind
from tests.conftest import RESOLUTION, World
from tests.web.conftest import post
from tests.web.manage.test_delegates import ALL_RIGHTS, card_of, make_link, open_link, primaries

RUBRICS = [
    "tab-cockpit",
    "tab-polls",
    "tab-meeting",
    "tab-agenda",
    "tab-notes",
    "tab-access",
    "tab-screen",
    "tab-documents",
    "tab-settings",
]


def text_of(html: str) -> str:
    text = " ".join(re.findall(r"[^ \t\n\r\f\v]+", unescape(re.sub(r"<[^>]+>", " ", html))))
    # A tag before a no-break space ("</strong>\u00a0:") leaves no gap in the page.
    return re.sub(r" ?\u00a0 ?", "\u00a0", text)


def page_of(client: TestClient, public_id: str) -> str:
    response = client.get(f"/manage/{public_id}")
    assert response.status_code == 200, response.text
    return response.text


def rubrics(html: str) -> dict[str, str]:
    """Each rubric's HTML, in the order of the page: some of RUBRICS, never another (Steering
    only once the session is open, Agenda not in a closed session without one)."""
    starts = [
        (match.start(), match.group(1))
        for match in re.finditer(r'<section class="rubric" id="(tab-[a-z]+)"', html)
    ]
    names = [name for _, name in starts]
    assert names == [name for name in RUBRICS if name in names]
    ends = [start for start, _ in starts[1:]] + [html.index("</main>")]
    return {name: html[start:end] for (start, name), end in zip(starts, ends, strict=True)}


def rubric_of(html: str, element_id: str) -> str | None:
    found = [name for name, part in rubrics(html).items() if f'id="{element_id}"' in part]
    assert len(found) <= 1
    return found[0] if found else None


def session_in(world: World, owner: User, state: str, **fields: object) -> str:
    public_id = world.assembly(owner, in_session=state != "preparation", **fields)
    if state == "closed":
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
    return public_id


@pytest.mark.parametrize("state", ["preparation", "in_session", "closed"])
def test_rubrics_in_order_with_their_links_and_unique_ids(
    admin: TestClient, world: World, owner: User, state: str
) -> None:
    """Steering exists once the session is open, and the console lands there; before, nothing
    is imposed: no Steering in the page, the sidebar or the tabs, and the console lands on
    Meeting, headed by the checklist. A closed session without an agenda has no Agenda."""
    html = page_of(admin, session_in(world, owner, state))
    preparation = state == "preparation"
    absent = {"preparation": "tab-cockpit", "in_session": None, "closed": "tab-agenda"}[state]
    assert list(rubrics(html)) == [name for name in RUBRICS if name != absent]
    landing = "tab-meeting" if preparation else "tab-cockpit"
    assert f'data-rubric data-default aria-labelledby="{landing}-title"' in html
    assert html.count("data-default") == 1
    for name in RUBRICS:  # once in the sidebar, once in the phone tabs (or their "More")
        shown = 0 if name == absent else 2
        assert html.count(f'href="#{name}" data-rubric-link') == shown
    assert ('href="#tab-cockpit"' in html) is not preparation
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert sorted({i for i in ids if ids.count(i) > 1}) == []
    assert '<script src="/static/js/console-boot.js' in html


@pytest.mark.parametrize("state", ["preparation", "in_session"])
def test_phone_tabs_lead_with_the_landing_rubric(
    admin: TestClient, world: World, owner: User, state: str
) -> None:
    """On a phone, four tabs: the rubric the console lands on (Meeting before the opening,
    Steering after), Polls, Agenda, Access; "More" holds the others, in the sidebar's order."""
    html = page_of(admin, session_in(world, owner, state))
    bar = html[html.index('<nav class="tab-bar"') :]
    bar = bar[: bar.index("</nav>")]
    tabs, more = bar.split('<details class="tab-more"')
    first = "tab-meeting" if state == "preparation" else "tab-cockpit"
    expected = [first, "tab-polls", "tab-agenda", "tab-access"]
    assert re.findall(r'href="#(tab-\w+)"', tabs) == expected
    assert re.findall(r'href="#(tab-\w+)"', more) == [
        *(["tab-meeting"] if state == "in_session" else []),
        "tab-notes",
        "tab-screen",
        "tab-documents",
        "tab-settings",
    ]
    assert text_of(tabs).split() == [
        "Séance" if state == "preparation" else "Conduite",
        "Votes",
        "Ordre",
        "du",
        "jour",
        "Accès",
    ]


@pytest.mark.parametrize(
    ("state", "places"),
    [
        (
            "preparation",
            {
                "checklist": "tab-meeting",
                "details": "tab-meeting",
                "tribune": "tab-meeting",
                "agenda": "tab-agenda",
                "notes": "tab-notes",
                "timer": "tab-meeting",
                "join": "tab-access",
                "presence": "tab-access",
                "live-panel": None,
                "summary": None,
                "cmd-notes": None,
                "cmd-timer": None,
            },
        ),
        (
            "in_session",
            {
                "agenda-now": "tab-cockpit",
                "live-panel": "tab-cockpit",
                "ready": "tab-cockpit",
                "cmd-notes": "tab-cockpit",
                "cmd-timer": "tab-cockpit",
                "cmd-join": "tab-cockpit",
                "cmd-presence": "tab-cockpit",
                "details": "tab-meeting",
                "tribune": "tab-meeting",
                "agenda": "tab-agenda",
                "notes": "tab-notes",
                "timer": "tab-meeting",
                "join": "tab-access",
                "presence": "tab-access",
                "checklist": None,
            },
        ),
        (
            "closed",
            {
                "summary": "tab-cockpit",
                "details": "tab-meeting",
                "tribune": "tab-meeting",
                "agenda": None,  # a closed session without agenda: nothing to show or change
                "notes": "tab-notes",
                "timer": None,  # the screen no longer shows it
                "join": "tab-access",
                "presence": "tab-access",
                "live-panel": None,
                "cmd-notes": None,
            },
        ),
    ],
)
def test_each_card_stays_in_its_rubric_in_every_state(
    admin: TestClient, world: World, owner: User, state: str, places: dict[str, str | None]
) -> None:
    """D-A: one feature, one card, one rubric; during the session Steering adds commands (ids
    prefixed cmd-) and no rubric points elsewhere. D-B: Meeting holds the details, then the
    chronometer above the tribune (the owner's request, 2026-11-06), Agenda and Session notes
    their card alone; Settings keeps the delegate links, the log, deletion."""
    html = page_of(admin, session_in(world, owner, state))
    assert {card: rubric_of(html, card) for card in places} == places
    for card in ("drafts", "results", "access", "links", "exports", "public-results"):
        assert rubric_of(html, card) is not None
    assert rubric_of(html, "delegates") == rubric_of(html, "log") == "tab-settings"
    assert "rubric-pointer" not in html and "cockpit-foot" not in html
    found = rubrics(html)
    meeting = text_of(found["tab-meeting"])
    head = "Avant d'ouvrir la session" if state == "preparation" else "Informations"
    assert meeting.startswith(f"Séance {head}")
    order = [found["tab-meeting"].find(f'id="{card}"') for card in ("details", "timer", "tribune")]
    if state == "closed":
        assert order[1] == -1 and 0 < order[0] < order[2]
    else:
        assert 0 < order[0] < order[1] < order[2]  # the chronometer above the tribune
        assert 'id="timer"' not in found["tab-screen"]
    assert 'href="#tab-meeting" data-rubric-link>' in html and ">Séance</span>" in html
    # A rubric of one card is named by the card's heading, not repeated above it.
    notes = found["tab-notes"]
    assert 'aria-labelledby="notes-title"' in notes and "rubric-title" not in notes
    assert text_of(notes).startswith("Notes de séance ")
    assert not text_of(notes).startswith("Notes de séance Notes de séance")
    assert ">Notes de séance</span>" in html
    if "tab-agenda" in found:
        agenda = found["tab-agenda"]
        assert 'aria-labelledby="agenda-title"' in agenda and "rubric-title" not in agenda
        assert text_of(agenda).startswith("Ordre du jour ")
        assert not text_of(agenda).startswith("Ordre du jour Ordre du jour")
        assert ">Ordre du jour</span>" in html


@pytest.mark.parametrize("state", ["preparation", "in_session"])
def test_details_are_read_only_once_the_session_is_open(
    admin: TestClient, world: World, owner: User, state: str
) -> None:
    public_id = session_in(world, owner, state, place="Salle des fêtes", quorum_rule="1/4")
    card = card_of(rubrics(page_of(admin, public_id))["tab-meeting"], "details")
    text = text_of(card)
    assert "Lieu Salle des fêtes" in text and "Date Date manquante" in text
    assert "Quorum 1/4" in text
    form = f'action="/manage/{public_id}/details#details"'
    assert (form in card) is (state == "preparation")
    if state == "preparation":
        assert '<details class="meeting-edit" id="details-edit" open>' in card  # date missing
    else:
        assert "Modifiables avant l'ouverture seulement" in text


def checklist_steps(admin: TestClient, public_id: str) -> list[tuple[str, str]]:
    """Each step of the checklist: its state, and where its line leads (a link, or the form
    that creates the trial vote)."""
    meeting = rubrics(page_of(admin, public_id))["tab-meeting"]
    pattern = (
        r'<li class="check-step (is-\w+)">\s*'
        r'(?:<a href="([^"]+)"|<form method="post" action="([^"]+)")'
    )
    return [(state, link or form) for state, link, form in re.findall(pattern, meeting)]


def test_checklist_before_opening(admin: TestClient, world: World, owner: User) -> None:
    public_id = session_in(world, owner, "preparation")

    def steps() -> list[tuple[str, str]]:
        return checklist_steps(admin, public_id)

    assert steps() == [
        ("is-todo", "#details"),
        ("is-todo", "#tribune"),
        ("is-todo", "#agenda"),
        ("is-todo", "#drafts"),
        ("is-done", "#join"),  # a session password always exists
        ("is-optional", "#links"),
        ("is-optional", f"/manage/{public_id}/polls/trial#checklist"),
    ]
    # A recap at the head of Meeting, one short line per step: its name, its state in words for
    # screen readers, what it lacks or holds, and the rubric of its card when it is another.
    meeting = rubrics(page_of(admin, public_id))["tab-meeting"]
    assert meeting.index('id="checklist"') < meeting.index('id="details"')
    recap = card_of(meeting, "checklist")
    lines = re.findall(r'<li class="check-step[^"]*">(.*?)</li>', recap, re.S)
    assert [text_of(line) for line in lines] == [
        "Informations (À faire) Date manquante · Lieu manquant",  # what they lack, by name
        "Tribune (À faire) Personne à la tribune pour l'instant.",
        "Ordre du jour (À faire) Pas encore d'ordre du jour. Ordre du jour",
        "Votes préparés (À faire) Aucun brouillon. Votes",
        "Mode d'accès (Prêt) Mot de passe de la session et QR code Accès",
        "Écran de projection Facultatif\u00a0: vérifier l'affichage Écran",
        "Vote d'essai Facultatif\u00a0: tester le réseau Créer",
    ]
    # The button shows its verb alone and keeps the whole action as its name.
    assert 'class="button small" aria-label="Créer le vote d\'essai"' in recap
    found = re.findall(r'<span class="check-home">(.*?)</span>\s*</a>', recap, re.S)
    assert [text_of(home) for home in found] == ["Ordre du jour", "Votes", "Accès", "Écran"]
    text = text_of(recap)
    assert "Avant d'ouvrir la session Étapes prêtes\u00a0: 1 sur 5 Informations" in text
    assert "Rien n'est obligatoire." in text
    assert "/open" not in recap  # the status bar opens the session
    sidebar = page_of(admin, public_id).split('<nav class="tab-bar"')[0]
    assert 'class="nav-count"' not in sidebar.split("<span>Séance</span>")[0]  # no "1/5"

    with world.database.write() as db:
        assemblies.update_assembly(
            db, owner, public_id, AssemblyInput(title="AG", date=None, place="Salle")
        )
    post(admin, f"/manage/{public_id}/tribune", {"name": "Marie Dupont", "role": ""})
    post(admin, f"/manage/{public_id}/agenda", {"items": "Accueil\nRapport moral"})
    world.poll(owner, public_id, open_it=False)
    assert [state for state, _ in steps()] == [
        "is-todo",  # the date is still missing
        "is-done",
        "is-done",
        "is-done",
        "is-done",
        "is-optional",
        "is-optional",
    ]
    recap = text_of(card_of(rubrics(page_of(admin, public_id))["tab-meeting"], "checklist"))
    assert "Tribune (Prêt) 1 personne" in recap and "Ordre du jour (Prêt) 2 points" in recap
    assert "Votes préparés (Prêt) 1 brouillon" in recap
    assert "Étapes prêtes\u00a0: 4 sur 5" in recap
    assert "Date manquante · Salle" in recap and "Lieu manquant" not in recap


def test_trial_vote_from_the_checklist(admin: TestClient, world: World, owner: User) -> None:
    """D-I: the optional line creates the draft "Tout le monde est-il connecté ?" (Oui, Non),
    marked as a trial; it never counts as a prepared poll, and one waits at a time."""
    public_id = session_in(world, owner, "preparation")
    response = post(admin, f"/manage/{public_id}/polls/trial")
    assert response.status_code == 303
    assert response.headers["location"] == f"/manage/{public_id}#checklist"
    with world.database.read() as db:
        (trial,) = polls.list_polls(db, owner, public_id)
        assert trial.trial and trial.title == "Tout le monde est-il connecté\u00a0?"
        assert [option.label for option in trial.options] == ["Oui", "Non"]
        assert trial.method == "single_choice"
    html = page_of(admin, public_id)  # the notice, once
    assert "Vote d'essai créé dans les brouillons" in text_of(html)
    assert "Étapes prêtes\u00a0: 1 sur 5" in text_of(html)  # optional: never counted
    steps = checklist_steps(admin, public_id)
    assert steps[3] == ("is-todo", "#drafts")  # a trial is not a prepared poll
    assert steps[-1] == ("is-done", f"#draft-{trial.id}")
    assert '<span class="badge neutral">Essai</span>' in draft_item(html, trial.id)

    post(admin, f"/manage/{public_id}/polls/trial")  # a second one waits: refused
    assert "Un vote d'essai attend déjà dans les brouillons" in text_of(page_of(admin, public_id))
    with world.database.read() as db:
        assert len(polls.list_polls(db, owner, public_id)) == 1


def test_checklist_with_individual_codes(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES, in_session=False)
    meeting = rubrics(page_of(admin, public_id))["tab-meeting"]
    assert '<li class="check-step is-todo">\n      <a href="#codes">' in meeting
    assert "Codes individuels\u00a0: à générer" in text_of(meeting)
    world.codes(owner, public_id, 2)
    meeting = rubrics(page_of(admin, public_id))["tab-meeting"]
    assert '<li class="check-step is-done">\n      <a href="#codes">' in meeting


def test_cockpit_during_the_session(admin: TestClient, world: World, owner: User) -> None:
    public_id = session_in(world, owner, "in_session")
    post(admin, f"/manage/{public_id}/agenda", {"items": "Accueil\nRapport moral\nQuestions"})
    draft = world.poll(owner, public_id, open_it=False)
    cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
    text = text_of(cockpit)
    assert "Aucun point en cours" in text and "À venir\u00a0: Accueil" in text
    assert f'action="/manage/{public_id}/agenda/current#tab-cockpit"' in cockpit
    assert f'action="/manage/{public_id}/polls/{draft}/open#tab-cockpit"' in cockpit
    assert "Aucun vote en cours." in text and "Prochain vote" in text
    # Each command leads to its card, in its rubric.
    found = re.findall(r'<a class="to-home" href="#([^"]+)">(.*?)</a>', cockpit, re.S)
    assert [(anchor, text_of(link)) for anchor, link in found] == [
        ("tab-polls", "Dans Votes"),
        ("agenda", "Tout l'ordre du jour"),
        ("drafts", "Brouillons, dans Votes"),
        ("notes", "Toutes les notes"),
        ("timer", "Réglages, dans Séance"),
        ("presence", "Dans Accès"),
        ("join", "Dans Accès"),
    ]

    post(admin, f"/manage/{public_id}/agenda/current", {"item": "next", "seen": ""})
    cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
    assert "Point 1 sur 3 Accueil" in text_of(cockpit)
    assert cockpit.count('class="is-current"') == 1

    post(admin, f"/manage/{public_id}/polls/{draft}/open", {})
    cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
    assert "Clore le vote" in cockpit and "Aucun brouillon." in text_of(cockpit)


def test_one_primary_button_per_card_in_steering(
    admin: TestClient, world: World, owner: User
) -> None:
    """R12: during the session, Steering shows three primary buttons, the next steps: "Next",
    "Close the vote" of the one poll open, "Push to voters" of the first draft ready. The
    tools (notes, chronometer, headcount) keep secondary buttons."""
    public_id = session_in(world, owner, "in_session")
    post(admin, f"/manage/{public_id}/agenda", {"items": "Accueil\nRapport moral\nQuestions"})
    post(admin, f"/manage/{public_id}/agenda/current", {"item": "next", "seen": ""})
    drafts = [world.poll(owner, public_id, open_it=False) for _ in range(4)]
    world.poll(owner, public_id)
    cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
    assert primaries(cockpit) == ["Clore le vote", "Suivant", "Lancer le vote"]
    pushes = re.findall(
        r'/polls/(\d+)/open#tab-cockpit">\s*<input[^>]*>\s*<button type="submit" class="([^"]*)"',
        card_of(cockpit, "ready"),
    )
    assert pushes == [(str(drafts[0]), "button primary small")]  # the next poll only
    assert "3 autres brouillons, dans Votes" in text_of(card_of(cockpit, "ready"))
    for card, label in (
        ("cmd-notes", "Ajouter"),
        ("cmd-timer", "Démarrer"),
        ("cmd-presence", "Enregistrer"),
    ):
        assert label in text_of(card_of(cockpit, card)) and primaries(card_of(cockpit, card)) == []

    # Two polls open: no single next step, so neither "Close the vote" is primary, in Steering
    # or at the top of Polls.
    world.poll(owner, public_id)
    html = page_of(admin, public_id)
    assert primaries(rubrics(html)["tab-cockpit"]) == ["Suivant", "Lancer le vote"]
    card = polls_open_card(rubrics(html)["tab-polls"])
    assert card.count("Clore le vote") == 2 and primaries(card) == []


def test_polls_rubric_leads_with_one_button_and_names_each_state(
    admin: TestClient, world: World, owner: User
) -> None:
    """R12 in Polls: the first draft and the one poll open lead; the way to Steering is quiet;
    each poll says its state in the words of its life cycle (● En cours, Clos, ✓ Publié,
    Annulé), and only the most recent result voters do not see offers a primary "Publish"."""
    public_id = session_in(world, owner, "in_session")
    older, newer, published, cancelled = (world.poll(owner, public_id) for _ in range(4))
    with world.database.write() as db:
        for poll in (older, newer, published):
            polls.close_poll(db, owner, poll)
        polls.publish_result(db, owner, published, voters=True)
        polls.cancel_poll(db, owner, cancelled, "Erreur de saisie")
    first, second = (world.poll(owner, public_id, open_it=False) for _ in range(2))
    world.poll(owner, public_id)
    votes = rubrics(page_of(admin, public_id))["tab-polls"]

    opened = polls_open_card(votes)
    assert primaries(opened) == ["Clore le vote"]
    assert '<span class="badge live is-live">En cours</span>' in opened
    assert '<a class="button small quiet" href="#live">Aller dans Conduite</a>' in opened

    assert primaries(draft_item(votes, first)) == ["Lancer le vote"]
    assert primaries(draft_item(votes, second)) == []

    results = card_of(votes, "results")

    def finished(poll_id: int) -> str:
        start = results.index(f'id="poll-{poll_id}"')
        end = results.find('<details class="finished-poll', start)  # the next tile, or the end
        return results[start : end if end != -1 else len(results)]

    assert '<span class="badge is-done">Publié</span>' in finished(published)
    assert '<span class="badge danger">Annulé</span>' in finished(cancelled)
    for poll in (older, newer):
        assert '<span class="badge neutral">Clos</span>' in finished(poll)
    assert primaries(finished(newer)) == ["Publier le résultat aux votants"]
    assert primaries(finished(older)) == []
    assert "Publier le résultat aux votants" in text_of(finished(older))
    assert primaries(results) == ["Publier le résultat aux votants"]


def test_closed_summary(admin: TestClient, world: World, owner: User) -> None:
    """Steering after the session: the summary with the minutes as its primary action, then
    three commands leading to Votes, Documents, Séance and the notes (console-pilotage-apres)."""
    public_id = session_in(world, owner, "in_session")
    poll = world.poll(owner, public_id, dataclasses.replace(RESOLUTION, title="Rapport moral"))
    published = world.poll(owner, public_id)
    cancelled = world.poll(owner, public_id)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll)
        polls.close_poll(db, owner, published)
        polls.publish_result(db, owner, published, voters=True)
        polls.cancel_poll(db, owner, cancelled, "Erreur de saisie")
        assemblies.close_assembly(db, owner, public_id)
    cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
    text = text_of(cockpit)
    summary = card_of(cockpit, "summary")
    assert "La session est terminée" in text
    assert "Votes tenus 2" in text and "Votes annulés 1" in text and "Présents 0" in text
    assert f'href="/manage/{public_id}/minutes.pdf"' in summary
    assert primaries(summary) == ["Procès-verbal (PDF)"]
    homes = re.findall(r'<a class="to-home" href="(#[\w-]+)">', cockpit)
    assert homes == ["#results", "#public-results", "#tribune", "#notes"]
    assert "Dans Votes" in text and "Dans Documents" in text and "Dans Séance" in text
    assert "Toutes les notes" in text_of(card_of(cockpit, "after-fix"))
    results = text_of(card_of(cockpit, "after-results"))
    assert (
        "1 résultat publié aux votants. Non publié\u00a0: «\u00a0Rapport moral\u00a0»." in results
    )
    assert "Désactivé. Une fois activé" in text_of(card_of(cockpit, "after-public"))
    assert "Nouveau vote" not in page_of(admin, public_id)


def test_status_bar_and_live_panel(admin: TestClient, world: World, owner: User) -> None:
    public_id = session_in(world, owner, "in_session", quorum_rule="2")
    (voter,) = world.voters(owner, public_id, 1)
    poll = world.poll(owner, public_id, RESOLUTION)
    panel = admin.get(f"/manage/{public_id}/panel").text
    status, polls_part = re.split(r'<div data-live-part="polls">', panel)
    assert status.startswith('<div data-live-part="status">')
    assert 'data-status="in_session"' in status
    text = text_of(status)
    # A single poll open: the ballots cast in it, a link to the polls open now (R15).
    assert "Présents 1" in text and "Quorum 2 · non atteint" in text and "Bulletins 0" in text
    assert "Votes en cours" not in text and '<a href="#live">0</a>' in status
    # The verb on the button, next to the state; the whole action as its name (label in name).
    assert (
        "Entrées ouvertes Verrouiller" in text and 'aria-label="Verrouiller les entrées"' in status
    )
    assert 'aria-hidden="true"' in status and "#i-lock" in status  # the lock, decorative
    assert "Terminer la session" in text and "data-timer-clock" not in status
    assert '<button type="submit" class="button small">Terminer la session</button>' in status
    assert "Clore le vote" in polls_part and "1 présent" in text_of(polls_part)

    # One ballot: the figure follows; a second poll open: the count of polls open instead.
    world.vote(public_id, voter, poll, {"choice": 1})
    status = admin.get(f"/manage/{public_id}/panel").text.split('data-live-part="polls"')[0]
    assert "Bulletins 1" in text_of(status)
    world.poll(owner, public_id, RESOLUTION)
    status = admin.get(f"/manage/{public_id}/panel").text.split('data-live-part="polls"')[0]
    assert "Votes en cours 2" in text_of(status) and "Bulletins" not in text_of(status)

    # The running chronometer shows in the status bar, on every rubric.
    post(admin, f"/manage/{public_id}/timer/start", {"mode": "countdown", "duration": "60"})
    status = admin.get(f"/manage/{public_id}/panel").text.split('data-live-part="polls"')[0]
    assert 'class="status-timer' in status and "1:00" in status

    # Entries are locked from the status bar; the page comes back where the form was sent.
    response = post(admin, f"/manage/{public_id}/entries", {"locked": "1"})
    assert response.headers["location"] == f"/manage/{public_id}"
    status = admin.get(f"/manage/{public_id}/panel").text.split('data-live-part="polls"')[0]
    assert "Entrées verrouillées" in text_of(status) and "Déverrouiller les entrées" in status
    response = post(admin, f"/manage/{public_id}/headcount", {"headcount": "1"})
    assert response.headers["location"] == f"/manage/{public_id}"  # each form names its copy


def test_closing_the_session_at_the_foot_of_steering(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    """On a phone the status bar leaves closing the session to the foot of Steering (D-F): the
    same form and confirmation, for the owner only; manage.css shows one or the other."""
    public_id = session_in(world, owner, "in_session")
    html = page_of(admin, public_id)
    status, steering = html.split('id="tab-cockpit"')[0], rubrics(html)["tab-cockpit"]
    foot = re.search(r'<div class="session-end">(.*?)</div>', steering, re.S)
    assert foot is not None and "cockpit-foot" not in steering
    assert steering[foot.end() :].strip() == "</section>"  # the very foot of Steering
    assert "Tous les votes sont clos\u00a0?" in text_of(foot[1])
    form = re.compile(r'<form method="post" action="/manage/\w+/close#tab-cockpit"[^>]*>')
    assert form.findall(foot[1]) == form.findall(status) != []
    assert (
        'data-confirm="Terminer la session\u00a0? Tous les accès des votants prendront fin."'
        in (form.findall(foot[1])[0])
    )
    assert primaries(foot[1]) == []  # a secondary button, as in the bar
    assert html.count('action="/manage/' + public_id + '/close#tab-cockpit"') == 2

    _, token = make_link(world, owner, public_id, ALL_RIGHTS)
    delegate = open_link(app, public_id, token).get(f"/manage/{public_id}").text
    assert "session-end" not in delegate and "/close" not in delegate
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    assert "session-end" not in page_of(admin, public_id)


def test_status_bar_before_opening_and_after_closing(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = session_in(world, owner, "preparation")
    status = page_of(admin, public_id).split('<section class="rubric"')[0]
    # Opened from the status bar, the console lands on Steering, which exists from then on.
    assert f'action="/manage/{public_id}/open#tab-cockpit"' in status
    assert "Présents" not in text_of(status) and "/entries" not in status
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
        assemblies.close_assembly(db, owner, public_id)
    status = page_of(admin, public_id).split('id="tab-cockpit"')[0]
    assert 'data-status="closed"' in status and "/open" not in status and "/close" not in status
    assert "/entries" not in status


def test_poll_launched_on_the_spot_shows_in_polls(
    admin: TestClient, world: World, owner: User
) -> None:
    """Created and launched from step 2 of "Nouveau vote", as a draft pushed from Polls: back to
    the top of Polls, which opens with the polls open now. Saved as a draft: to the drafts."""
    public_id = session_in(world, owner, "in_session")
    data = {
        **fields_from_kind("resolution").model_dump(exclude=set(EditorFields.BUTTONS)),
        "title": "Budget",
    }
    response = post(admin, f"/manage/{public_id}/polls/new", {**data, "action": "draft"})
    assert response.headers["location"] == f"/manage/{public_id}#drafts"
    response = post(admin, f"/manage/{public_id}/polls/new", {**data, "action": "launch"})
    assert response.headers["location"] == f"/manage/{public_id}#tab-polls"
    card = polls_open_card(rubrics(page_of(admin, public_id))["tab-polls"])
    assert "Budget" in text_of(card) and "hidden" not in opening_tag(card)


def polls_open_card(html: str) -> str:
    """The card of the polls open now at the top of Polls (_polls_open.html)."""
    start = html.index('<section class="card polls-open"')
    return html[start : html.index("</section>", start) + len("</section>")]


def opening_tag(html: str) -> str:
    return html[: html.index(">") + 1]


def test_polls_open_now_head_the_polls_rubric(admin: TestClient, world: World, owner: User) -> None:
    public_id = session_in(world, owner, "in_session")
    world.voters(owner, public_id, 1)
    draft = world.poll(owner, public_id, open_it=False)
    html = page_of(admin, public_id)
    assert rubric_of(html, "polls-open") == "tab-polls"
    card = polls_open_card(rubrics(html)["tab-polls"])
    assert " hidden" in opening_tag(card)  # no poll open: nothing to show
    assert 'class="open-poll"' not in card

    # Pushed from Polls: the redirect keeps the rubric of the form, the card lists the poll.
    response = post(admin, f"/manage/{public_id}/polls/{draft}/open", {})
    assert response.headers["location"] == f"/manage/{public_id}"
    html = page_of(admin, public_id)
    votes = rubrics(html)["tab-polls"]
    card = polls_open_card(votes)
    assert 'data-shown-with=".open-poll"' in opening_tag(card)
    assert "hidden" not in opening_tag(card)
    assert votes.index('id="polls-open"') < votes.index('id="drafts"')  # on top
    text = text_of(card)
    assert "Votes en cours" in text and "Approbation des comptes" in text
    assert "0 bulletin / 1 présent" in text
    assert f'action="/manage/{public_id}/polls/{draft}/close"' in card
    assert 'href="#live"' in card and "Aller dans Conduite" in text
    # Steering's list at one click, and the tiles of Polls under ids of their own, folded.
    assert html.count('data-live-part="polls"') == 1
    assert html.count('data-live-part="poll-tiles"') == 1
    assert f'<details class="open-poll poll-tile" id="open-poll-{draft}">' in card
    assert rubric_of(html, "live-panel") == "tab-cockpit"
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert sorted({i for i in ids if ids.count(i) > 1}) == []
    assert html.count("data-refresh-hint") == 2  # a timer that closes a poll, in both places
    panel = admin.get(f"/manage/{public_id}/panel").text
    assert panel.count('data-live-part="polls"') == 1
    assert panel.count('data-live-part="poll-tiles"') == 1

    post(admin, f"/manage/{public_id}/polls/{draft}/close", {})
    card = polls_open_card(rubrics(page_of(admin, public_id))["tab-polls"])
    assert " hidden" in opening_tag(card) and 'class="open-poll"' not in card


@pytest.mark.parametrize("state", ["preparation", "closed"])
def test_no_polls_open_now_outside_the_session(
    admin: TestClient, world: World, owner: User, state: str
) -> None:
    html = page_of(admin, session_in(world, owner, state))
    assert 'id="polls-open"' not in html and "data-refresh-hint" not in html


def draft_item(html: str, poll_id: int) -> str:
    # A tile (macros/polls.html): the details of the draft, its "⋯" menu nested inside.
    pattern = rf'<details class="poll-item poll-tile" id="draft-{poll_id}">.*?</details>\s*</li>'
    match = re.search(pattern, html, re.S)
    assert match
    return match.group(0)


@pytest.mark.parametrize("state", ["preparation", "in_session", "closed"])
def test_draft_has_one_action_and_a_menu(
    admin: TestClient, world: World, owner: User, state: str
) -> None:
    public_id = session_in(world, owner, "preparation")
    draft = world.poll(owner, public_id, open_it=False)
    if state != "preparation":
        with world.database.write() as db:
            assemblies.open_assembly(db, owner, public_id)
            if state == "closed":
                assemblies.close_assembly(db, owner, public_id)
    item = draft_item(page_of(admin, public_id), draft)
    visible, menu = item.split('<details class="menu" data-menu>')
    menu_text = text_of(menu)
    assert 'aria-label="Autres actions\u00a0: Approbation des comptes"' in menu
    assert ("Lancer le vote" in text_of(visible)) is (state == "in_session")
    assert ("Modifier" in text_of(visible)) is (state == "preparation")
    assert ("Lancer avec cette minuterie" in menu_text) is (state == "in_session")
    assert ('name="minutes"' in menu) is (state == "in_session")
    assert ("Modifier" in menu_text) is (state == "in_session")
    assert ("Dupliquer" in menu_text) is (state != "closed")
    assert "Supprimer" in menu_text and 'data-confirm="Supprimer ce brouillon\u00a0?"' in menu


def test_push_with_a_timer_from_the_menu(admin: TestClient, world: World, owner: User) -> None:
    public_id = session_in(world, owner, "in_session")
    draft = world.poll(owner, public_id, open_it=False)
    response = post(admin, f"/manage/{public_id}/polls/{draft}/open", {"minutes": "10"})
    assert response.status_code == 303
    with world.database.read() as db:
        poll = polls.get_poll(db, owner, draft)
        assert poll.closes_at is not None


def test_chronometer_settings_fold_under_the_last_choice(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = session_in(world, owner, "in_session")
    html = page_of(admin, public_id)
    card = html[html.index('id="timer"') : html.index("</section>", html.index('id="timer"'))]
    summary = re.search(r'<p class="timer-summary" data-timer-summary>(.*?)</p>', card, re.S)
    assert summary and text_of(summary.group(1)) == "Compte à rebours 3 min"
    settings = card.index('<details class="timer-settings"')
    folded = card[settings : card.index("</details>", settings)]
    assert "<summary>Modifier les réglages</summary>" in folded
    for field in ('name="mode"', 'name="duration"', 'name="label"', 'name="show"'):
        assert field in folded
    assert len(re.findall(r"<button[^>]*>Démarrer</button>", card)) == 1
    assert card.index("</details>", settings) < card.index(">Démarrer</button>")

    # The last choice comes back on the line after a reset.
    base = f"/manage/{public_id}/timer"
    data = {"mode": "countdown", "duration": "custom", "minutes": "2", "seconds": "30"}
    post(admin, f"{base}/start", {**data, "label": "Temps de parole"})
    post(admin, f"{base}/reset")
    html = page_of(admin, public_id)
    summary = re.search(r'<p class="timer-summary" data-timer-summary>(.*?)</p>', html, re.S)
    assert summary and text_of(summary.group(1)) == "Compte à rebours 2:30 Temps de parole"
    post(admin, f"{base}/start", {"mode": "up", "show": "1"})  # remembered as the mode used
    post(admin, f"{base}/reset")
    summary = re.search(
        r'<p class="timer-summary" data-timer-summary>(.*?)</p>', page_of(admin, public_id), re.S
    )
    assert summary and text_of(summary.group(1)).startswith("Chronomètre")
    assert "data-summary-duration hidden" in summary.group(1)


def test_long_help_folds_under_learn_more(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    html = page_of(admin, public_id)
    for card in ("codes", "notes"):
        start = html.index(f'id="{card}"')
        part = html[start : html.index("</section>", start)]
        assert '<details class="help">' in part, card
        assert '<span class="help-toggle">En savoir plus</span>' in part, card


def test_poll_closed_command_heads_steering_until_published_or_shown(
    admin: TestClient, world: World, owner: User
) -> None:
    """D-K: right after a poll closes, the "Vote clos" command heads Steering with its verdict,
    "Publier le résultat aux votants" (primary) and "Afficher sur l'écran", posting back to
    Steering; it goes once either is done, or once another poll closes after it."""
    public_id = session_in(world, owner, "in_session")
    first = world.poll(owner, public_id, RESOLUTION)

    def command() -> str | None:
        cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
        return card_of(cockpit, "just-closed") if 'id="just-closed"' in cockpit else None

    assert command() is None  # nothing closed yet
    response = post(admin, f"/manage/{public_id}/polls/{first}/close")
    assert response.headers["location"] == f"/manage/{public_id}"  # back where it was closed
    card = command()
    assert card is not None
    text = text_of(card)
    assert "Vote clos Détail, dans Votes Approbation des comptes" in text
    assert "0 bulletin" in text and "Détail, dans Votes" in text
    assert f'href="#poll-{first}"' in card
    assert primaries(card) == ["Publier le résultat aux votants"]
    assert "Afficher sur l'écran" in text
    for route in ("publish", "screen"):
        assert f'action="/manage/{public_id}/polls/{first}/{route}#tab-cockpit"' in card
    cockpit = rubrics(page_of(admin, public_id))["tab-cockpit"]
    assert cockpit.index('id="just-closed"') < cockpit.index('id="live"')  # on top

    # Published from the command: back to Steering, the command gone.
    response = post(admin, f"/manage/{public_id}/polls/{first}/publish", {"voters": "1"})
    assert response.headers["location"] == f"/manage/{public_id}"
    assert command() is None

    # Shown on the screen instead: gone too; the finished poll in Polls posts to itself.
    second = world.poll(owner, public_id, RESOLUTION)
    post(admin, f"/manage/{public_id}/polls/{second}/close")
    assert command() is not None and f"/polls/{second}/publish" in (command() or "")
    response = post(admin, f"/manage/{public_id}/polls/{second}/screen", {"shown": "1"})
    assert response.headers["location"] == f"/manage/{public_id}"
    assert command() is None
    votes = rubrics(page_of(admin, public_id))["tab-polls"]
    assert f'action="/manage/{public_id}/polls/{first}/screen#poll-{first}"' in votes

    # Only the poll closed last: an older one left unpublished does not come back.
    third = world.poll(owner, public_id, RESOLUTION)
    post(admin, f"/manage/{public_id}/polls/{third}/close")
    post(admin, f"/manage/{public_id}/polls/{third}/cancel", {"reason": "Erreur"})
    assert command() is None


def test_steering_commands_on_a_phone_and_a_computer_share_one_page(
    admin: TestClient, world: World, owner: User
) -> None:
    """The commands and their cards live on one page: two copies of the chronometer and of the
    notes, each with ids of its own, one script tag each."""
    public_id = session_in(world, owner, "in_session")
    html = page_of(admin, public_id)
    assert html.count('data-timer-url="') == 2 and html.count('data-notes-url="') == 2
    assert html.count("/static/js/timer.js") == 1 and html.count("/static/js/notes.js") == 1
    assert 'id="cmd-note-text"' in html and 'id="note-text"' in html
    assert 'data-notes-draft="agaura-note-draft:' + public_id + ':cmd"' in html
    assert f'action="/manage/{public_id}/notes#cmd-notes"' in html
    assert f'action="/manage/{public_id}/notes#notes"' in html
    assert f'action="/manage/{public_id}/timer/start#cmd-timer"' in html
    assert f'action="/manage/{public_id}/timer/start#timer"' in html
    assert f'action="/manage/{public_id}/headcount#cmd-presence"' in html
    assert f'action="/manage/{public_id}/headcount#presence"' in html
    # The status bar's running chronometer leads to the command during the session.
    post(admin, f"/manage/{public_id}/timer/start", {"mode": "up", "show": "1"})
    status = admin.get(f"/manage/{public_id}/panel").text.split('data-live-part="polls"')[0]
    assert 'href="#cmd-timer"' in status
    # The script's answers carry both copies.
    alone = admin.get(f"/manage/{public_id}/timer").text
    assert 'id="timer"' in alone and 'id="cmd-timer"' in alone
    notes = admin.get(f"/manage/{public_id}/notes").text
    assert 'id="notes"' in notes and 'id="cmd-notes"' in notes and "data-notes-last" in notes


def test_the_trial_vote_comes_first_among_the_drafts(
    admin: TestClient, world: World, owner: User
) -> None:
    """The checklist says to run it first: created after other drafts, it takes the first place,
    and "Prochain vote" offers it once the session is open."""
    public_id = session_in(world, owner, "preparation")
    world.poll(owner, public_id, open_it=False)
    world.poll(owner, public_id, PollInput("Lieu", "single_choice", ["A", "B"]), open_it=False)
    post(admin, f"/manage/{public_id}/polls/trial")
    with world.database.read() as db:
        drafts = polls.list_polls(db, owner, public_id)
    assert [poll.title for poll in drafts] == [
        "Tout le monde est-il connecté\u00a0?",
        "Approbation des comptes",
        "Lieu",
    ]
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
    ready = card_of(page_of(admin, public_id), "ready")
    assert "Tout le monde est-il connecté\u00a0?" in text_of(ready)


def tile_summary(html: str, tile: str) -> str:
    """The summary of a poll's tile in Polls, the details element ``tile`` folded."""
    match = re.search(
        rf'<details class="{tile}">\s*(<summary class="poll-tile-head">.*?</summary>)', html, re.S
    )
    assert match, tile
    return text_of(match.group(1))


def test_every_poll_of_polls_is_a_tile_folded_by_default(
    admin: TestClient, world: World, owner: User
) -> None:
    """Drafts, polls open now and finished polls: a tile each, folded on every load, its title
    and state in view; the rest, actions included, once unfolded. Steering keeps its commands at
    one click."""
    public_id = world.assembly(owner)
    draft = world.poll(
        owner, public_id, dataclasses.replace(RESOLUTION, title="Budget 2027"), open_it=False
    )
    opened = world.poll(owner, public_id, dataclasses.replace(RESOLUTION, title="Cotisation"))
    finished = world.poll(owner, public_id, dataclasses.replace(RESOLUTION, title="Rapport moral"))
    with world.database.write() as db:
        polls.close_poll(db, owner, finished)
    html = page_of(admin, public_id)
    votes = html[html.index('id="tab-polls"') :]
    assert tile_summary(votes, f'poll-item poll-tile" id="draft-{draft}') == "Budget 2027 Brouillon"
    assert (
        tile_summary(votes, f'open-poll poll-tile" id="open-poll-{opened}') == "Cotisation En cours"
    )
    assert tile_summary(votes, f'finished-poll poll-tile" id="poll-{finished}') == (
        "N°\u00a02 Rapport moral Clos"  # numbered in the order polls were pushed
    )
    assert not re.search(r'<details class="[^"]*poll-tile"[^>]*\sopen', votes)  # all folded
    # Inside the folded tiles: the actions, there once unfolded.
    assert f'/polls/{draft}/open"' in votes and f'/polls/{opened}/close"' in votes
    # Steering: the poll open now at one click, no tile.
    cockpit = card_of(html, "live-panel")
    assert '<li class="open-poll">' in cockpit and "poll-tile" not in cockpit
