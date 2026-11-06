"""Server-Sent Events of the projection screen: the console channel of its session (FR-73)."""

from __future__ import annotations

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from agaura.models import User
from agaura.services import access
from agaura.web.realtime import STREAM_CAPS, live_channel, manage_channel
from tests.conftest import World
from tests.web.conftest import post
from tests.web.streams import follow, hold_streams


def test_follows_the_console_channel(
    client: TestClient, app: FastAPI, session_id: str, url: str
) -> None:
    hub = app.state.hub
    channel = manage_channel(session_id)

    def console() -> None:
        deadline = time.monotonic() + 5
        while hub.subscribers(channel) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        hub.publish(channel, "turnout", "console")  # a console action
        hub.publish(channel, "turnout")  # a join or a ballot
        hub.publish(live_channel(session_id), "update", "polls")  # voters only
        hub.publish(manage_channel("OTHER1"), "turnout", "console")
        time.sleep(0.05)
        hub.close()  # ends the stream, so the test client gets the whole response

    thread = threading.Thread(target=console)
    thread.start()
    response = client.get(f"{url}/events")
    thread.join()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: turnout") == 2
    assert "event: update" not in response.text


def test_refused_without_a_valid_link(client: TestClient, session_id: str, url: str) -> None:
    # EventSource does not reconnect after a 404; screen.js then reloads the page.
    assert client.get(f"/{session_id}/screen/wrong/events").status_code == 404
    assert client.get(f"/ZZZZ99/screen/{url.rsplit('/', 1)[1]}/events").status_code == 404


def test_deleting_the_session_wakes_its_screens(
    app: FastAPI, admin: TestClient, world: World, owner: User, monkeypatch: MonkeyPatch
) -> None:
    """An open screen refetches at once and turns into the 404 page."""
    public_id = world.assembly(owner, in_session=False)
    published: list[tuple[str, ...]] = []
    monkeypatch.setattr(app.state.hub, "publish", lambda *args: published.append(args))
    response = post(admin, f"/manage/{public_id}/delete", {"confirm": "yes"})
    assert response.headers["location"] == "/dashboard"
    assert published == [(manage_channel(public_id), "turnout", "console")]


def test_a_regenerated_link_ends_open_streams(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str, url: str
) -> None:
    hub = app.state.hub
    channel = manage_channel(session_id)

    def regenerate() -> None:
        with world.database.write() as db:
            access.regenerate_screen_token(db, world.keyring, owner, session_id)
        hub.publish(channel, "turnout", "console")  # as the console does

    followed = follow(client, f"{url}/events", hub, channel, regenerate)
    assert followed.ended_by_itself and "event:" not in followed.response.text


def test_a_screen_link_keeps_a_few_streams(
    client: TestClient, app: FastAPI, session_id: str, url: str
) -> None:
    hub = app.state.hub
    channel = manage_channel(session_id)
    held = hold_streams(client, f"{url}/events", hub, channel, STREAM_CAPS["screen"])
    assert client.get(f"{url}/events").status_code == 429
    assert all(response.status_code == 200 for response in held.close(hub))
    assert hub.streams() == 0
