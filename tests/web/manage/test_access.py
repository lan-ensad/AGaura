"""Access and attendance rubric of the console: the card "Entrées" (lock and access cap, FR-27),
the attendance record inside the headcount card (FR-28), the access mode card (FR-20, FR-23)."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import AccessMode, User
from agaura.services import accounts, assemblies, polls
from tests.conftest import World
from tests.web.conftest import post
from tests.web.manage.test_delegates import card_of, make_link, open_link
from tests.web.manage.test_layout import rubrics, text_of


def access_rubric(client: TestClient, public_id: str) -> str:
    response = client.get(f"/manage/{public_id}")
    assert response.status_code == 200, response.text
    return rubrics(response.text)["tab-access"]


def delegate_access_rubric(client: TestClient, public_id: str) -> str:
    """The rubric on a delegate's console, which has only the rubrics of its rights."""
    page = client.get(f"/manage/{public_id}").text
    start = page.index('<section class="rubric" id="tab-access"')
    end = page.find('<section class="rubric"', start + 1)
    return page[start : end if end != -1 else page.index("</main>")]


def entries_card(client: TestClient, public_id: str) -> str:
    return card_of(access_rubric(client, public_id), "entries")


def test_cards_in_pairs(admin: TestClient, world: World, owner: User) -> None:
    """Joining and entries side by side, then the headcount (with its attendance record) and
    the access mode, then the codes (console-acces)."""
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    rubric = access_rubric(admin, public_id)
    ids = re.findall(r'<section class="card[^"]*" id="([\w-]+)"', rubric)
    assert ids == ["join", "entries", "presence", "access", "codes"]
    assert rubric.count('<div class="grid-2 rubric-pair">') == 2
    presence = card_of(rubric, "presence")
    assert presence.index('name="headcount"') < presence.index('id="attendance"')
    assert 'id="attendance"' not in card_of(rubric, "access")


def test_lock_from_the_card(admin: TestClient, world: World, owner: User) -> None:
    """The same form as the status bar, posting back to the card."""
    public_id = world.assembly(owner)
    card = entries_card(admin, public_id)
    assert "Entrées ouvertes" in text_of(card)
    form = f'action="/manage/{public_id}/entries#entries"'
    assert form in card and 'name="locked" value="1"' in card
    assert "Verrouiller les entrées" in text_of(card)
    assert "Le même verrou figure dans la barre d'état." in text_of(card)
    response = post(admin, f"/manage/{public_id}/entries", {"locked": "1"})
    assert response.status_code == 303 and response.headers["location"] == f"/manage/{public_id}"
    card = entries_card(admin, public_id)
    assert "Entrées verrouillées" in text_of(card) and 'name="locked" value="0"' in card
    assert "Déverrouiller les entrées" in text_of(card)
    assert "laisser entrer un retardataire" in text_of(card)


def test_entries_before_and_after_the_session(admin: TestClient, world: World, owner: User) -> None:
    """Before the opening, no lock to press (the status bar has none either): the card says
    when voters may join; after the closing, that every access has ended."""
    public_id = world.assembly(owner, in_session=False, registered_count=100)
    card = entries_card(admin, public_id)
    assert "Ouvertes avec la session" in text_of(card) and "/entries" not in card
    assert "barre d'état." not in text_of(card)
    assert "Plafond\u00a0: 110 accès (100 membres inscrits" in text_of(card)
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
        assemblies.close_assembly(db, owner, public_id)
    card = entries_card(admin, public_id)
    assert "Session terminée" in text_of(card) and "tous les accès des votants" in text_of(card)
    assert "/entries" not in card and "Plafond" not in text_of(card)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"registered_count": 280}, "Plafond\u00a0: 308 accès (280 membres inscrits et une marge"),
        ({"registered_count": 50}, "Plafond\u00a0: 60 accès (50 membres inscrits et une marge"),
        (
            {},
            "Plafond\u00a0: {quota} accès, le quota de codes du compte, faute de nombre de membres",
        ),
    ],
)
def test_access_cap_explained(
    admin: TestClient, world: World, owner: User, fields: dict[str, int], expected: str
) -> None:
    public_id = world.assembly(owner, **fields)
    quota = accounts.quotas(owner).codes_per_assembly
    text = text_of(entries_card(admin, public_id))
    assert expected.format(quota=quota) in text
    assert "saisir l'effectif compté relève le plafond, sans jamais l'abaisser" in text


def test_the_headcount_raises_the_cap(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, registered_count=100)
    post(admin, f"/manage/{public_id}/headcount", {"headcount": "90"})  # lower: members count
    assert "Plafond\u00a0: 110 accès (100 membres" in text_of(entries_card(admin, public_id))
    post(admin, f"/manage/{public_id}/headcount", {"headcount": "150"})
    text = text_of(entries_card(admin, public_id))
    assert "Plafond\u00a0: 165 accès (150 personnes comptées et une marge" in text
    assert "l'effectif compté l'a relevé" in text


def test_codes_mode_has_no_cap(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES, registered_count=100)
    text = text_of(entries_card(admin, public_id))
    assert "Codes individuels\u00a0: pas de plafond" in text and "Plafond\u00a0:" not in text


def test_access_mode_card(admin: TestClient, world: World, owner: User) -> None:
    """The mode in words, the password to change, the switch refused while a poll is open."""
    public_id = world.assembly(owner)
    card = card_of(access_rubric(admin, public_id), "access")
    text = text_of(card)
    assert text.startswith('id="access" aria-labelledby="access-title"> Mode d\'accès')
    assert "Mot de passe de la session\u00a0: un vote par téléphone." in text
    assert "Changer le mot de passe" in text and "Passer aux codes individuels" in text
    assert "Impossible pendant un vote en cours." not in text and " disabled" not in card
    poll_id = world.poll(owner, public_id)
    card = card_of(access_rubric(admin, public_id), "access")
    assert "Impossible pendant un vote en cours." in text_of(card)
    assert re.search(r'<button type="submit" class="button small"[^>]* disabled>', card)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
        assemblies.close_assembly(db, owner, public_id)
    card = card_of(access_rubric(admin, public_id), "access")
    assert "/mode" not in card and "/password" not in card


def test_codes_mode_card(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    text = text_of(card_of(access_rubric(admin, public_id), "access"))
    assert "Codes individuels\u00a0: un vote par personne" in text
    assert "Revenir au mot de passe de la session" in text
    assert "Changer le mot de passe" not in text


def test_a_headcount_delegate_gets_the_headcount_only(
    app: FastAPI, world: World, owner: User
) -> None:
    """FR-92: the headcount card, without the attendance record, the entries or the cap."""
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ["headcount"])
    rubric = delegate_access_rubric(open_link(app, public_id, token), public_id)
    ids = re.findall(r'<section class="card[^"]*" id="([\w-]+)"', rubric)
    assert ids == ["presence"]
    assert 'id="attendance"' not in rubric and "Plafond" not in rubric
