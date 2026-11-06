"""Service ethics: on the home page, behind the information button of every voter page, and the
link to the source code once its address is configured (AGPL, section 13)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import User
from tests.conftest import World
from tests.web.conftest import post

SOURCE = "https://example.org/agaura"


def test_home_page_states_the_service_ethics(client: TestClient) -> None:
    page = client.get("/").text
    assert "Déontologie du service" in page
    for heading in (
        "Secret du vote.",
        "Votants sans compte.",
        "Aucun pistage.",
        "Résultats vérifiables.",
        "Limites annoncées.",
        "Code source ouvert.",
    ):
        assert heading in page
    assert "Lire le code source" not in page  # no address configured


def test_source_code_is_linked_once_configured(app: FastAPI) -> None:
    app.state.settings = app.state.settings.model_copy(update={"source_url": SOURCE})
    with TestClient(app) as client:
        page = client.get("/").text
    assert page.count(f'href="{SOURCE}"') == 2  # ethics card and footer
    assert "Lire le code source" in page


def test_voter_pages_offer_the_information_button(
    client: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    entry = client.get(f"/{public_id}").text
    assert '<details class="about">' in entry and "À propos du service" in entry
    assert "Déontologie du service" in entry
    post(client, f"/{public_id}/join", {"secret": world.password(owner, public_id)})
    live = client.get(f"/{public_id}").text
    assert "En attente du prochain vote" in live
    assert '<details class="about">' in live and "Secret du vote." in live


def test_limits_state_the_rule_of_the_projection_screen(client: TestClient) -> None:
    """The sentence on small polls follows the code of the screen (FR-40a, FR-75)."""
    from agaura.services.screen import SCREEN_DETAIL_MIN

    page = client.get("/").text.replace("&#39;", "'")
    assert "L'écran de projection n'affiche un résultat que si la tribune le choisit" in page
    assert f"le détail des voix qu'à partir de {SCREEN_DETAIL_MIN} bulletins" in page
    assert "le résultat publié sur les téléphones et le procès-verbal en donnent" in page
