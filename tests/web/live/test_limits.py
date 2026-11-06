"""Limits against guessing and scanning on the voter side (FR-25, SPEC §9.3,
``agaura.web.limits``): a session under attack stays open to voters with the right secret,
scanning session IDs stops without stopping a room behind one address from joining, IPv6
addresses count by /64, the tribune is warned."""

from __future__ import annotations

import re
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import AccessMode, User
from agaura.security import new_session_password
from agaura.services import access, assemblies
from agaura.services.errors import AccessDeniedError
from agaura.web import limits
from agaura.web.limits import (
    JOIN_ALERT,
    JOIN_FAILURES_IN_LOCKDOWN,
    JOIN_FAILURES_PER_IP,
    JOIN_FAILURES_PER_SESSION,
    UNKNOWN_IDS_PER_IP,
    WINDOW,
    join_session_key,
    limit_address,
)
from agaura.web.live.routes import VOTER_COOKIE
from agaura.web.security import RateLimiter
from tests.conftest import World
from tests.web.conftest import log_in, post

UNKNOWN_IDS = [f"{n:05d}A" for n in range(UNKNOWN_IDS_PER_IP[0] + 5)]  # well-formed, missing
UNKNOWN_KEY = "unknown-id:testclient"
JOIN_IP_KEY = "join-ip:testclient"
TITLES = ("association", "AG secrète", "AG à codes", "AG close", "AG verrouillée")
GENERIC_ERROR = "Impossible de rejoindre la session."


@pytest.fixture
def published(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    events: list[tuple[str, str, str]] = []
    original = app.state.hub.publish

    def record(channel: str, event: str, data: str = "") -> None:
        events.append((channel, event, data))
        original(channel, event, data)

    monkeypatch.setattr(app.state.hub, "publish", record)
    return events


def attack(app: FastAPI, session_id: str, failures: int) -> None:
    """Failures of other addresses on the session."""
    for _ in range(failures):
        app.state.limiter.record(join_session_key(session_id))


def test_session_under_attack_stays_open_to_the_right_password(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    attack(app, session_id, JOIN_FAILURES_PER_SESSION[0])
    password = world.password(owner, session_id)

    # A voter arriving with the password (QR code or typed) joins.
    response = post(client, f"/{session_id}/join", {"secret": password})
    assert response.status_code == 303 and VOTER_COOKIE in client.cookies

    # An address that keeps failing on this session gets a few tries, then nothing.
    other = TestClient(app, follow_redirects=False)
    other.get("/")
    for _ in range(JOIN_FAILURES_IN_LOCKDOWN[0]):
        assert post(other, f"/{session_id}/join", {"secret": "00000000"}).status_code == 400
    blocked = post(other, f"/{session_id}/join", {"secret": password})
    assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text
    assert VOTER_COOKIE not in other.cookies


def test_without_attack_a_few_typos_never_block(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    for _ in range(JOIN_FAILURES_IN_LOCKDOWN[0] + 2):
        assert post(client, f"/{session_id}/join", {"secret": "12345678"}).status_code == 400
    response = post(client, f"/{session_id}/join", {"secret": world.password(owner, session_id)})
    assert response.status_code == 303


def test_concurrent_wrong_secrets_stop_at_the_limit(
    app: FastAPI, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked and counted in one step: wrong secrets sent at once from one address cannot all
    pass the check before any of them is counted."""
    monkeypatch.setattr(limits, "JOIN_FAILURES_PER_IP", (10, WINDOW))
    clients = [TestClient(app, follow_redirects=False) for _ in range(30)]
    for each in clients:
        each.get("/")  # CSRF cookie
    barrier = threading.Barrier(len(clients))
    statuses: list[int] = []

    def attempt(each: TestClient) -> None:
        barrier.wait()
        statuses.append(post(each, f"/{session_id}/join", {"secret": "00000000"}).status_code)

    threads = [threading.Thread(target=attempt, args=(each,)) for each in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [400] * 10 + [429] * 20


def test_a_successful_join_is_not_counted_as_a_failure(
    app: FastAPI, world: World, owner: User, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "JOIN_FAILURES_PER_IP", (3, WINDOW))
    typist = TestClient(app, follow_redirects=False)
    typist.get("/")
    for _ in range(2):
        assert post(typist, f"/{session_id}/join", {"secret": "00000000"}).status_code == 400
    voter = TestClient(app, follow_redirects=False)
    voter.get("/")
    password = world.password(owner, session_id)
    assert post(voter, f"/{session_id}/join", {"secret": password}).status_code == 303
    # Still one try left for this address: the successful join was taken back.
    assert post(typist, f"/{session_id}/join", {"secret": "00000000"}).status_code == 400
    assert post(typist, f"/{session_id}/join", {"secret": password}).status_code == 429


def test_tribune_is_warned(
    client: TestClient,
    app: FastAPI,
    world: World,
    owner: User,
    session_id: str,
    published: list[tuple[str, str, str]],
) -> None:
    attack(app, session_id, JOIN_ALERT - 1)
    assert post(client, f"/{session_id}/join", {"secret": "nope"}).status_code == 400
    assert (f"manage:{session_id}", "turnout", "console") in published  # the console refreshes

    tribune = TestClient(app, follow_redirects=False)
    log_in(tribune, owner.email)
    panel = tribune.get(f"/manage/{session_id}/panel").text
    assert 'id="join-alert"' in panel
    assert "30 mots de passe ou codes refusés ces 10 dernières minutes." in panel
    assert "changez le mot de passe" in panel and "désormais refusées" not in panel
    attack(app, session_id, JOIN_FAILURES_PER_SESSION[0])
    page = tribune.get(f"/manage/{session_id}").text
    assert "désormais refusées" in page and "le bon mot de passe ou le bon code" in page


def test_no_warning_below_the_alert(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    attack(app, session_id, JOIN_ALERT - 2)
    tribune = TestClient(app, follow_redirects=False)
    log_in(tribune, owner.email)
    assert 'id="join-alert"' not in tribune.get(f"/manage/{session_id}/panel").text


def scan(client: TestClient) -> None:
    """Ask for as many unknown session IDs as the limit allows: the address is now scanning."""
    for unknown in UNKNOWN_IDS[: UNKNOWN_IDS_PER_IP[0]]:
        assert client.get(f"/{unknown}").status_code == 404
    assert client.get(f"/{UNKNOWN_IDS[-1]}").status_code == 200  # the neutral page


def without_id(text: str, public_id: str) -> str:
    """A page with its session ID taken out: nothing else may depend on the session."""
    return text.replace(public_id, "{ID}")


def sessions_in_every_state(world: World, owner: User) -> dict[str, str]:
    """Sessions in preparation, in codes mode, closed and with locked entries."""
    preparing = world.assembly(owner, in_session=False, title="AG secrète")
    codes = world.assembly(owner, mode=AccessMode.CODES, title="AG à codes")
    closed = world.assembly(owner, title="AG close")
    locked = world.assembly(owner, title="AG verrouillée")
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, closed)
        access.set_entries_locked(db, owner, locked, True)
    return {"preparing": preparing, "codes": codes, "closed": closed, "locked": locked}


def test_scanning_address_gets_one_neutral_page_for_every_id(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    others = sessions_in_every_state(world, owner)
    for path in ("/favicon.ico", "/sitemap.xml", "/wp-login.php"):  # malformed: not counted
        assert client.get(path).status_code == 404
    scan(client)
    pages = set()
    for public_id in (session_id, *others.values(), UNKNOWN_IDS[-2]):
        response = client.get(f"/{public_id}")
        assert response.status_code == 200, public_id
        assert response.headers["cache-control"] == "no-store"
        assert f'action="/{public_id}/join"' in response.text and "data-autojoin" in response.text
        assert "Mot de passe de la session ou code personnel" in response.text
        assert "Vote secret\u00a0: aucun bulletin n" in response.text  # FR-40a, the same for all
        assert not any(title in response.text for title in TITLES)
        assert "commencé" not in response.text and "terminée" not in response.text
        assert "inputmode" not in response.text and "chiffres" not in response.text
        pages.add(without_id(response.text, public_id))
    assert len(pages) == 1
    # Unknown IDs keep counting: the scan goes on getting nothing.
    assert app.state.limiter.count(UNKNOWN_KEY, WINDOW) == UNKNOWN_IDS_PER_IP[0] + 2


def test_scanning_address_other_spellings_redirect_alike(
    client: TestClient, session_id: str
) -> None:
    scan(client)
    for public_id in (session_id, UNKNOWN_IDS[-2]):
        response = client.get(f"/{public_id.lower()}")
        assert response.status_code == 303 and response.headers["location"] == f"/{public_id}"


def test_scanning_address_joins_with_the_right_password(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """A venue behind one address: someone scanned IDs, the room still joins."""
    scan(client)
    response = post(client, f"/{session_id}/join", {"secret": world.password(owner, session_id)})
    assert response.status_code == 303 and response.headers["location"] == f"/{session_id}"
    assert VOTER_COOKIE in client.cookies
    page = client.get(f"/{session_id}")
    assert page.status_code == 200 and "En attente du prochain vote" in page.text
    assert "AG de l&#39;association" in page.text


def test_scanning_address_joins_with_an_individual_code(
    client: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    code = world.codes(owner, public_id, 1)[0]
    scan(client)
    response = post(client, f"/{public_id}/join", {"secret": code.lower()})
    assert response.status_code == 303 and VOTER_COOKIE in client.cookies
    assert "En attente du prochain vote" in client.get(f"/{public_id}").text


def test_scanning_address_gets_one_answer_for_every_failure(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    others = sessions_in_every_state(world, owner)
    scan(client)
    attempts = [
        (session_id, "00000000"),  # wrong password
        (UNKNOWN_IDS[-2], "00000000"),  # unknown ID
        (others["preparing"], world.password(owner, others["preparing"])),  # not open yet
        (others["closed"], "00000000"),
        (others["codes"], "K7M4QX9P"),  # wrong code
        (others["locked"], world.password(owner, others["locked"])),  # entries locked
    ]
    pages = set()
    for public_id, secret in attempts:
        response = post(client, f"/{public_id}/join", {"secret": secret})
        assert response.status_code == 400, public_id
        assert GENERIC_ERROR in response.text and "data-autojoin" in response.text
        assert not any(title in response.text for title in TITLES)
        pages.add(without_id(response.text, public_id))
    assert len(pages) == 1
    assert VOTER_COOKIE not in client.cookies
    limiter = app.state.limiter
    # As without scanning: refused secrets and locked entries count as join failures...
    assert limiter.count(JOIN_IP_KEY, WINDOW) == 3
    assert limiter.count(join_session_key(session_id), WINDOW) == 1
    assert limiter.count(join_session_key(others["preparing"]), WINDOW) == 0
    # ...and an unknown ID as an unknown ID.
    assert limiter.count(UNKNOWN_KEY, WINDOW) == UNKNOWN_IDS_PER_IP[0] + 2


def test_scanning_address_at_the_access_cap_gets_the_neutral_error(
    client: TestClient, app: FastAPI, world: World, owner: User
) -> None:
    """The access cap (FR-27) must not tell a scanning address that the session exists."""
    public_id = world.assembly(owner, registered_count=1, title="AG secrète")  # cap: 1 + 10
    world.voters(owner, public_id, 11)
    scan(client)
    full = post(client, f"/{public_id}/join", {"secret": world.password(owner, public_id)})
    assert full.status_code == 400 and GENERIC_ERROR in full.text
    assert not any(title in full.text for title in TITLES)
    unknown = post(client, f"/{UNKNOWN_IDS[-2]}/join", {"secret": "00000000"})
    assert unknown.status_code == 400
    assert without_id(unknown.text, UNKNOWN_IDS[-2]) == without_id(full.text, public_id)
    assert VOTER_COOKIE not in client.cookies
    assert app.state.limiter.count(join_session_key(public_id), WINDOW) == 0  # not a failure


def test_scanning_address_stays_under_the_session_lockdown(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    password = world.password(owner, session_id)
    attack(app, session_id, JOIN_FAILURES_PER_SESSION[0])
    for _ in range(JOIN_FAILURES_IN_LOCKDOWN[0]):
        assert post(client, f"/{session_id}/join", {"secret": "00000000"}).status_code == 400
    scan(client)
    refused = post(client, f"/{session_id}/join", {"secret": password})
    assert refused.status_code == 400 and GENERIC_ERROR in refused.text
    assert VOTER_COOKIE not in client.cookies
    unknown = post(client, f"/{UNKNOWN_IDS[-2]}/join", {"secret": password})
    assert unknown.status_code == 400
    assert without_id(unknown.text, UNKNOWN_IDS[-2]) == without_id(refused.text, session_id)


def test_scanning_address_over_its_join_limit_is_refused_for_every_id(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    password = world.password(owner, session_id)
    for _ in range(JOIN_FAILURES_PER_IP[0]):
        app.state.limiter.record(JOIN_IP_KEY)
    scan(client)
    pages = set()
    for public_id in (session_id, UNKNOWN_IDS[-2]):
        response = post(client, f"/{public_id}/join", {"secret": password})
        assert response.status_code == 429 and "Trop de tentatives" in response.text
        pages.add(without_id(response.text, public_id))
    assert len(pages) == 1
    assert VOTER_COOKIE not in client.cookies


def test_scanning_address_session_closing_meanwhile(
    client: TestClient,
    world: World,
    owner: User,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def closed_meanwhile(*args: Any) -> None:
        raise AccessDeniedError("session_not_in_session")

    scan(client)
    monkeypatch.setattr(access, "join", closed_meanwhile)
    response = post(client, f"/{session_id}/join", {"secret": world.password(owner, session_id)})
    assert response.status_code == 400 and GENERIC_ERROR in response.text


def test_scanning_address_forgets_a_stale_cookie(client: TestClient, session_id: str) -> None:
    client.cookies.set(VOTER_COOKIE, "forged", domain="testserver.local", path=f"/{session_id}")
    scan(client)
    response = client.get(f"/{session_id}")
    assert response.status_code == 200 and "data-autojoin" in response.text
    assert "association" not in response.text
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith(f'{VOTER_COOKIE}=""') for cookie in cookies)


def test_scanning_never_blocks_a_voter_already_in(
    client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    joined = post(client, f"/{session_id}/join", {"secret": world.password(owner, session_id)})
    assert joined.status_code == 303
    for unknown in UNKNOWN_IDS:
        client.get(f"/{unknown}")
    page = client.get(f"/{session_id}")
    assert page.status_code == 200 and "En attente du prochain vote" in page.text
    again = post(client, f"/{session_id}/join", {"secret": "x"})  # a second scan of the QR code
    assert again.status_code == 303 and again.headers["location"] == f"/{session_id}"
    with world.database.read() as db:
        assert access.attendees(db, assemblies.find_assembly(db, session_id)) == 1


def test_unknown_ids_in_the_join_form_count_too(client: TestClient) -> None:
    client.get("/")
    for unknown in UNKNOWN_IDS[: UNKNOWN_IDS_PER_IP[0]]:
        assert post(client, f"/{unknown}/join", {"secret": "x"}).status_code == 404
    response = client.get(f"/{UNKNOWN_IDS[-1]}")
    assert response.status_code == 200 and "data-autojoin" in response.text


def request_from(address: str) -> Any:
    return SimpleNamespace(client=SimpleNamespace(host=address))


@pytest.mark.parametrize(
    ("address", "counted"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("testclient", "testclient"),
    ],
)
def test_ipv6_addresses_count_by_64(address: str, counted: str) -> None:
    assert limit_address(request_from(address)) == counted


def test_limiter_drops_the_oldest_keys_first() -> None:
    limiter = RateLimiter()
    limiter.MAX_KEYS = 10
    limiter.record("session-under-attack")
    for n in range(9):
        limiter.record(f"address-{n}")
    limiter.record("session-under-attack")  # recorded again: now the most recent
    limiter.record("address-new")  # the eleventh key: the oldest one goes
    assert limiter.count("session-under-attack", 60) == 2
    assert limiter.count("address-0", 60) == 0
    assert limiter.count("address-new", 60) == 1


def test_session_passwords_have_8_digits(admin: TestClient, world: World, owner: User) -> None:
    assert all(re.fullmatch(r"\d{8}", new_session_password()) for _ in range(20))
    public_id = world.assembly(owner)
    post(admin, f"/manage/{public_id}/password", {"password": "1234567"})
    assert "Le mot de passe est trop court." in admin.get(f"/manage/{public_id}").text
    post(admin, f"/manage/{public_id}/password", {"password": "Assemblée 2026"})
    assert world.password(owner, public_id) == "Assemblée 2026"
    with world.database.read() as db:
        assert access.session_password(db, world.keyring, owner, public_id) == "Assemblée 2026"
