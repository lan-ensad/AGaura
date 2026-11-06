"""A disabled account (``agaura user disable``): its sessions look unknown from outside."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import accounts
from agaura.web.live.routes import VOTER_COOKIE
from tests.conftest import World
from tests.web.conftest import post
from tests.web.live.conftest import ballots_of, join
from tests.web.live.test_limits import without_id
from tests.web.results.test_public_page import enable_link
from tests.web.screen.conftest import screen_url


def set_disabled(world: World, owner: User, disabled: bool) -> None:
    with world.database.write() as db:
        accounts.set_disabled(db, owner.email, disabled)


def test_screen_results_and_voters_are_cut_off_until_enabled_again(
    app: FastAPI, client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """A compromised or abused account: what it shows the public stops at once, the voters
    already in can neither read nor vote, and nothing is deleted."""
    join(client, world, owner, session_id)
    poll_id = world.poll(owner, session_id)
    screen = screen_url(world, owner, session_id)
    results = f"/{session_id}/results/{enable_link(world, owner, session_id)}"
    newcomer = TestClient(app, follow_redirects=False)
    assert client.get(screen).status_code == 200 and client.get(results).status_code == 200

    set_disabled(world, owner, True)
    assert client.get(screen).status_code == 404
    assert client.get(results).status_code == 404
    page = client.get(f"/{session_id}")
    unknown = client.get("/ZZZZZ9")  # the page of a session that never existed
    assert page.status_code == unknown.status_code == 404
    assert without_id(page.text, session_id) == without_id(unknown.text, "ZZZZZ9")
    vote = post(client, f"/{session_id}/polls/{poll_id}", {"choice": "01"})
    assert vote.status_code in (303, 403, 404) and ballots_of(world, poll_id) == []
    refused = post(newcomer, f"/{session_id}/join", {"secret": world.password(owner, session_id)})
    assert refused.status_code == 404 and not newcomer.cookies.get(VOTER_COOKIE)

    set_disabled(world, owner, False)  # everything comes back: the same links, the same device
    assert client.get(screen).status_code == 200 and client.get(results).status_code == 200
    assert post(client, f"/{session_id}/polls/{poll_id}", {"choice": "01"}).status_code == 303
    assert ballots_of(world, poll_id) == [(False, {"choice": 1})]
