"""Delegate links (FR-92): opening a link without an account, the rights of each console route,
isolation between sessions, end of a link, attribution in the session log."""

from __future__ import annotations

import inspect
import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.models import AuditEntry, Poll, PollStatus, User
from agaura.services import assemblies, delegates, meeting
from agaura.web import manage
from agaura.web.manage import delegates as delegates_web
from agaura.web.manage.common import ANY_RIGHT, DELEGATE_COOKIE, session_for
from tests.conftest import World
from tests.web.conftest import log_in, post
from tests.web.manage.test_isolation import NOT_READY, fill, manage_routes

ALL_RIGHTS = ["agenda", "notes", "polls", "timer", "screen", "headcount"]

# The decision for every console route a delegate link may use, by right; every other console
# route is the owner's only. A new console route is the owner's until it is added here.
DELEGABLE: dict[str, str] = {
    "GET /manage/{public_id}": ANY_RIGHT,
    "GET /manage/{public_id}/panel": ANY_RIGHT,
    "GET /manage/{public_id}/events": ANY_RIGHT,
    "GET /manage/{public_id}/agenda": "agenda",
    "POST /manage/{public_id}/agenda": "agenda",
    "POST /manage/{public_id}/agenda/current": "agenda",
    "GET /manage/{public_id}/notes": "notes",
    "POST /manage/{public_id}/notes": "notes",
    "POST /manage/{public_id}/notes/edit": "notes",
    "POST /manage/{public_id}/notes/delete": "notes",
    "GET /manage/{public_id}/polls/new": "polls",
    "POST /manage/{public_id}/polls/new": "polls",
    "GET /manage/{public_id}/polls/{poll_id}/edit": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/edit": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/move": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/open": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/close": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/cancel": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/publish": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/dismiss": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/screen": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/duplicate": "polls",
    "POST /manage/{public_id}/polls/{poll_id}/delete": "polls",
    "GET /manage/{public_id}/timer": "timer",
    "POST /manage/{public_id}/timer/start": "timer",
    "POST /manage/{public_id}/timer/pause": "timer",
    "POST /manage/{public_id}/timer/resume": "timer",
    "POST /manage/{public_id}/timer/reset": "timer",
    "POST /manage/{public_id}/timer/add": "timer",
    "POST /manage/{public_id}/timer/show": "timer",
    "POST /manage/{public_id}/screen/show": "screen",
    "POST /manage/{public_id}/headcount": "headcount",
}
OWNER_ONLY = "owner"
SESSION_DEPENDENCY = session_for(None).__qualname__


def route_right(app: FastAPI, method: str, path: str) -> str:
    """The right a console route requires, read from its session dependency."""
    route = next(
        route
        for route in manage.router.routes
        if isinstance(route, APIRoute) and route.path == path and method in route.methods
    )
    found = [
        inspect.getclosurevars(dependency.call).nonlocals["right"]
        for dependency in route.dependant.dependencies
        if getattr(dependency.call, "__qualname__", "") == SESSION_DEPENDENCY
    ]
    assert len(found) == 1, f"{method} {path}: one session dependency expected, got {found}"
    right = found[0]
    return OWNER_ONLY if right is None else str(right)


def make_link(world: World, owner: User, public_id: str, rights: list[str], name: str = "Claire"):
    with world.database.write() as db:
        delegate, token = delegates.create_delegate(
            db, world.keyring, owner, public_id, name, rights
        )
        return delegate.id, token


def open_link(app: FastAPI, public_id: str, token: str) -> TestClient:
    """A browser without an account that opens the link, as delegate.js does."""
    browser = TestClient(app, follow_redirects=False)
    assert browser.get(f"/manage/{public_id}/delegate").status_code == 200
    response = post(browser, f"/manage/{public_id}/delegate", {"key": token})
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/manage/{public_id}"
    return browser


def audit_count(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(AuditEntry)) or 0


@pytest.fixture
def target(world: World, owner: User) -> dict[str, str]:
    """A session in session with a draft, for filling route parameters."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, open_it=False)
    return {"public_id": public_id, "poll_id": str(poll_id), "batch_id": "1"}


# --- Rights of the console routes ---------------------------------------------------------------


def test_every_console_route_states_who_may_use_it(app: FastAPI) -> None:
    rights = {
        f"{method} {path}": route_right(app, method, path) for method, path in manage_routes(app)
    }
    delegable = {route: right for route, right in rights.items() if right != OWNER_ONLY}
    assert delegable == DELEGABLE
    assert "POST /manage/{public_id}/polls/copy" in rights  # other sessions: the owner's only
    assert rights["POST /manage/{public_id}/polls/copy"] == OWNER_ONLY


@pytest.mark.parametrize(
    "rights", [ALL_RIGHTS, ["agenda"], ["notes"], ["polls"], ["timer"], ["screen"], ["headcount"]]
)
def test_a_delegate_gets_404_outside_its_rights(
    app: FastAPI, world: World, owner: User, target: dict[str, str], rights: list[str]
) -> None:
    _, token = make_link(world, owner, target["public_id"], rights)
    browser = open_link(app, target["public_id"], token)
    before = audit_count(world)
    for method, path in manage_routes(app):
        right = DELEGABLE.get(f"{method} {path}", OWNER_ONLY)
        if right == ANY_RIGHT or right in rights:
            continue
        url = fill(path, target)
        response = browser.get(url) if method == "GET" else post(browser, url, {"confirm": "yes"})
        assert response.status_code == 404, f"{method} {url}"
    assert audit_count(world) == before  # nothing happened


@pytest.mark.parametrize("rights", [ALL_RIGHTS, ["timer"]])
def test_a_delegate_reaches_the_reads_of_its_rights(
    app: FastAPI, world: World, owner: User, target: dict[str, str], rights: list[str]
) -> None:
    _, token = make_link(world, owner, target["public_id"], rights)
    browser = open_link(app, target["public_id"], token)
    for method, path in manage_routes(app):
        right = DELEGABLE.get(f"{method} {path}", OWNER_ONLY)
        allowed = right == ANY_RIGHT or right in rights
        if method != "GET" or path.endswith("/events") or not allowed:
            continue
        expected = NOT_READY.get(path, 200)
        assert browser.get(fill(path, target)).status_code == expected, path


# --- Opening a link -----------------------------------------------------------------------------


def test_the_owner_creates_a_link_and_sees_it_again(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    response = post(
        admin, f"/manage/{public_id}/delegates", {"name": "Claire", "rights": ["timer", "polls"]}
    )
    assert response.status_code == 303
    with world.database.read() as db:
        (delegate,) = delegates.list_delegates(db, owner, public_id)
        token = delegates.link_token(world.keyring, delegate)
    assert response.headers["location"] == f"/manage/{public_id}#delegate-{delegate.id}-link"
    response = admin.get(f"/manage/{public_id}")
    assert response.headers["cache-control"] == "no-store"  # it carries the keys
    page = response.text
    assert "Lien créé" in page
    assert f"/manage/{public_id}/delegate#k={token}" in page
    assert delegate.rights == ["polls", "timer"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "", "rights": ["timer"]}, "Indiquez le nom"),
        ({"name": "Claire"}, "Cochez au moins"),
        ({"name": "Claire", "rights": ["settings"]}, "Cochez au moins"),
    ],
)
def test_invalid_links_are_refused(
    admin: TestClient, world: World, owner: User, fields: dict, message: str
) -> None:
    public_id = world.assembly(owner)
    response = post(admin, f"/manage/{public_id}/delegates", fields)
    assert response.status_code == 303
    assert message in admin.get(f"/manage/{public_id}").text
    with world.database.read() as db:
        assert delegates.list_delegates(db, owner, public_id) == []


def test_opening_a_link_keeps_its_key_in_a_cookie_of_the_session(
    app: FastAPI, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ["timer"])
    browser = TestClient(app, follow_redirects=False)
    page = browser.get(f"/manage/{public_id}/delegate")
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "data-delegate-join" in page.text
    response = post(browser, f"/manage/{public_id.lower()}/delegate", {"key": f" {token} "})
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{DELEGATE_COOKIE}={token};")
    assert f"Path=/manage/{public_id}" in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_a_wrong_key_is_refused_then_limited(app: FastAPI, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    browser = TestClient(app, follow_redirects=False)
    response = post(browser, f"/manage/{public_id}/delegate", {"key": "nope"})
    assert response.status_code == 403
    assert "ne donne pas accès à la console" in response.text
    assert DELEGATE_COOKIE not in response.headers.get("set-cookie", "")
    for _ in range(29):
        post(browser, f"/manage/{public_id}/delegate", {"key": "nope"})
    _, token = make_link(world, owner, public_id, ["timer"])
    response = post(browser, f"/manage/{public_id}/delegate", {"key": token})
    assert response.status_code == 429


def test_the_delegate_console_shows_its_rubrics_only(
    app: FastAPI, world: World, owner: User
) -> None:
    other = world.assembly(owner, title="Autre session confidentielle")
    world.poll(owner, other, open_it=False)
    public_id = world.assembly(owner, title="AG 2026")
    world.poll(owner, public_id, open_it=False)
    password = world.password(owner, public_id)
    _, token = make_link(world, owner, public_id, ["polls", "timer"], name="Claire Martin")
    page = open_link(app, public_id, token).get(f"/manage/{public_id}")
    assert page.status_code == 200
    text = page.text
    assert "Accès délégué\u00a0: Claire Martin" in text and "Quitter" in text
    assert 'id="tab-cockpit"' in text and 'id="tab-polls"' in text
    assert 'id="cmd-timer"' in text and 'id="live"' in text  # the commands of its rights
    # The chronometer's card in its rubric, without the screen link (D-A).
    screen = text[text.index('<section class="rubric" id="tab-screen"') :]
    assert 'id="timer"' in screen and 'id="links"' not in text
    for absent in ("meeting", "agenda", "notes", "access", "documents", "settings"):
        assert f'id="tab-{absent}"' not in text, absent
    assert 'id="notes"' not in text and 'id="presence"' not in text and 'id="join"' not in text
    assert 'id="cmd-join"' not in text and 'id="details"' not in text
    assert password not in text and f"<span>{password[4:]}</span>" not in text
    assert "/screen/" not in text and "Autre session confidentielle" not in text
    assert "/entries" not in text and "/close" not in text.replace("/polls/", "")
    assert "Se connecter" not in text


@pytest.mark.parametrize("rights", [["polls"], ["agenda", "notes", "timer", "headcount"]])
def test_the_polls_open_now_in_polls_need_the_polls_right(
    app: FastAPI, world: World, owner: User, rights: list[str]
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    _, token = make_link(world, owner, public_id, rights)
    browser = open_link(app, public_id, token)
    page = browser.get(f"/manage/{public_id}").text
    panel = browser.get(f"/manage/{public_id}/panel").text
    allowed = "polls" in rights
    assert ('id="polls-open"' in page) is allowed
    assert ('data-live-part="polls"' in panel) is allowed
    assert (f"/polls/{poll_id}/close" in page + panel) is allowed
    assert page.count('data-live-part="polls"') == (1 if allowed else 0)  # Steering
    assert page.count('data-live-part="poll-tiles"') == (1 if allowed else 0)  # Polls


@pytest.mark.parametrize("in_session", [False, True])
def test_a_delegate_sees_the_rubrics_of_its_rights(
    app: FastAPI, world: World, owner: User, in_session: bool
) -> None:
    """D-A: the cards of its rights stay in their rubric in every state (the chronometer in
    Projection screen, the headcount in Access and attendance), with nothing else of those
    rubrics. The rubric it lands on says whose link it is, its rights leading to their cards:
    Steering, with the commands, once the session is open; before, when Steering does not
    exist yet, the first rubric of its rights."""
    public_id = world.assembly(owner, in_session=in_session)
    _, token = make_link(world, owner, public_id, ["timer", "headcount"])
    text = open_link(app, public_id, token).get(f"/manage/{public_id}").text
    landing = "tab-cockpit" if in_session else "tab-access"
    home = section_of(text, landing)
    assert f'<section class="rubric" id="{landing}" data-rubric data-default' in home
    assert text.count("data-default") == 1 and text.count('id="delegate-home"') == 1
    assert 'id="delegate-home"' in home
    assert 'href="#timer"' in home and 'href="#presence"' in home
    assert ('id="cmd-timer"' in home) is in_session
    assert ('id="cmd-presence"' in home) is in_session
    assert "Ouvrir la session" not in text and 'id="checklist"' not in text
    for card in ("timer", "presence"):
        assert f'id="{card}"' in text
    rubrics = (
        ["tab-cockpit", "tab-access", "tab-screen"] if in_session else ["tab-access", "tab-screen"]
    )
    assert re.findall(r'<section class="rubric" id="(tab-[a-z]+)"', text) == rubrics
    for owner_card in ("join", "access", "codes", "links", "attendance", "details", "tribune"):
        assert f'id="{owner_card}"' not in text, owner_card
    tabs = re.findall(r'<a href="#(tab-[a-z]+)" data-rubric-link>', text)
    assert tabs == rubrics * 2  # sidebar, then tabs
    assert "<span>Écran</span>" in text and "tab-more" not in text


def test_a_delegate_with_every_right_has_a_tab_per_rubric(
    app: FastAPI, world: World, owner: User
) -> None:
    """Agenda and Session notes are two rubrics, each a tab of its own; Meeting stays the
    owner's (details, tribune); no "More" menu for a delegate."""
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ALL_RIGHTS)
    text = open_link(app, public_id, token).get(f"/manage/{public_id}").text
    rubrics = ["tab-cockpit", "tab-polls", "tab-agenda", "tab-notes", "tab-access", "tab-screen"]
    assert re.findall(r'<section class="rubric" id="(tab-[a-z]+)"', text) == rubrics
    assert re.findall(r'<a href="#(tab-[a-z]+)" data-rubric-link>', text) == rubrics * 2
    assert "<span>Notes</span>" in text and "<span>Notes de séance</span>" in text
    assert "tab-more" not in text and 'id="tab-meeting"' not in text


@pytest.mark.parametrize(
    ("rights", "landing"),
    [
        (["polls", "agenda"], "tab-agenda"),
        (["notes"], "tab-notes"),
        (["polls", "notes"], "tab-notes"),
        (["polls", "timer"], "tab-polls"),
        (["screen"], "tab-screen"),
    ],
)
def test_a_delegate_lands_on_a_rubric_of_its_rights_before_the_opening(
    app: FastAPI, world: World, owner: User, rights: list[str], landing: str
) -> None:
    """Before the opening Steering does not exist: the console lands on the first rubric of the
    link's rights (Meeting is the owner's), which says whose link it is."""
    public_id = world.assembly(owner, in_session=False)
    _, token = make_link(world, owner, public_id, rights)
    text = open_link(app, public_id, token).get(f"/manage/{public_id}").text
    assert 'id="tab-cockpit"' not in text and 'href="#tab-cockpit"' not in text
    assert re.findall(
        r'<section class="rubric" id="(tab-[a-z]+)" data-rubric data-default', text
    ) == [landing]
    home = section_of(text, landing)
    assert text.count('id="delegate-home"') == 1 and 'id="delegate-home"' in home
    assert "La session n'est pas encore ouverte" in home


def test_the_owner_stays_the_owner_with_a_delegate_cookie(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ["timer"])
    post(admin, f"/manage/{public_id}/delegate", {"key": token})
    text = admin.get(f"/manage/{public_id}").text
    assert 'id="tab-settings"' in text and "Accès délégué\u00a0:" not in text


def test_another_account_with_a_link_is_a_delegate(
    app: FastAPI, client: TestClient, world: World, owner: User
) -> None:
    from tests.web.conftest import make_account

    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ["agenda"])
    make_account(world, "claire@example.org")
    log_in(client, "claire@example.org")
    assert client.get(f"/manage/{public_id}").status_code == 404
    post(client, f"/manage/{public_id}/delegate", {"key": token})
    text = client.get(f"/manage/{public_id}").text
    assert 'id="tab-agenda"' in text and 'id="tab-settings"' not in text


# --- Isolation between sessions -----------------------------------------------------------------


def test_a_link_opens_no_other_session(app: FastAPI, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    other = world.assembly(owner, title="Autre session confidentielle")
    _, token = make_link(world, owner, public_id, ALL_RIGHTS)
    browser = TestClient(app, follow_redirects=False)
    cookie = {"cookie": f"{DELEGATE_COOKIE}={token}"}
    for url in (f"/manage/{other}", f"/manage/{other}/panel", f"/manage/{other}/timer"):
        response = browser.get(url, headers=cookie)
        assert response.status_code == 403, url  # the page of an ended link, nothing more
        assert "Autre session confidentielle" not in response.text
    assert post(browser, f"/manage/{other}/delegate", {"key": token}).status_code == 403
    assert browser.get("/dashboard").status_code == 303  # no account


# --- End of a link ------------------------------------------------------------------------------


def test_a_revoked_link_stops_at_once(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    delegate_id, token = make_link(world, owner, public_id, ["timer"])
    browser = open_link(app, public_id, token)
    assert browser.get(f"/manage/{public_id}/panel").status_code == 200
    response = post(admin, f"/manage/{public_id}/delegates/revoke", {"delegate": str(delegate_id)})
    assert response.status_code == 303
    for url in (f"/manage/{public_id}", f"/manage/{public_id}/panel"):
        ended = browser.get(url)
        assert ended.status_code == 403 and "Accès délégué terminé" in ended.text
    assert post(browser, f"/manage/{public_id}/timer/start", {"mode": "up"}).status_code == 403
    left = post(browser, f"/manage/{public_id}/delegate/leave")
    assert left.status_code == 303 and left.headers["location"] == "/"
    assert browser.get(f"/manage/{public_id}").headers["location"].startswith("/login")


def test_closing_the_session_ends_the_links(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ALL_RIGHTS)
    browser = open_link(app, public_id, token)
    post(admin, f"/manage/{public_id}/close")
    assert browser.get(f"/manage/{public_id}").status_code == 403
    assert "ont pris fin" in admin.get(f"/manage/{public_id}").text


def test_taking_a_right_away_applies_at_once(
    app: FastAPI, admin: TestClient, world: World, owner: User, target: dict[str, str]
) -> None:
    public_id = target["public_id"]
    delegate_id, token = make_link(world, owner, public_id, ["polls", "timer"])
    browser = open_link(app, public_id, token)
    assert browser.get(f"/manage/{public_id}/polls/new").status_code == 200
    post(
        admin,
        f"/manage/{public_id}/delegates/edit",
        {"delegate": str(delegate_id), "name": "Claire", "rights": ["timer"]},
    )
    assert browser.get(f"/manage/{public_id}/polls/new").status_code == 404
    assert browser.get(f"/manage/{public_id}/timer").status_code == 200


def test_links_of_another_account_cannot_be_changed(
    client: TestClient, world: World, owner: User
) -> None:
    from tests.web.conftest import make_account

    public_id = world.assembly(owner)
    delegate_id, token = make_link(world, owner, public_id, ["timer"])
    make_account(world, "intruder@example.org")
    log_in(client, "intruder@example.org")
    for action in ("revoke", "edit"):
        response = post(
            client,
            f"/manage/{public_id}/delegates/{action}",
            {"delegate": str(delegate_id), "name": "X", "rights": ["polls"]},
        )
        assert response.status_code == 404
    with world.database.read() as db:
        assert delegates.authenticate(db, public_id, token) is not None


# --- What a delegate does, and the log ----------------------------------------------------------


def test_delegate_actions_are_logged_with_its_name(
    app: FastAPI, admin: TestClient, world: World, owner: User, target: dict[str, str]
) -> None:
    public_id = target["public_id"]
    delegate_id, token = make_link(world, owner, public_id, ["polls"], name="Claire")
    browser = open_link(app, public_id, token)
    response = post(browser, f"/manage/{public_id}/polls/{target['poll_id']}/open")
    assert response.status_code == 303
    with world.database.read() as db:
        poll = db.get(Poll, int(target["poll_id"]))
        assert poll is not None and poll.status is PollStatus.OPEN
        entry = db.scalars(select(AuditEntry).where(AuditEntry.action == "poll.opened")).one()
        assert entry.actor == "delegate" and entry.details["delegate"] == delegate_id
        assert entry.user_id == owner.id
        assert "Claire" not in str(entry.details)
    log = admin.get(f"/manage/{public_id}").text
    assert "par Claire, lien délégué" in log


def test_a_delegate_writes_notes_from_the_script(app: FastAPI, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ["notes"])
    browser = open_link(app, public_id, token)
    response = post(
        browser,
        f"/manage/{public_id}/notes",
        {"text": "Le quorum est atteint."},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200 and "Le quorum est atteint." in response.text
    assert "Le quorum est atteint." in browser.get(f"/manage/{public_id}/notes").text


def test_the_headcount_delegate_saves_the_headcount(
    app: FastAPI, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ["headcount"])
    browser = open_link(app, public_id, token)
    assert post(browser, f"/manage/{public_id}/headcount", {"headcount": "42"}).status_code == 303
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None and assembly.headcount == 42
        entry = db.scalars(
            select(AuditEntry).where(AuditEntry.action == "access.headcount_set")
        ).one()
        assert entry.actor == "delegate"
    text = browser.get(f"/manage/{public_id}").text
    assert re.search(r"Comptés dans la salle</dt><dd>42", text)


# --- Cards handed over: a pill on the card, the links in Settings only (R13) -------------------


def card_of(page: str, card_id: str) -> str:
    """The HTML of the card ``card_id``, up to its end."""
    start = page.index(f'id="{card_id}"')
    return page[start : page.index("</section>", start)]


def section_of(page: str, rubric: str) -> str:
    """The rubric ``rubric`` (``tab-…``) of a console page, up to the next rubric."""
    start = page.index(f'<section class="rubric" id="{rubric}"')
    end = page.find('<section class="rubric"', start + 1)
    return page[start : end if end != -1 else page.index("</main>")]


def steering_of(page: str) -> str:
    """The Steering rubric of a console page (once the session is open), up to the next one."""
    return section_of(page, "tab-cockpit")


def pills_of(html: str) -> list[str]:
    """The text of each "Handed over to …" pill, followed by its title when it has one, then
    the link it leads to in Settings."""
    found = []
    pattern = r'<a class="badge delegate" href="#(delegate-\d+)"(?: title="([^"]*)")?>(.*?)</a>'
    for target, title, body in re.findall(pattern, html, re.S):
        text = re.sub(r"<[^>]+>", "", body).strip()
        found.append((f"{text} [{title}]" if title else text) + f" → {target}")
    return found


def hand_overs_of(html: str) -> list[str]:
    """The cards and commands of ``html`` whose head offers "Confier", in order."""
    return re.findall(r'<details class="hand-over" id="([\w-]+)-hand" name="hand-over"', html)


def hand_over_panel(page: str, card: str) -> str:
    """The "Confier" disclosure of ``card``, from its opening tag to its end."""
    start = page.index(f'<details class="hand-over" id="{card}-hand"')
    end = page.index("</a></p>", page.index("Gérer les liens, dans Réglages", start))
    return page[start:end]


def primaries(html: str) -> list[str]:
    """The labels of the primary buttons of ``html``, in order."""
    found = []
    for match in re.finditer(r'<(button|a)\b[^>]*class="([^"]*)"[^>]*>(.*?)</\1>', html, re.S):
        if {"button", "primary"} <= set(match.group(2).split()):
            found.append(" ".join(re.sub(r"<[^>]+>|&[a-z]+;", " ", match.group(3)).split()))
    return found


# The cards of each right on the owner's console, in their rubric in every state, then the
# commands of Steering during the session (D-A).
CARDS = {
    False: {
        "agenda": ["agenda"],
        "notes": ["notes"],
        "polls": ["drafts"],
        "timer": ["timer"],
        "screen": ["screen-now"],
        "headcount": ["presence"],
    },
}
COMMANDS = {
    "agenda": ["agenda-now"],
    "notes": ["cmd-notes"],
    "polls": ["ready"],
    "timer": ["cmd-timer"],
    "screen": [],
    "headcount": ["cmd-presence"],
}
CARDS[True] = {right: cards + COMMANDS[right] for right, cards in CARDS[False].items()}


@pytest.mark.parametrize("in_session", [True, False])
def test_cards_handed_over_name_who_holds_them(
    admin: TestClient, world: World, owner: User, in_session: bool
) -> None:
    public_id = world.assembly(owner, in_session=in_session)
    world.poll(owner, public_id, open_it=False)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
    paul, _ = make_link(world, owner, public_id, ["notes", "timer"], name="Paul Martin, secrétaire")
    claire, _ = make_link(world, owner, public_id, ["timer", "polls"], name="Claire")
    make_link(world, owner, public_id, ["polls"], name="Ali")
    page = admin.get(f"/manage/{public_id}").text
    expected = {
        "agenda": [],
        "screen": [],
        "headcount": [],
        "notes": [f"Confié à Paul Martin, secrétaire → delegate-{paul}"],
        "timer": [
            "Confié à Paul Martin, secrétaire et 1 autre [Paul Martin, secrétaire, Claire]"
            f" → delegate-{paul}"
        ],
        "polls": [f"Confié à Claire et 1 autre [Claire, Ali] → delegate-{claire}"],
    }
    cards = CARDS[in_session]
    for right, card_ids in cards.items():
        for card_id in card_ids:
            assert pills_of(card_of(page, card_id)) == expected[right], (right, card_id)
    held = ("notes", "timer", "polls")
    assert len(pills_of(page)) == sum(len(cards[right]) for right in held)  # nowhere else
    # Every card and command of a right offers "Confier", next to the pill when held (D-G).
    for right, card_ids in cards.items():
        for card_id in card_ids:
            assert hand_overs_of(card_of(page, card_id)) == [card_id], (right, card_id)
    assert sorted(hand_overs_of(page)) == sorted(card for ids in cards.values() for card in ids)


@pytest.mark.parametrize("in_session", [True, False])
def test_each_card_hands_its_right_over_in_place(
    admin: TestClient, world: World, owner: User, in_session: bool
) -> None:
    """D-G as the owner changed it: one compact "Confier" button in the head of each card of a
    right, and of its command in Steering, opens in the card a new link form: a name, the
    card's right ticked, the other rights folded, and only a way to Settings to manage them."""
    public_id = world.assembly(owner, in_session=in_session)
    world.poll(owner, public_id, open_it=False)
    page = admin.get(f"/manage/{public_id}").text
    assert "card-share" not in page and "cockpit-foot" not in page
    card = "cmd-timer" if in_session else "timer"
    panel = hand_over_panel(page, card)
    # A toggle, closed, one open at a time on the page (the details share their name).
    assert panel.startswith(
        f'<details class="hand-over" id="{card}-hand" name="hand-over" data-menu>'
    )
    assert 'aria-label="Confier le chronomètre à une personne"' in panel
    assert "<span>Confier</span></summary>" in panel
    assert f'action="/manage/{public_id}/delegates#{card}"' in panel
    assert f'name="card" value="{card}"' in panel
    assert f'id="{card}-hand-name" name="name"' in panel
    assert f'id="{card}-hand-timer" name="rights" value="timer" checked>' in panel
    folded = panel[panel.index('<details class="hand-over-more">') :]
    for right in ("agenda", "notes", "polls", "headcount"):
        assert f'name="rights" value="{right}">' in folded, right
    assert re.findall(r'<a href="([^"]+)"', panel) == ["#delegates"]  # Settings, nothing else
    assert "Gérer les liens, dans Réglages" in panel
    if in_session:
        assert card_of(steering_of(page), "cmd-timer").count('class="hand-over"') == 1


def test_a_link_from_a_card_comes_back_to_that_card(
    admin: TestClient, world: World, owner: User
) -> None:
    """Without script, the new link comes back to the card it was made from, in Steering,
    open on its address, copy button and QR code; it is also in Settings, with the pill."""
    public_id = world.assembly(owner)
    fields = {"card": "cmd-timer", "name": "Ali, scrutateur", "rights": ["timer"]}
    response = post(admin, f"/manage/{public_id}/delegates", fields)
    with world.database.read() as db:
        (delegate,) = delegates.list_delegates(db, owner, public_id)
        token = delegates.link_token(world.keyring, delegate)
    place = f"?hand=cmd-timer&link={delegate.id}#cmd-timer"
    assert response.headers["location"] == f"/manage/{public_id}{place}"
    assert delegate.rights == ["timer"]
    page = admin.get(response.headers["location"]).text
    panel = hand_over_panel(page, "cmd-timer")
    opened = '<details class="hand-over" id="cmd-timer-hand" name="hand-over" data-menu open>'
    assert panel.startswith(opened)
    assert f"/manage/{public_id}/delegate#k={token}" in panel and '<div class="qr"' in panel
    assert 'data-copy="cmd-timer-hand-url"' in panel
    assert "Lien créé pour Ali, scrutateur." in panel
    # "Fermer", for manage.js only, and the form kept under the link, hidden, for the next person.
    assert "data-hand-over-close hidden>Fermer</button>" in panel
    assert "data-hand-over-form hidden>" in panel and 'id="cmd-timer-hand-name"' in panel
    command = card_of(steering_of(page), "cmd-timer")
    assert pills_of(command) == [f"Confié à Ali, scrutateur → delegate-{delegate.id}"]
    assert " open>" not in hand_over_panel(page, "timer")  # the other copy stays closed
    assert f'id="delegate-{delegate.id}"' in card_of(page, "delegates")

    # An error comes back to the card, its form open again; nothing is created.
    response = post(admin, f"/manage/{public_id}/delegates", {"card": "presence", "name": ""})
    assert response.headers["location"] == f"/manage/{public_id}?hand=presence#presence"
    page = admin.get(response.headers["location"]).text
    assert "Indiquez le nom" in page
    assert hand_over_panel(page, "presence").startswith(
        '<details class="hand-over" id="presence-hand" name="hand-over" data-menu open>'
    )
    # A card the console does not know is Settings; the query parameters read nothing else.
    response = post(admin, f"/manage/{public_id}/delegates", {**fields, "card": "settings"})
    assert re.fullmatch(rf"/manage/{public_id}#delegate-\d+-link", response.headers["location"])
    page = admin.get(f"/manage/{public_id}?hand=nowhere&link={delegate.id}").text
    assert " data-menu open>" not in page


def test_a_link_from_a_card_with_the_script(admin: TestClient, world: World, owner: User) -> None:
    """For manage.js: the answer is the delegation of every card, the one sent from open on the
    new link, and the Settings card; an error is its message, status 400."""
    public_id = world.assembly(owner)
    fetch = {"X-Requested-With": "fetch"}
    fields = {"card": "ready", "name": "Claire", "rights": ["polls", "notes"]}
    response = post(admin, f"/manage/{public_id}/delegates", fields, headers=fetch)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    html = response.text
    assert "<html" not in html
    slots = re.findall(r'<div class="hand-over-slot" data-hand-over="([\w-]+)">', html)
    assert sorted(slots) == sorted(delegates_web.HAND_OVER_CARDS)
    assert hand_over_panel(html, "ready").startswith(
        '<details class="hand-over" id="ready-hand" name="hand-over" data-menu open>'
    )
    assert "Lien créé pour Claire." in hand_over_panel(html, "ready")
    assert html.count("Confié à Claire") == 4  # polls and notes, card and command each
    assert 'id="delegates"' in html and "Claire" in card_of(html, "delegates")
    with world.database.read() as db:
        assert [d.name for d in delegates.list_delegates(db, owner, public_id)] == ["Claire"]
    error = post(
        admin, f"/manage/{public_id}/delegates", {"card": "ready", "name": "X"}, headers=fetch
    )
    assert error.status_code == 400 and error.text.startswith("Cochez au moins")
    # The page's flash notice was neither set nor consumed by the script's answers.
    assert "Lien créé" not in admin.get(f"/manage/{public_id}").text


def test_settings_manage_every_link(admin: TestClient, world: World, owner: User) -> None:
    """The card of Settings creates a link with any rights, shows each link with its address
    and QR code, changes its name and rights, and revokes it, with nothing folded (the owner's
    request, 2026-11-06): the "+" of its head leads to the tile of a new link, folded until it
    is the target of the address, and shown from the start while there is no link."""
    public_id = world.assembly(owner)
    empty = card_of(admin.get(f"/manage/{public_id}").text, "delegates")
    assert 'class="delegate-item delegate-new" id="delegates-new"' in empty
    assert 'href="#delegates-new"' not in empty and "Annuler" not in empty
    delegate_id, token = make_link(world, owner, public_id, ["notes", "timer"], name="Paul")
    card = card_of(admin.get(f"/manage/{public_id}").text, "delegates")
    assert card.count("<details") == 1 and '<details class="help">' in card  # "En savoir plus"
    add = card[card.index('class="button small tip delegates-add"') :]
    assert add.startswith('class="button small tip delegates-add" href="#delegates-new"')
    assert 'aria-label="Nouveau lien délégué"' in add.split(">")[0]
    assert 'class="delegate-item delegate-new is-folded" id="delegates-new"' in card
    assert '<a class="button quiet" href="#delegates">Annuler</a>' in card
    create = card[card.index('id="delegates-new"') :]
    assert f'action="/manage/{public_id}/delegates"' in create
    assert 'id="delegates-new-name" name="name"' in create
    for right in ALL_RIGHTS:
        assert f'name="rights" value="{right}">' in create, right
    assert f'id="delegate-{delegate_id}"' in card
    assert f"/manage/{public_id}/delegate#k={token}" in card and '<div class="qr"' in card
    edit = card[card.index(f'/delegates/edit#delegate-{delegate_id}"') :]
    assert f'id="delegate-{delegate_id}-edit"' in edit.split(">")[0]
    assert f'name="delegate" value="{delegate_id}"' in edit and 'value="Paul"' in edit
    assert f'type="submit" class="button small" form="delegate-{delegate_id}-edit">' in edit
    for right in ALL_RIGHTS:
        checked = " checked" if right in ("notes", "timer") else ""
        assert f'name="rights" value="{right}"{checked}>' in edit, right
    revoke = card[card.index(f'action="/manage/{public_id}/delegates/revoke#delegates"') :]
    assert f'name="delegate" value="{delegate_id}"' in revoke

    response = post(admin, f"/manage/{public_id}/delegates", {"name": "Ali", "rights": ["polls"]})
    with world.database.read() as db:
        created = [d for d in delegates.list_delegates(db, owner, public_id) if d.name == "Ali"]
    assert len(created) == 1 and created[0].rights == ["polls"]
    assert response.headers["location"] == f"/manage/{public_id}#delegate-{created[0].id}-link"


def test_a_card_field_hands_no_right_alone(admin: TestClient, world: World, owner: User) -> None:
    """The ``card`` field only says where to come back: a link needs its rights ticked."""
    public_id = world.assembly(owner)
    for card, place in (("timer", "?hand=timer#timer"), ("settings", "#delegates-new")):
        response = post(admin, f"/manage/{public_id}/delegates", {"card": card, "name": "X"})
        assert response.status_code == 303
        assert response.headers["location"] == f"/manage/{public_id}{place}"
    assert "Cochez au moins" in admin.get(f"/manage/{public_id}").text
    with world.database.read() as db:
        assert delegates.list_delegates(db, owner, public_id) == []


def test_delegates_and_closed_sessions_get_no_pill(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    _, token = make_link(world, owner, public_id, ALL_RIGHTS)
    page = open_link(app, public_id, token).get(f"/manage/{public_id}").text
    assert "badge delegate" not in page and "cockpit-foot" not in page
    assert 'href="#delegates"' not in page and "hand-over" not in page  # the owner's only
    assert "badge delegate" in admin.get(f"/manage/{public_id}").text
    post(admin, f"/manage/{public_id}/close")
    closed = admin.get(f"/manage/{public_id}").text
    assert "badge delegate" not in closed and "hand-over" not in closed


@pytest.mark.parametrize(
    ("rights", "expected"),
    [
        (["timer"], ["Démarrer"]),  # the chronometer alone: its button leads
        (["headcount"], ["Enregistrer"]),
        (["notes"], ["Ajouter"]),
        (["timer", "headcount"], []),  # two tools, no step of the session: neither leads
        (["timer", "polls"], ["Lancer le vote"]),  # the first draft leads, the tool does not
        (["agenda", "notes"], ["Suivant"]),
        (ALL_RIGHTS, ["Suivant", "Lancer le vote"]),
    ],
)
def test_a_delegate_console_keeps_one_primary_per_card(
    app: FastAPI, world: World, owner: User, rights: list[str], expected: list[str]
) -> None:
    public_id = world.assembly(owner)
    world.poll(owner, public_id, open_it=False)
    world.poll(owner, public_id, open_it=False)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
    _, token = make_link(world, owner, public_id, rights)
    browser = open_link(app, public_id, token)
    page = browser.get(f"/manage/{public_id}").text
    assert primaries(steering_of(page)) == expected
    if "timer" in rights:
        # The card alone, for timer.js, has the buttons of the page: nothing to replace.
        status = re.compile(r"<div data-timer-status>(.*)</div>\s*<script", re.S)
        on_page = status.search(card_of(page, "timer"))
        alone = status.search(browser.get(f"/manage/{public_id}/timer").text)
        assert on_page and alone
        assert primaries(on_page.group(1)) == primaries(alone.group(1))


@pytest.mark.parametrize("in_session", [True, False])
def test_notes_and_agenda_are_handed_over_apart(
    app: FastAPI, world: World, owner: User, in_session: bool
) -> None:
    """The secretary takes notes without moving the item under discussion, and the other way
    round."""
    public_id = world.assembly(owner, in_session=in_session)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
    _, notes_token = make_link(world, owner, public_id, ["notes"], name="Paul")
    secretary = open_link(app, public_id, notes_token)
    page = secretary.get(f"/manage/{public_id}").text
    assert 'id="notes"' in page and 'id="agenda-items"' not in page
    assert "/agenda/current" not in page and 'action="/manage/' + public_id + "/agenda" not in page
    assert secretary.get(f"/manage/{public_id}/agenda").status_code == 404
    assert 'id="tab-notes"' in page  # the notes in their rubric in every state (D-A)
    assert 'id="tab-agenda"' not in page and 'id="tab-meeting"' not in page
    assert 'id="details"' not in page and 'id="tribune"' not in page  # the owner's (D-B)
    item = post(secretary, f"/manage/{public_id}/agenda/current", {"item": "next", "seen": ""})
    assert item.status_code == 404
    assert post(secretary, f"/manage/{public_id}/notes", {"text": "Ouverture"}).status_code == 303

    _, agenda_token = make_link(world, owner, public_id, ["agenda"], name="Marie")
    chair = open_link(app, public_id, agenda_token)
    page = chair.get(f"/manage/{public_id}").text
    assert 'id="tab-agenda"' in page and 'id="notes"' not in page and "Ouverture" not in page
    assert 'id="tab-notes"' not in page and 'id="tab-meeting"' not in page
    assert 'id="details"' not in page and 'id="tribune"' not in page
    assert 'id="agenda-items"' in page and 'id="agenda-current-item"' in page  # the agenda card
    assert "Rapport moral\nBudget</textarea>" in chair.get(f"/manage/{public_id}/agenda").text
    assert chair.get(f"/manage/{public_id}/notes").status_code == 404
    assert post(chair, f"/manage/{public_id}/notes", {"text": "x"}).status_code == 404


def test_no_hand_over_once_the_links_are_full(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    for number in range(delegates.MAX_DELEGATES):
        make_link(world, owner, public_id, ["polls"], name=f"Personne {number}")
    page = admin.get(f"/manage/{public_id}").text
    assert 'class="hand-over"' not in page and 'id="delegates-new"' not in page
    assert "badge delegate" in page  # the pills stay


def test_the_last_link_from_a_card_shows_without_a_form(
    admin: TestClient, world: World, owner: User
) -> None:
    """The link that fills the links of the session still shows in the card it was made from,
    with no form under it: the links are at their maximum."""
    public_id = world.assembly(owner)
    for number in range(delegates.MAX_DELEGATES - 1):
        make_link(world, owner, public_id, ["polls"], name=f"Personne {number}")
    fields = {"card": "timer", "name": "Ali", "rights": ["timer"]}
    response = post(admin, f"/manage/{public_id}/delegates", fields)
    page = admin.get(response.headers["location"]).text
    panel = hand_over_panel(page, "timer")
    assert "Lien créé pour Ali." in panel and "data-hand-over-form" not in panel
    assert page.count('class="hand-over"') == 1  # the other cards offer "Confier" no more
