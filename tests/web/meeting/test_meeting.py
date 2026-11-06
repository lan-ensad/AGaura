"""Console cards of the tribune and the agenda: forms without JavaScript, values kept on error,
the agenda saved in place for its script, the choice of the item under discussion, real-time
notification of the projection screen and the other consoles."""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from agaura.models import AuditEntry, User
from agaura.services import assemblies, meeting
from agaura.services.meeting import Member
from agaura.web.realtime import manage_channel
from tests.conftest import World
from tests.web.conftest import log_in, make_account, post


def console(client: TestClient, public_id: str) -> str:
    response = client.get(f"/manage/{public_id}")
    assert response.status_code == 200, response.text
    return response.text


def card(html: str, card_id: str) -> str:
    """The HTML of one console card (``<section id=…>``)."""
    match = re.search(rf'<section class="card meeting" id="{card_id}".*?</section>', html, re.S)
    assert match, card_id
    return match.group(0)


def text_of(html: str) -> str:
    text = " ".join(re.findall(r"[^ \t\n\r\f\v]+", unescape(re.sub(r"<[^>]+>", " ", html))))
    # A tag before a no-break space ("</strong>\u00a0:") leaves no gap in the page.
    return re.sub(r" ?\u00a0 ?", "\u00a0", text)


def values(html: str, name: str) -> list[str]:
    """Values of the form's inputs called ``name`` (the row ``<template>`` left out)."""
    form = re.sub(r"<template.*?</template>", "", html, flags=re.S)
    pattern = rf'<input [^>]*name="{name}" value="([^"]*)"'
    return [unescape(value) for value in re.findall(pattern, form)]


ADD = 'aria-label="Ajouter une personne"'


def stored(world: World, owner: User, public_id: str) -> tuple[list, list[str], int | None]:
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        return assembly.tribune, meeting.agenda_titles(assembly), assembly.agenda_current


def tribune_form(rows: list[tuple[str, str]], **extra: str) -> dict:
    return {"name": [name for name, _ in rows], "role": [role for _, role in rows], **extra}


def assert_redirect(response: Response, public_id: str, anchor: str) -> None:
    """``anchor`` empty: no fragment, the browser keeps the one of the form's address."""
    assert response.status_code == 303, response.text
    fragment = f"#{anchor}" if anchor else ""
    assert response.headers["location"] == f"/manage/{public_id}{fragment}"


@pytest.fixture
def public_id(world: World, owner: User) -> str:
    return world.assembly(owner)


# --- Tribune ------------------------------------------------------------------------------------


def test_empty_tribune_offers_one_row(admin: TestClient, public_id: str) -> None:
    html = console(admin, public_id)
    tribune = card(html, "tribune")
    assert "Personne à la tribune pour l'instant." in text_of(tribune)
    assert '<details class="meeting-edit" id="tribune-edit" open>' in tribune
    assert values(tribune, "name") == [""] and values(tribune, "role") == [""]
    assert "placeholder" not in tribune  # no suggested function
    assert ADD in tribune and 'name="more" value="1"' in tribune
    # The "+" button comes after "Save": Enter in a field saves, it never adds a row.
    assert tribune.index("Enregistrer la tribune") < tribune.index(ADD)
    # The row added in place by meeting.js, numbered by the script.
    template = r'<template id="tribune-row-template"><fieldset.*?id="tribune-name-__N__"'
    assert re.search(template, tribune, re.S)
    assert '<script src="/static/js/meeting.js?v=' in html
    assert 'action="/manage/' + public_id + '/tribune#tribune"' in tribune
    for word in ("administrat", "organisat", "admin"):
        assert word not in text_of(tribune).lower()


def test_save_tribune_ignores_empty_rows(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    published: list[tuple] = []
    original = admin.app.state.hub.publish  # type: ignore[attr-defined]
    admin.app.state.hub.publish = lambda *args: published.append(args)  # type: ignore[attr-defined]
    rows = [("Marie Dupont", "Présidente de séance"), ("", ""), (" Paul Martin ", "Secrétaire")]
    try:
        response = post(admin, f"/manage/{public_id}/tribune", tribune_form(rows))
    finally:
        admin.app.state.hub.publish = original  # type: ignore[attr-defined]
    assert_redirect(response, public_id, "tribune")
    assert published == [(manage_channel(public_id), "turnout", "console")]  # screen refreshes
    assert stored(world, owner, public_id)[0] == [
        {"name": "Marie Dupont", "role": "Présidente de séance"},
        {"name": "Paul Martin", "role": "Secrétaire"},
    ]
    html = console(admin, public_id)
    assert "Tribune enregistrée." in html
    tribune = card(html, "tribune")
    assert "Marie Dupont — Présidente de séance" in text_of(tribune)
    assert '<details class="meeting-edit" id="tribune-edit">' in tribune  # folded once filled
    # The members' rows only: the "+" button adds one.
    assert values(tribune, "name") == ["Marie Dupont", "Paul Martin"]
    assert "Tribune modifiée" in text_of(card_log(html))


def card_log(html: str) -> str:
    match = re.search(r'<details class="card" id="log">.*?</details>', html, re.S)
    assert match
    return match.group(0)


def test_tribune_error_keeps_the_values(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    rows = [("Marie Dupont", "Présidente"), ("", ""), ("", "Secrétaire"), ("", "")]
    response = post(admin, f"/manage/{public_id}/tribune", tribune_form(rows))
    assert response.status_code == 400
    tribune = card(response.text, "tribune")
    assert '<details class="meeting-edit" id="tribune-edit" open>' in tribune
    assert "La tribune n'a pas été enregistrée" in text_of(tribune)
    # Filled rows first (the faulty one is row 2), as many rows as sent.
    assert values(tribune, "name") == ["Marie Dupont", "", "", ""]
    assert values(tribune, "role") == ["Présidente", "Secrétaire", "", ""]
    assert 'id="tribune-error-2-name"' in tribune
    assert 'aria-invalid="true" aria-describedby="tribune-error-2-name"' in tribune
    assert "Indiquez le nom de cette personne, ou videz sa fonction." in text_of(tribune)
    assert stored(world, owner, public_id)[0] == []


def test_too_many_people_and_too_long_function(admin: TestClient, public_id: str) -> None:
    rows = [(f"Personne {n}", "") for n in range(20)] + [("Léa", "x" * 101)]
    response = post(admin, f"/manage/{public_id}/tribune", tribune_form(rows))
    assert response.status_code == 400
    tribune = card(response.text, "tribune")
    assert "La tribune compte au plus 20 personnes." in text_of(tribune)
    assert 'id="tribune-error-21-role"' in tribune
    assert len(values(tribune, "name")) == 21  # nothing lost
    assert ADD not in tribune


def test_add_a_row_without_javascript(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    rows = [("Marie Dupont", "Présidente"), ("Paul", ""), ("", ""), ("Léa", "Scrutatrice")]
    response = post(admin, f"/manage/{public_id}/tribune", tribune_form(rows, more="1"))
    assert response.status_code == 200
    tribune = card(response.text, "tribune")
    # Filled rows first, the empty row sent kept, plus one.
    assert values(tribune, "name") == ["Marie Dupont", "Paul", "Léa", "", ""]
    assert values(tribune, "role") == ["Présidente", "", "Scrutatrice", "", ""]
    assert '<details class="meeting-edit" id="tribune-edit" open>' in tribune
    assert "La tribune n'a pas été enregistrée" not in tribune
    assert stored(world, owner, public_id)[0] == []  # nothing saved yet
    response = post(admin, f"/manage/{public_id}/tribune", tribune_form([("", "")], more="1"))
    assert values(card(response.text, "tribune"), "name") == ["", ""]


def test_rows_never_exceed_the_limit(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    with world.database.write() as db:
        meeting.set_tribune(db, owner, public_id, [Member(f"P{n}") for n in range(19)])
    tribune = card(console(admin, public_id), "tribune")
    assert len(values(tribune, "name")) == 19 and ADD in tribune
    assert 'data-max="20"' in tribune
    rows = [(f"P{n}", "") for n in range(19)] + [("", "")]
    response = post(admin, f"/manage/{public_id}/tribune", tribune_form(rows, more="1"))
    tribune = card(response.text, "tribune")
    assert len(values(tribune, "name")) == 20 and ADD not in tribune


def test_tribune_editable_after_closing(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    response = post(admin, f"/manage/{public_id}/tribune", tribune_form([("Marie Dupond", "")]))
    assert_redirect(response, public_id, "tribune")
    assert stored(world, owner, public_id)[0] == [{"name": "Marie Dupond", "role": ""}]


def test_another_account_cannot_touch_the_tribune(
    client: TestClient, world: World, owner: User, public_id: str
) -> None:
    make_account(world, "intruder@example.org")
    log_in(client, "intruder@example.org")
    for path, data in (
        ("tribune", tribune_form([("X", "")])),
        ("agenda", {"items": "X"}),
        ("agenda/current", {"item": "none"}),
    ):
        assert post(client, f"/manage/{public_id}/{path}", data).status_code == 404
    assert stored(world, owner, public_id) == ([], [], None)


# --- Agenda -------------------------------------------------------------------------------------

FETCH = {"X-Requested-With": "fetch"}  # as agenda.js sends the text


def text_area(html: str) -> str:
    """The text of the agenda card's text area."""
    match = re.search(r'<textarea id="agenda-items"[^>]*>(.*?)</textarea>', html, re.S)
    assert match
    return unescape(match.group(1))


def chosen(html: str) -> list[tuple[str, str]]:
    """The choice of the item under discussion: (value, label) of each option, the selected one
    marked with "*"."""
    options = re.findall(r'<option value="([^"]*)"( selected)?>(.*?)</option>', html)
    return [
        (value + ("*" if selected else ""), unescape(label)) for value, selected, label in options
    ]


def agenda_entries(world: World) -> list[dict]:
    with world.database.read() as db:
        entries = db.scalars(select(AuditEntry).where(AuditEntry.action == "meeting.agenda_set"))
        return [entry.details for entry in entries]


def test_empty_agenda_is_a_text_area(admin: TestClient, public_id: str) -> None:
    """One text area edited in place, nothing folded, no list of buttons: the card is small."""
    html = console(admin, public_id)
    agenda = card(html, "agenda")
    assert text_area(agenda) == ""
    assert "<details" not in agenda.split('data-hand-over="agenda"')[0]  # only "Confier" folds
    assert 'class="visually-hidden">Points de l\'ordre du jour</label>' in agenda
    assert 'aria-describedby="agenda-hint"' in agenda and "aria-invalid" not in agenda
    assert (
        '<p class="hint" id="agenda-hint">Un point par ligne, numéroté automatiquement.</p>'
        in agenda
    )
    # One button saves; the primary one while the agenda is empty (R12).
    assert (
        '<button type="submit" class="button primary">Enregistrer l\'ordre du jour</button>'
        in agenda
    )
    assert "agenda/current" not in agenda and "<select" not in agenda  # no item to choose yet
    assert f'action="/manage/{public_id}/agenda#agenda"' in agenda
    assert f'data-agenda-url="/manage/{public_id}/agenda"' in agenda
    assert '<script src="/static/js/agenda.js?v=' in html


def test_save_agenda_strips_numbering_and_blank_lines(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    text = "1. Approbation du PV\r\n\r\n- Rapport moral\n2) Rapport - financier\n   \n"
    response = post(admin, f"/manage/{public_id}/agenda", {"items": text})
    assert_redirect(response, public_id, "agenda")
    assert stored(world, owner, public_id)[1:] == (
        ["Approbation du PV", "Rapport moral", "Rapport - financier"],
        None,
    )
    html = console(admin, public_id)
    assert "Ordre du jour enregistré." in html
    agenda = card(html, "agenda")
    saved = "Approbation du PV\nRapport moral\nRapport - financier"
    assert text_area(agenda) == saved
    assert f'data-saved="{saved}"' in agenda  # what agenda.js compares the text with
    assert '<button type="submit" class="button">Enregistrer l\'ordre du jour</button>' in agenda
    assert chosen(agenda) == [
        ("none*", "Aucun point en cours"),
        ("1", "1. Approbation du PV"),
        ("2", "2. Rapport moral"),
        ("3", "3. Rapport - financier"),
    ]
    assert "Ordre du jour modifié" in text_of(card_log(html))


def test_agenda_errors_keep_the_text_on_the_field(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    text = "Ouverture\n" + "x" * 201
    response = post(admin, f"/manage/{public_id}/agenda", {"items": text})
    assert response.status_code == 400
    agenda = card(response.text, "agenda")
    assert text_area(agenda) == text
    assert 'aria-describedby="agenda-hint agenda-errors" aria-invalid="true" autofocus>' in agenda
    errors = re.search(
        r'<div class="agenda-errors" id="agenda-errors" role="alert">.*?</div>', agenda
    )
    assert errors
    assert text_of(errors.group(0)) == "Le point 2 dépasse 200 caractères\u00a0: raccourcissez-le."
    too_many = "\n".join(f"Point {n}" for n in range(51))
    response = post(admin, f"/manage/{public_id}/agenda", {"items": too_many})
    assert response.status_code == 400
    assert "L'ordre du jour compte au plus 50 points." in text_of(card(response.text, "agenda"))
    assert stored(world, owner, public_id)[1] == []
    # Blank lines and spaces around an item are ignored: 50 items fit, whatever the padding.
    padded = "\n\n".join(f"  Point {n}  " for n in range(50))
    assert_redirect(
        post(admin, f"/manage/{public_id}/agenda", {"items": padded}), public_id, "agenda"
    )
    assert stored(world, owner, public_id)[1] == [f"Point {n}" for n in range(50)]


def test_the_script_saves_in_place(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    """agenda.js gets the card alone: the agenda as saved, or the text refused with its
    messages; the console is told either way it changed, never through the flash cookie."""
    published: list[tuple] = []
    original = admin.app.state.hub.publish  # type: ignore[attr-defined]
    admin.app.state.hub.publish = lambda *args: published.append(args)  # type: ignore[attr-defined]
    url = f"/manage/{public_id}/agenda"
    try:
        response = post(admin, url, {"items": "1. Accueil\n\n Rapport moral "}, headers=FETCH)
        assert response.status_code == 200
        assert published == [(manage_channel(public_id), "turnout", "console")]
        refused = post(admin, url, {"items": "Accueil\n" + "x" * 201}, headers=FETCH)
        assert published == [(manage_channel(public_id), "turnout", "console")]  # once
    finally:
        admin.app.state.hub.publish = original  # type: ignore[attr-defined]
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers  # no notice waits for the next page
    alone = card(response.text, "agenda")
    assert text_area(alone) == "Accueil\nRapport moral"
    assert 'role="status" data-agenda-saved>Ordre du jour enregistré.</span>' in alone
    assert "<script" not in response.text  # the page has its script already
    assert stored(world, owner, public_id)[1] == ["Accueil", "Rapport moral"]

    assert refused.status_code == 400 and "set-cookie" not in refused.headers
    alone = card(refused.text, "agenda")
    assert text_area(alone) == "Accueil\n" + "x" * 201
    assert 'data-saved="Accueil\nRapport moral"' in alone and 'aria-invalid="true"' in alone
    assert "Le point 2 dépasse 200 caractères" in text_of(alone)
    assert "data-agenda-saved></span>" in alone
    assert stored(world, owner, public_id)[1] == ["Accueil", "Rapport moral"]

    # Saved again unchanged (the button pressed with nothing new): nothing logged.
    assert post(admin, url, {"items": "Accueil\nRapport moral\n"}, headers=FETCH).status_code == 200
    assert agenda_entries(world) == [{"count": 2}]

    # The card alone, for agenda.js to follow the other consoles: the flash notice stays.
    post(admin, f"/manage/{public_id}/tribune", tribune_form([("Marie Dupont", "")]))
    response = admin.get(url)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert text_area(card(response.text, "agenda")) == "Accueil\nRapport moral"
    assert "data-agenda-saved></span>" in response.text
    assert "Tribune enregistrée." in console(admin, public_id)


def test_choose_the_item_under_discussion_in_the_card(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    """The card's light control: one list, none or any item; "Previous" and "Next" sit in
    Steering, on the same route."""
    post(admin, f"/manage/{public_id}/agenda", {"items": "A\nB\nC"})
    agenda = card(console(admin, public_id), "agenda")
    form = re.search(r'<form [^>]*action="([^"]+)" class="agenda-current" data-live-form>', agenda)
    assert form and form.group(1) == f"/manage/{public_id}/agenda/current#agenda"
    assert '<label for="agenda-current-item">Point en cours</label>' in agenda
    assert '<button type="submit" class="button">Changer</button>' in agenda
    assert "primary" not in agenda.split('class="agenda-current"')[1]
    url = f"/manage/{public_id}/agenda/current"

    def control(item: str, seen: str = "") -> int | None:
        # The agenda card posts to …#agenda, the cockpit to …#tab-cockpit: no anchor here.
        assert_redirect(post(admin, url, {"item": item, "seen": seen}), public_id, "")
        return stored(world, owner, public_id)[2]

    assert control("2") == 2
    assert chosen(card(console(admin, public_id), "agenda"))[:3] == [
        ("none", "Aucun point en cours"),
        ("1", "1. A"),
        ("2*", "2. B"),
    ]
    assert control("2") == 2  # the same item again: nothing logged
    assert control("none") is None
    assert chosen(card(console(admin, public_id), "agenda"))[0] == ("none*", "Aucun point en cours")
    # Steering's moves, with the double tap guard.
    assert control("next") == 1
    assert control("next", "1") == 2
    assert control("next", "1") == 2  # second tap of a double tap: no skip
    assert control("3", "2") == 3
    assert control("previous", "3") == 2
    # Answered in place to manage.js, which then fetches the live parts and the card.
    response = post(admin, url, {"item": "3"}, headers=FETCH)
    assert response.status_code == 204 and stored(world, owner, public_id)[2] == 3
    log = text_of(card_log(console(admin, public_id)))
    assert "Point en cours de l'ordre du jour changé" in log


def test_the_item_under_discussion_follows_an_edit(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    """Its title, wherever it moved; else its place, for a title corrected in place; else
    none."""
    url = f"/manage/{public_id}/agenda"
    post(admin, url, {"items": "Accueil\nRapport moral\nBudget"})
    post(admin, f"{url}/current", {"item": "2"})
    post(admin, url, {"items": "Accueil\nPrésentation\nRapport moral\nBudget"})
    assert stored(world, owner, public_id)[2] == 3
    post(admin, url, {"items": "Accueil\nPrésentation\nRapport moral 2026\nBudget"})
    assert stored(world, owner, public_id)[2] == 3
    agenda = card(console(admin, public_id), "agenda")
    assert ("3*", "3. Rapport moral 2026") in chosen(agenda)
    post(admin, url, {"items": "Accueil"}, headers=FETCH)
    assert stored(world, owner, public_id)[1:] == (["Accueil"], None)


@pytest.mark.parametrize(
    ("item", "seen"), [("7", ""), ("0", ""), ("abc", ""), ("", ""), ("previous", ""), ("9" * 9, "")]
)
def test_invalid_items_are_refused(
    admin: TestClient, world: World, owner: User, public_id: str, item: str, seen: str
) -> None:
    post(admin, f"/manage/{public_id}/agenda", {"items": "A\nB"})
    response = post(admin, f"/manage/{public_id}/agenda/current", {"item": item, "seen": seen})
    assert_redirect(response, public_id, "")
    assert "Ce point de l'ordre du jour n'existe plus" in text_of(console(admin, public_id))
    assert stored(world, owner, public_id)[2] is None


def test_closed_session_shows_the_agenda_read_only(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    post(admin, f"/manage/{public_id}/agenda", {"items": "A\nB"})
    post(admin, f"/manage/{public_id}/agenda/current", {"item": "2", "seen": ""})
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    html = console(admin, public_id)
    agenda = card(html, "agenda")
    assert "agenda/current" not in agenda and 'name="items"' not in agenda
    assert re.search(r'<ol class="agenda-read">\s*<li>A</li><li>B</li>\s*</ol>', agenda)
    assert "La session est terminée\u00a0: l'ordre du jour ne peut plus changer." in text_of(agenda)
    assert "agenda.js" not in html
    response = post(admin, f"/manage/{public_id}/agenda", {"items": "Autre"})
    assert_redirect(response, public_id, "agenda")
    assert "La session est terminée." in console(admin, public_id)
    assert stored(world, owner, public_id)[1:] == (["A", "B"], 2)
    # An invalid agenda posted from a stale page still gets a message.
    response = post(admin, f"/manage/{public_id}/agenda", {"items": "x" * 201})
    assert response.status_code == 400
    assert "Le point 1 dépasse 200 caractères" in text_of(card(response.text, "agenda"))
    # The same from a stale card with its script: the card alone, read only, says why.
    response = post(admin, f"/manage/{public_id}/agenda", {"items": "Autre"}, headers=FETCH)
    assert response.status_code == 400
    assert "La session est terminée." in text_of(card(response.text, "agenda"))
    assert stored(world, owner, public_id)[1] == ["A", "B"]


def test_closed_session_without_agenda_hides_the_card(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    html = console(admin, public_id)
    assert 'id="agenda"' not in html and 'id="tribune"' in html
