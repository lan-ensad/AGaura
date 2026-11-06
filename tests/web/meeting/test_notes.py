"""Console card "Notes de séance": forms without JavaScript, answers to notes.js, the card alone,
isolation, and notes never shown outside the console and the minutes."""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agaura.models import User
from agaura.services import access, assemblies, meeting, notes
from agaura.web.realtime import manage_channel
from tests.conftest import World
from tests.web.conftest import log_in, make_account, post

FETCH = {"X-Requested-With": "fetch"}


def card(html: str) -> str:
    match = re.search(
        r'<section class="card meeting notes-card" id="notes".*?</section>', html, re.S
    )
    assert match
    return match.group(0)


def text_of(html: str) -> str:
    text = " ".join(re.findall(r"[^ \t\n\r\f\v]+", unescape(re.sub(r"<[^>]+>", " ", html))))
    # A tag before a no-break space ("</strong>\u00a0:") leaves no gap in the page.
    return re.sub(r" ?\u00a0 ?", "\u00a0", text)


def console(client: TestClient, public_id: str) -> str:
    response = client.get(f"/manage/{public_id}")
    assert response.status_code == 200
    return response.text


def stored(world: World, owner: User, public_id: str) -> list[tuple[str, bool, int | None]]:
    with world.database.read() as db:
        return [
            (note.text, note.internal, note.item_position)
            for note in notes.list_notes(db, owner, public_id)
        ]


def note_ids(world: World, owner: User, public_id: str) -> list[int]:
    with world.database.read() as db:
        return [note.id for note in notes.list_notes(db, owner, public_id)]


def assert_redirect(response: Response, location: str) -> None:
    assert response.status_code == 303, response.text
    assert response.headers["location"] == location


@pytest.fixture
def public_id(world: World, owner: User) -> str:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Rapport moral"])
        meeting.set_current_item(db, owner, public_id, 2)
    return public_id


def test_empty_card(admin: TestClient, public_id: str) -> None:
    notes_card = card(console(admin, public_id))
    assert "Notes de séance" in notes_card and "Aucune note pour l'instant." in notes_card
    assert "Rattachée au point en cours\u00a0: 2. Rapport moral" in notes_card
    assert f'action="/manage/{public_id}/notes#notes"' in notes_card
    assert 'name="text"' in notes_card and 'maxlength="5000"' in notes_card
    assert 'name="internal" value="1"' in notes_card and ">Ajouter</button>" in notes_card


def test_add_without_javascript(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    published: list[tuple] = []
    original = admin.app.state.hub.publish  # type: ignore[attr-defined]
    admin.app.state.hub.publish = lambda *args: published.append(args)  # type: ignore[attr-defined]
    try:
        response = post(
            admin, f"/manage/{public_id}/notes", {"text": "Rapport adopté\r\nsans débat."}
        )
    finally:
        admin.app.state.hub.publish = original  # type: ignore[attr-defined]
    # No anchor: the card (#notes) and the command of Steering (#cmd-notes) post to their own
    # address, which the browser keeps.
    assert_redirect(response, f"/manage/{public_id}")
    assert published == [(manage_channel(public_id), "turnout", "console")]
    assert stored(world, owner, public_id) == [("Rapport adopté\nsans débat.", False, 2)]
    html = console(admin, public_id)
    assert "Note ajoutée." in html
    notes_card = card(html)
    assert '<p class="note-text">Rapport adopté\nsans débat.</p>' in notes_card
    assert re.search(
        r"<time datetime=\"[^\"]+\">\d\d:\d\d</time>\s*· 2\. Rapport moral", notes_card
    )


def test_add_from_the_script_answers_with_the_card(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    post(admin, f"/manage/{public_id}/notes", {"text": "Première"})
    response = post(
        admin,
        f"/manage/{public_id}/notes",
        {"text": "Interne", "internal": "1"},
        headers=FETCH,
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.text.lstrip().startswith("<section")
    notes_card = card(response.text)
    # Most recent first, internal marked.
    assert notes_card.index("Interne</p>") < notes_card.index("Première</p>")
    assert '<span class="badge warning">Interne</span>' in notes_card
    assert stored(world, owner, public_id)[1] == ("Interne", True, 2)
    # The flash notice was not consumed by the answer.
    assert "Note ajoutée." in console(admin, public_id)


def test_errors(admin: TestClient, world: World, owner: User, public_id: str) -> None:
    response = post(admin, f"/manage/{public_id}/notes", {"text": "  "}, headers=FETCH)
    assert response.status_code == 400
    assert response.text == "Écrivez la note avant de l'ajouter."
    response = post(admin, f"/manage/{public_id}/notes", {"text": "x" * 5001})
    assert_redirect(response, f"/manage/{public_id}")
    assert "Une note compte au plus 5 000 caractères." in text_of(console(admin, public_id))
    assert stored(world, owner, public_id) == []


def test_card_alone(admin: TestClient, world: World, owner: User, public_id: str) -> None:
    with world.database.write() as db:
        notes.add_note(db, owner, public_id, "Vue par une autre console")
    response = admin.get(f"/manage/{public_id}/notes")
    assert response.status_code == 200
    assert "Vue par une autre console" in response.text
    assert "<html" not in response.text and response.headers["cache-control"] == "no-store"


def test_edit_and_delete(admin: TestClient, world: World, owner: User, public_id: str) -> None:
    post(admin, f"/manage/{public_id}/notes", {"text": "Brouillon"})
    (note_id,) = note_ids(world, owner, public_id)
    response = post(
        admin,
        f"/manage/{public_id}/notes/edit",
        {"note": str(note_id), "text": "Corrigée", "internal": "1", "item": "1"},
    )
    assert_redirect(response, f"/manage/{public_id}#note-{note_id}")
    assert stored(world, owner, public_id) == [("Corrigée", True, 1)]
    notes_card = card(console(admin, public_id))
    assert "1. Ouverture" in notes_card and "· modifiée" in notes_card
    assert re.search(r'<option value="1" selected>1\. Ouverture</option>', notes_card)
    response = post(
        admin, f"/manage/{public_id}/notes/edit", {"note": str(note_id), "text": "Hors", "item": ""}
    )
    assert stored(world, owner, public_id) == [("Hors", False, None)]
    response = post(
        admin, f"/manage/{public_id}/notes/delete", {"note": str(note_id)}, headers=FETCH
    )
    assert response.status_code == 200 and "Aucune note pour l'instant." in response.text
    assert stored(world, owner, public_id) == []


@pytest.mark.parametrize("path", ["edit", "delete"])
def test_unknown_or_foreign_note_is_not_found(
    admin: TestClient, world: World, owner: User, public_id: str, path: str
) -> None:
    other = world.assembly(owner)
    with world.database.write() as db:
        foreign = notes.add_note(db, owner, other, "Autre session").id
    for note in (str(foreign), "abc", "", "9" * 12):
        data = {"note": note, "text": "x"}
        assert post(admin, f"/manage/{public_id}/notes/{path}", data).status_code == 404
        assert (
            post(admin, f"/manage/{public_id}/notes/{path}", data, headers=FETCH).status_code == 404
        )
    assert stored(world, owner, other) == [("Autre session", False, None)]


def test_another_account_gets_404(
    client: TestClient, world: World, owner: User, public_id: str
) -> None:
    with world.database.write() as db:
        note_id = notes.add_note(db, owner, public_id, "Secret").id
    make_account(world, "intruder@example.org")
    log_in(client, "intruder@example.org")
    assert client.get(f"/manage/{public_id}/notes").status_code == 404
    for path, data in (
        ("notes", {"text": "x"}),
        ("notes/edit", {"note": str(note_id), "text": "x"}),
        ("notes/delete", {"note": str(note_id)}),
    ):
        assert post(client, f"/manage/{public_id}/{path}", data).status_code == 404
    assert stored(world, owner, public_id) == [("Secret", False, 2)]


def test_editable_after_closing(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    post(admin, f"/manage/{public_id}/notes", {"text": "Ajout au procès-verbal"})
    assert stored(world, owner, public_id) == [("Ajout au procès-verbal", False, 2)]
    html = console(admin, public_id)
    assert "Note ajoutée après la clôture" in text_of(html)
    assert "Ajout au procès-verbal" not in re.search(r'id="log">.*?</details>', html, re.S).group(0)


def test_never_shown_to_voters_screen_or_public_page(
    client: TestClient, world: World, owner: User, public_id: str
) -> None:
    secret = "Note confidentielle de la tribune"
    with world.database.write() as db:
        notes.add_note(db, owner, public_id, secret)
        notes.add_note(db, owner, public_id, secret + " interne", internal=True)
        access.set_public_results(db, world.keyring, owner, public_id, True)
        screen = access.screen_token(db, world.keyring, owner, public_id)
        results = access.public_results_token(db, world.keyring, owner, public_id)
    password = world.password(owner, public_id)
    world.join(public_id, password)
    pages = [
        f"/{public_id}",
        f"/{public_id}/screen/{screen}",
        f"/{public_id}/screen/{screen}/state",
        f"/{public_id}/results/{results}",
    ]
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        assert "confidentielle" not in response.text, page


def test_unknown_agenda_item_is_refused(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    post(admin, f"/manage/{public_id}/notes", {"text": "Note"})
    (note_id,) = note_ids(world, owner, public_id)
    for item in ("abc", "7"):
        data = {"note": str(note_id), "text": "Changée", "item": item}
        response = post(admin, f"/manage/{public_id}/notes/edit", data, headers=FETCH)
        assert response.status_code == 400
        assert "n'existe plus" in response.text
    assert stored(world, owner, public_id) == [("Note", False, 2)]
