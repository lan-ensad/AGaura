"""Mes sessions (FR-10, FR-11, D-J): the sessions grouped by state, and the access mode of a new
session as two choice cards."""

from __future__ import annotations

import datetime as dt
import re

from fastapi.testclient import TestClient

from agaura.models import AccessMode, User
from agaura.services import assemblies
from tests.conftest import World
from tests.web.conftest import make_account, post
from tests.web.manage.test_layout import text_of


def groups(page: str) -> list[tuple[str, list[str]]]:
    """Each group of the list: its heading, and the session IDs of its rows, in order."""
    found = []
    for match in re.finditer(r'<section class="session-group".*?</section>', page, re.S):
        group = match.group(0)
        heading = re.search(r"<h3[^>]*>([^<]*)</h3>", group)
        assert heading is not None
        found.append((heading.group(1), re.findall(r'<a href="/manage/(\w+)">', group)))
    return found


def test_sessions_grouped_by_state(admin: TestClient, world: World, owner: User) -> None:
    prepared = world.assembly(
        owner, in_session=False, title="AG 2027", date=dt.date(2027, 6, 12), place="Médiathèque"
    )
    running = world.assembly(owner, title="AG extraordinaire")
    older = world.assembly(owner, title="AG 2025")
    newer = world.assembly(owner, title="AG 2026")
    for public_id in (older, newer):
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
    world.poll(owner, prepared, open_it=False)
    world.poll(owner, prepared, open_it=False)
    post(admin, f"/manage/{prepared}/polls/trial")  # the trial vote counts nowhere (D-I)
    page = admin.get("/dashboard").text
    assert groups(page) == [
        ("En cours", [running]),
        ("En préparation", [prepared]),
        ("Terminées", [newer, older]),  # most recent first
    ]
    row = re.search(
        rf'<li class="session-row">\s*<div class="session-text">\s*'
        rf'<a href="/manage/{prepared}">.*?</li>',
        page,
        re.S,
    )
    assert row is not None
    text = text_of(row.group(0))
    assert text == f"AG 2027 {prepared} 12 juin 2027 Médiathèque En préparation 2 brouillons"
    closed_row = re.search(rf'<a href="/manage/{newer}">.*?</li>', page, re.S)
    assert closed_row is not None and "0 vote · 0 présent" in text_of(closed_row.group(0))
    # One heading per page level: the list's own heading is for screen readers.
    assert '<h2 id="sessions-title" class="visually-hidden">Sessions</h2>' in page
    assert '<a class="button primary" href="#create">' in page


def test_no_session_yet(admin: TestClient) -> None:
    page = admin.get("/dashboard").text
    assert "Aucune session pour l'instant" in page and "session-group" not in page


def test_access_mode_as_two_cards(admin: TestClient, world: World) -> None:
    """Two real radios, the password checked by default; the codes card creates a codes
    session, and a refused form keeps the choice."""
    page = admin.get("/dashboard").text
    cards = re.findall(
        r'<label class="mode-card"><input type="radio" name="access_mode" '
        r'value="(\w+)" ?(checked)?>',
        page,
    )
    assert cards == [("password", "checked"), ("codes", "")]
    assert "Mot de passe de la session" in page and "Codes individuels imprimés" in page
    assert "Un vote par téléphone." in page and "Un vote par personne." in page
    response = post(admin, "/dashboard", {"title": "", "access_mode": "codes"})
    assert response.status_code == 400
    assert 'value="codes" checked>' in response.text
    response = post(admin, "/dashboard", {"title": "AG codes", "access_mode": "codes"})
    public_id = response.headers["location"].rsplit("/", 1)[1]
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None and assembly.access_mode is AccessMode.CODES


def test_only_the_account_sessions(admin: TestClient, world: World, owner: User) -> None:
    stranger = make_account(world, "stranger@example.org")
    foreign = world.assembly(stranger, title="AG d'un autre compte")
    mine = world.assembly(owner)
    page = admin.get("/dashboard").text
    assert foreign not in page and "AG d&#39;un autre compte" not in page and mine in page
