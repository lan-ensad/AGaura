"""Attendance record at the foot of the headcount card of the console (FR-28)."""

from __future__ import annotations

import re
from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy import select

from agaura.models import Assembly, User
from agaura.services import audit
from tests.conftest import World
from tests.web.conftest import post


def presence_card(client: TestClient, public_id: str) -> str:
    response = client.get(f"/manage/{public_id}")
    assert response.status_code == 200
    pattern = r'<section class="card presence-card" id="presence".*?</section>'
    match = re.search(pattern, response.text, re.S)
    assert match
    return match.group(0)


def table_rows(html: str) -> list[list[str]]:
    body = re.search(r'<table class="data attendance-table">.*?<tbody>(.*?)</tbody>', html, re.S)
    assert body
    return [
        [unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in re.findall(r"<td.*?</td>", row)]
        for row in re.findall(r"<tr>.*?</tr>", body.group(1), re.S)
    ]


def test_empty_record_under_the_headcount(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    page = admin.get(f"/manage/{public_id}").text
    # The record sits under the headcount form, in its card; the access mode card follows.
    card = presence_card(admin, public_id)
    assert card.index('name="headcount"') < card.index('id="attendance"')
    access = page.index('id="access"')  # the chronometer of Meeting has a name="mode" too
    assert page.index('id="presence"') < access < page.index('name="mode"', access)
    assert "Suivi des présences" in card and "Aucun relevé pour l'instant." in card
    assert "jamais à l'arrivée ni au vote d'une personne" in card


def test_readings_most_recent_first(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    world.voters(owner, public_id, 2)
    response = post(admin, f"/manage/{public_id}/headcount", {"headcount": "30"})
    assert response.status_code == 303  # never blocks
    world.poll(owner, public_id)
    world.voters(owner, public_id, 1)
    response = admin.get(f"/manage/{public_id}")
    assert "Effectif enregistré" in response.text
    card = presence_card(admin, public_id)
    assert "(3 relevés)" in card
    rows = table_rows(card)
    assert [row[1:] for row in rows] == [
        ["Vote n° 1 lancé", "30", "2"],
        ["Effectif saisi", "30", "2"],
        ["Session ouverte", "\u2013", "0"],
    ]
    assert all(re.fullmatch(r"\d\d:\d\d", row[0]) for row in rows)


def test_older_entries_show_blanks(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        assembly = db.scalar(select(Assembly).where(Assembly.public_id == public_id))
        assert assembly is not None
        audit.record(db, "poll.opened", user_id=owner.id, assembly_id=assembly.id, details={})
    first = table_rows(presence_card(admin, public_id))[0]
    assert first[1:] == ["Vote n° ? lancé", "\u2013", "\u2013"]
