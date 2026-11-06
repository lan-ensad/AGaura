"""Live parts of the console (GET /manage/{id}/panel, FR-91): during the session, the commands of
Steering that other consoles change, the item under discussion and the next poll, follow them
live, each for whoever holds its right; every action that changes them notifies the console
channel; "Previous" and "Next" of Steering are answered in place when manage.js sends them."""

from __future__ import annotations

import re
from collections.abc import Callable
from html import unescape

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agaura.models import User
from agaura.services import assemblies, meeting
from agaura.web.manage.editor import EditorFields, fields_from_kind
from agaura.web.realtime import manage_channel
from tests.conftest import World
from tests.web.conftest import post
from tests.web.manage.test_delegates import ALL_RIGHTS, make_link, open_link

FETCH = {"X-Requested-With": "fetch"}  # as manage.js sends a form of a live part
STEERING = {"agenda-now", "ready", "ready-link"}


def text_of(html: str) -> str:
    text = " ".join(re.findall(r"[^ \t\n\r\f\v]+", unescape(re.sub(r"<[^>]+>", " ", html))))
    # A tag before a no-break space ("</strong>\u00a0:") leaves no gap in the page.
    return re.sub(r" ?\u00a0 ?", "\u00a0", text)


def parts(html: str) -> dict[str, list[str]]:
    """The inner HTML of each ``[data-live-part]`` element, by name, in the order of the page,
    without the white space around it (manage.js compares them the same way)."""
    found: dict[str, list[str]] = {}
    for match in re.finditer(r'<(div|span)\b[^>]*\bdata-live-part="([\w-]+)"[^>]*>', html):
        tag, name = match.groups()
        tokens = re.compile(rf"<{tag}\b|</{tag}>")
        depth, index, end = 1, match.end(), match.end()
        while depth:
            token = tokens.search(html, index)
            assert token, name
            depth += -1 if token.group(0).startswith("</") else 1
            index, end = token.end(), token.start()
        found.setdefault(name, []).append(html[match.end() : end].strip())
    return found


def panel(client: TestClient, public_id: str) -> dict[str, list[str]]:
    response = client.get(f"/manage/{public_id}/panel")
    assert response.status_code == 200, response.text
    return parts(response.text)


def with_agenda(world: World, owner: User, public_id: str, *titles: str) -> None:
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, list(titles))


def current_item(world: World, owner: User, public_id: str) -> int | None:
    with world.database.read() as db:
        return assemblies.get_assembly(db, owner, public_id).agenda_current


def draft_form(title: str) -> dict[str, object]:
    fields = fields_from_kind("resolution").model_dump(exclude=set(EditorFields.BUTTONS))
    return {**fields, "title": title, "action": "draft"}


@pytest.fixture
def published(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    events: list[tuple[str, str, str]] = []
    original = app.state.hub.publish

    def record(channel: str, event: str, data: str = "") -> None:
        events.append((channel, event, data))
        original(channel, event, data)

    monkeypatch.setattr(app.state.hub, "publish", record)
    return events


# --- The panel ----------------------------------------------------------------------------------


def test_the_panel_answers_the_commands_during_the_session_only(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, in_session=False)
    first = world.poll(owner, public_id, open_it=False)
    with_agenda(world, owner, public_id, "Rapport moral", "Budget")
    # Before the opening:
    assert set(panel(admin, public_id)) == {"status", "polls", "poll-tiles", "screen-now"}

    post(admin, f"/manage/{public_id}/open")
    answer = panel(admin, public_id)
    assert set(answer) == {"status", "polls", "poll-tiles", "screen-now", *STEERING}
    now, ready, link = answer["agenda-now"][0], answer["ready"][0], answer["ready-link"][0]
    assert text_of(now).startswith(
        "Aucun point en cours À venir\u00a0: Rapport moral Précédent Suivant"
    )
    assert "data-live-form" in now and 'name="item" value="next"' in now
    assert "Approbation des comptes" in text_of(ready) and "Lancer le vote" in text_of(ready)
    assert f"/polls/{first}/open#tab-cockpit" in ready and link == "Brouillons, dans Votes"
    # The page carries the same HTML: manage.js leaves a part alone until it changes. The heads
    # ("Confier" included) stay out of the parts.
    page = parts(admin.get(f"/manage/{public_id}").text)
    for name in (*STEERING, "screen-now"):
        assert page[name] == answer[name], name
    assert "hand-over" not in now + ready and "Point en cours" not in text_of(now)

    # What another console changes shows in the next answer.
    post(admin, f"/manage/{public_id}/agenda/current", {"item": "next", "seen": ""})
    second = world.poll(owner, public_id, open_it=False)
    answer = panel(admin, public_id)
    assert "Point 1 sur 2 Rapport moral" in text_of(answer["agenda-now"][0])
    assert '<input type="hidden" name="seen" value="1">' in answer["agenda-now"][0]
    assert answer["ready-link"] == ["1 autre brouillon, dans Votes"]
    post(admin, f"/manage/{public_id}/polls/{first}/open")
    ready = panel(admin, public_id)["ready"][0]
    assert f"/polls/{second}/open" in ready and f"/polls/{first}/" not in ready
    assert panel(admin, public_id)["ready-link"] == ["Brouillons, dans Votes"]
    post(admin, f"/manage/{public_id}/polls/{second}/delete")
    assert "Aucun brouillon." in text_of(panel(admin, public_id)["ready"][0])

    post(admin, f"/manage/{public_id}/polls/{first}/close")
    assert post(admin, f"/manage/{public_id}/close").status_code == 303
    # After the closing:
    assert set(panel(admin, public_id)) == {"status", "polls", "poll-tiles", "screen-now"}
    assert 'data-status="closed"' in admin.get(f"/manage/{public_id}/panel").text


def test_the_panel_leaves_the_notice_to_the_console_page(
    admin: TestClient, world: World, owner: User
) -> None:
    """Fetched on every event, the panel never consumes the notice of an action, nor is it
    stored."""
    public_id = world.assembly(owner)
    with_agenda(world, owner, public_id, "Rapport moral")
    post(admin, f"/manage/{public_id}/agenda/current", {"item": "next", "seen": ""})
    response = admin.get(f"/manage/{public_id}/panel")
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "Point en cours modifié." in text_of(admin.get(f"/manage/{public_id}").text)


@pytest.mark.parametrize(
    ("rights", "expected"),
    [
        (["agenda"], {"status", "agenda-now"}),
        (["polls"], {"status", "polls", "poll-tiles", "ready", "ready-link"}),
        (["notes", "timer", "headcount"], {"status"}),
        (["screen"], {"status", "screen-now"}),
        (ALL_RIGHTS, {"status", "polls", "poll-tiles", "screen-now", *STEERING}),
    ],
)
def test_a_delegate_gets_the_parts_of_its_rights(
    app: FastAPI, world: World, owner: User, rights: list[str], expected: set[str]
) -> None:
    """Only the parts of its rights, and the commands during the session only. The item under
    discussion, which a delegate without the agenda right reads on its page, never follows."""
    public_id = world.assembly(owner, in_session=False)
    world.poll(owner, public_id, open_it=False)
    with_agenda(world, owner, public_id, "Rapport moral", "Budget")
    _, token = make_link(world, owner, public_id, rights)
    browser = open_link(app, public_id, token)
    assert set(panel(browser, public_id)) == expected - STEERING  # before the opening
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
    answer = panel(browser, public_id)
    assert set(answer) == expected
    page = parts(browser.get(f"/manage/{public_id}").text)
    for name in expected & (STEERING | {"screen-now"}):
        assert page[name] == answer[name], name
    if "agenda" not in rights:
        assert all("/agenda/current" not in html for copies in answer.values() for html in copies)


# --- Events ------------------------------------------------------------------------------------


def test_the_actions_on_the_commands_notify_the_consoles(
    admin: TestClient, world: World, owner: User, published: list[tuple[str, str, str]]
) -> None:
    """Moving the item, editing the agenda, and every change to the drafts publish the console
    event on which the other consoles fetch their live parts."""
    public_id = world.assembly(owner)
    source = world.assembly(owner, in_session=False, title="AG 2025")
    world.poll(owner, source, open_it=False)
    first = world.poll(owner, public_id, open_it=False)
    base = f"/manage/{public_id}"
    actions: dict[str, Callable[[], Response]] = {
        "agenda": lambda: post(admin, f"{base}/agenda", {"items": "Rapport moral\nBudget"}),
        "item": lambda: post(admin, f"{base}/agenda/current", {"item": "next", "seen": ""}),
        "item in place": lambda: post(
            admin, f"{base}/agenda/current", {"item": "next", "seen": "1"}, headers=FETCH
        ),
        "create": lambda: post(admin, f"{base}/polls/new", draft_form("Budget 2027")),
        "edit": lambda: post(admin, f"{base}/polls/{first}/edit", draft_form("Comptes 2026")),
        "reorder": lambda: post(admin, f"{base}/polls/{first}/move", {"direction": "down"}),
        "duplicate": lambda: post(admin, f"{base}/polls/{first}/duplicate"),
        "copy": lambda: post(admin, f"{base}/polls/copy", {"source": source}),
        "push": lambda: post(admin, f"{base}/polls/{first}/open"),
    }
    for name, action in actions.items():
        published.clear()
        response = action()
        assert response.status_code in (204, 303), name
        assert "agaura_flash=error" not in response.headers.get("set-cookie", ""), name
        assert (manage_channel(public_id), "turnout", "console") in published, name
    answer = panel(admin, public_id)
    # Four drafts (the first, one created, one duplicated, one copied), one of them pushed.
    assert answer["ready-link"] == ["2 autres brouillons, dans Votes"]
    (draft,) = re.findall(r"/polls/(\d+)/open", answer["ready"][0])
    published.clear()
    assert post(admin, f"{base}/polls/{draft}/delete").status_code == 303
    assert (manage_channel(public_id), "turnout", "console") in published


# --- "Previous" and "Next" in place -------------------------------------------------------------


def test_moving_the_item_from_steering_is_answered_in_place(
    admin: TestClient, world: World, owner: User, published: list[tuple[str, str, str]]
) -> None:
    public_id = world.assembly(owner)
    with_agenda(world, owner, public_id, "Rapport moral", "Budget")
    url = f"/manage/{public_id}/agenda/current"
    response = post(admin, url, {"item": "next", "seen": ""}, headers=FETCH)
    assert response.status_code == 204 and response.content == b""
    assert "set-cookie" not in response.headers  # no notice for a page that stays
    assert current_item(world, owner, public_id) == 1
    assert published == [(manage_channel(public_id), "turnout", "console")]
    # A double tap moves once: the second one comes from an item already left.
    assert post(admin, url, {"item": "next", "seen": ""}, headers=FETCH).status_code == 204
    assert current_item(world, owner, public_id) == 1

    # Refused: 400, and the message waits for the console page that manage.js loads again.
    published.clear()
    response = post(admin, url, {"item": "7", "seen": "1"}, headers=FETCH)
    assert response.status_code == 400 and published == []
    assert "agaura_flash=error:meeting_no_such_item" in response.headers["set-cookie"]
    page = text_of(admin.get(f"/manage/{public_id}").text)
    assert "Ce point de l'ordre du jour n'existe plus" in page

    # Without script, the redirect and the notice as before; other routes keep their redirect.
    response = post(admin, url, {"item": "next", "seen": "1"})
    assert response.status_code == 303 and response.headers["location"] == f"/manage/{public_id}"
    tribune = {"name": "Marie Dupont", "role": ""}  # the agenda card has a script of its own
    response = post(admin, f"/manage/{public_id}/tribune", tribune, headers=FETCH)
    assert response.status_code == 303
