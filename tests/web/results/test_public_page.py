"""Public results page (FR-17): secret link, published results and cancellations only."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import access
from tests.conftest import World
from tests.exports.conftest import Built, build_session


def enable_link(world: World, owner: User, public_id: str, enabled: bool = True) -> str | None:
    with world.database.write() as db:
        return access.set_public_results(db, world.keyring, owner, public_id, enabled)


@pytest.fixture
def built(world: World, owner: User) -> Built:
    return build_session(world, owner, close=False)


@pytest.fixture
def token(world: World, owner: User, built: Built) -> str:
    token = enable_link(world, owner, built.public_id)
    assert token is not None
    return token


def test_shows_published_results_and_cancellations(
    client: TestClient, built: Built, token: str
) -> None:
    response = client.get(f"/{built.public_id}/results/{token}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    page = response.text
    titles = re.findall(r'<h2 id="result-\d+-title">([^<]+)</h2>', page)
    assert titles == [
        "Approbation des comptes",
        "Élection du conseil (2 sièges)",
        "Projets de quartier",
        "Cotisation 2027",
        "Modification des statuts",
    ]
    for text in (
        "Assemblée générale 2026",
        "20 juin 2026 · Salle des fêtes",
        "La session est en cours\u00a0: d'autres résultats peuvent suivre.",
        "Adoptée",
        "Égalité entre Bruno, Chloé\u00a0: appliquez vos statuts",
        "Égalité entre Jardin, Fresque\u00a0: appliquez vos statuts",
        "Annulé\u00a0: Vote à refaire",
        "Annulé\u00a0: Erreur dans l&#39;énoncé",
        "Participation",
        "SHA-256",
        'class="merit"',
    ):
        assert text in page, text
    for hidden in ("Motion non publiée", "Brouillon secret"):
        assert hidden not in page


def test_page_is_csp_clean(client: TestClient, built: Built, token: str) -> None:
    page = client.get(f"/{built.public_id}/results/{token}").text
    assert " style=" not in page
    assert re.findall(r"<script(?![^>]*\bsrc=)", page) == []


def test_open_polls_never_show(
    client: TestClient, world: World, owner: User, built: Built, token: str
) -> None:
    world.poll(owner, built.public_id)  # RESOLUTION, open
    page = client.get(f"/{built.public_id}/results/{token}").text
    assert page.count("Approbation des comptes") == 1


def test_wrong_or_disabled_links(
    client: TestClient, world: World, owner: User, built: Built, token: str
) -> None:
    assert client.get(f"/{built.public_id}/results/{token}x").status_code == 404
    assert client.get(f"/ZZZZZ9/results/{token}").status_code == 404
    assert client.get(f"/not-an-id/results/{token}").status_code == 404
    other = world.assembly(owner)
    assert client.get(f"/{other}/results/{token}").status_code == 404  # link of another session
    renewed = enable_link(world, owner, built.public_id)
    assert client.get(f"/{built.public_id}/results/{token}").status_code == 404
    assert client.get(f"/{built.public_id}/results/{renewed}").status_code == 200
    enable_link(world, owner, built.public_id, enabled=False)
    response = client.get(f"/{built.public_id}/results/{renewed}")
    assert response.status_code == 404 and "Page introuvable" in response.text


def test_screen_token_is_not_a_results_token(
    client: TestClient, world: World, owner: User, built: Built, token: str
) -> None:
    with world.database.read() as db:
        screen = access.screen_token(db, world.keyring, owner, built.public_id)
    assert client.get(f"/{built.public_id}/results/{screen}").status_code == 404


def test_lowercase_session_id_redirects(client: TestClient, built: Built, token: str) -> None:
    response = client.get(f"/{built.public_id.lower()}/results/{token}")
    assert response.status_code == 303
    assert response.headers["location"] == f"/{built.public_id}/results/{token}"
    assert client.get(f"/{built.public_id.lower()}/results/wrong").status_code == 404


def test_nothing_published_yet(client: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    world.poll(owner, public_id)
    token = enable_link(world, owner, public_id)
    page = client.get(f"/{public_id}/results/{token}").text
    assert "Aucun résultat n'a encore été publié." in page
    assert "Approbation des comptes" not in page


def test_closed_session(client: TestClient, world: World, owner: User) -> None:
    built = build_session(world, owner)
    token = enable_link(world, owner, built.public_id)
    page = client.get(f"/{built.public_id}/results/{token}").text
    assert "Projets de quartier" in page and "La session est en cours" not in page
