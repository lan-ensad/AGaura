"""Server-Sent Events of the live page (FR-42): delivery, cap per device, end on revocation."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import access, assemblies
from agaura.web import realtime
from agaura.web.live.routes import VOTER_COOKIE
from agaura.web.realtime import STREAM_CAPS, live_channel
from tests.conftest import World
from tests.web.live.conftest import join
from tests.web.streams import follow, hold_streams


def test_refused_without_credential(client: TestClient, session_id: str) -> None:
    # EventSource stops reconnecting on 204; the page then refreshes and reloads (live.js).
    response = client.get(f"/{session_id}/events")
    assert response.status_code == 204 and response.content == b""
    assert client.get("/ZZZZ99/events").status_code == 204
    assert client.get("/nope/events").status_code == 204


def test_delivers_updates_published_on_the_hub(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    hub = app.state.hub
    channel = live_channel(session_id)

    def console() -> None:
        deadline = time.monotonic() + 5
        while hub.subscribers(channel) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        hub.publish(channel, "update", "polls")
        hub.publish(live_channel("OTHER1"), "update", "polls")
        time.sleep(0.05)
        hub.close()  # ends the stream, so the test client gets the whole response

    thread = threading.Thread(target=console)
    thread.start()
    response = client.get(f"/{session_id}/events")
    thread.join()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: update") == 1
    assert 'data: "polls"' in response.text


def test_a_device_keeps_a_few_streams(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    """Past its cap a device gets 429: live.js then polls (other holders: test_realtime.py)."""
    join(client, world, owner, session_id)
    hub = app.state.hub
    channel = live_channel(session_id)
    url = f"/{session_id}/events"
    held = hold_streams(client, url, hub, channel, STREAM_CAPS["voter"])
    refused = client.get(url)
    assert refused.status_code == 429 and refused.content == b""
    assert hub.streams() == STREAM_CAPS["voter"]
    assert all(response.status_code == 200 for response in held.close(hub))
    assert hub.streams() == 0  # every slot given back


def test_a_stream_ends_when_the_device_leaves(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    """An event published after the credential stopped working is never passed on."""
    join(client, world, owner, session_id)
    token = client.cookies[VOTER_COOKIE]
    hub = app.state.hub
    channel = live_channel(session_id)

    def leave() -> None:
        with world.database.write() as db:
            access.leave(db, session_id, token)
        hub.publish(channel, "update", "polls")

    followed = follow(client, f"/{session_id}/events", hub, channel, leave)
    assert followed.ended_by_itself
    assert followed.response.status_code == 200 and "event:" not in followed.response.text
    assert hub.streams() == 0


def test_a_stream_ends_without_any_event(
    client: TestClient,
    app: FastAPI,
    world: World,
    owner: User,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the session ends voter streams at the next periodic check."""
    monkeypatch.setattr(realtime, "RECHECK_INTERVAL", 0.05)
    join(client, world, owner, session_id)
    hub = app.state.hub

    def close_session() -> None:
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)

    channel = live_channel(session_id)
    followed = follow(client, f"/{session_id}/events", hub, channel, close_session)
    assert followed.ended_by_itself and "event:" not in followed.response.text
