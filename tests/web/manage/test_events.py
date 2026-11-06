"""Server-Sent Events of the console: one stream per tab, up to a cap per holder, ended when its
holder loses the console (SPEC §9.3)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import assemblies, auth, delegates
from agaura.web.realtime import STREAM_CAPS, manage_channel
from tests.conftest import World
from tests.web.manage.test_delegates import make_link, open_link
from tests.web.streams import follow, hold_streams, wait_for


def test_a_revoked_delegate_link_ends_its_streams(
    client: TestClient, app: FastAPI, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = make_link(world, owner, public_id, ["timer"])
    browser = open_link(app, public_id, token)
    hub = app.state.hub
    channel = manage_channel(public_id)

    def revoke() -> None:
        with world.database.write() as db:
            delegates.revoke_delegate(db, owner, public_id, delegate_id)
        hub.publish(channel, "turnout", "console")  # as the console does

    # Through ``client``, whose event loop runs the hub (``browser`` has a loop per request).
    client.cookies.update(browser.cookies)
    followed = follow(client, f"/manage/{public_id}/events", hub, channel, revoke)
    assert followed.response.status_code == 200 and followed.ended_by_itself
    assert "event:" not in followed.response.text


def test_the_owner_logged_out_elsewhere_loses_its_streams(
    admin: TestClient, app: FastAPI, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    hub = app.state.hub
    channel = manage_channel(public_id)

    def log_out_everywhere() -> None:
        with world.database.write() as db:
            auth.end_all_sessions(db, owner)
        hub.publish(channel, "turnout")

    followed = follow(admin, f"/manage/{public_id}/events", hub, channel, log_out_everywhere)
    assert followed.ended_by_itself and "event:" not in followed.response.text


def test_the_owner_keeps_its_streams_while_allowed(
    admin: TestClient, app: FastAPI, world: World, owner: User
) -> None:
    """Up to the cap, then 429; the owner keeps following a closed session."""
    public_id = world.assembly(owner)
    hub = app.state.hub
    channel = manage_channel(public_id)
    url = f"/manage/{public_id}/events"
    held = hold_streams(admin, url, hub, channel, STREAM_CAPS["console"])
    assert admin.get(url).status_code == 429
    hub.publish(channel, "turnout")
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    hub.publish(channel, "turnout", "console")
    assert wait_for(lambda: hub.streams() == STREAM_CAPS["console"])
    responses = held.close(hub)
    assert [response.text.count("event: turnout") for response in responses] == [2] * 8
    assert hub.streams() == 0
