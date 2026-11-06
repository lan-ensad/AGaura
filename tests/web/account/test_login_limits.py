"""Failed logins (SPEC §9.3): refused per address and account, never for everyone; past a few
failures on an account from anywhere, the anti-robot check; argon2 a few at a time."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agaura import security
from agaura.models import User
from agaura.security import WorkGate, hash_token
from agaura.web import public
from tests.web.account.conftest import altcha
from tests.web.conftest import PASSWORD, post

WRONG = "not the password"


def client_at(app: FastAPI, address: str) -> TestClient:
    return TestClient(app, follow_redirects=False, client=(address, 50000))


def log_in(client: TestClient, email: str, password: str, *, solve: bool = False) -> Response:
    data = {"email": email, "password": password}
    if solve:
        data["altcha"] = altcha(client)
    return post(client, "/login", data)


def fail(client: TestClient, email: str, times: int) -> None:
    for _ in range(times):
        response = log_in(client, email, WRONG, solve=True)
        assert response.status_code == 400 and "mot de passe incorrect" in response.text


def test_lockout_is_per_address_and_account(app: FastAPI, owner: User) -> None:
    """Failing on purpose from one address never locks the owner out elsewhere."""
    attacker = client_at(app, "198.51.100.7")
    fail(attacker, owner.email, public.LOGIN_FAILURES_PER_PAIR[0])
    blocked = log_in(attacker, owner.email, PASSWORD, solve=True)
    assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text

    make_other = post(attacker, "/login", {"email": "other@example.org", "password": WRONG})
    assert make_other.status_code == 400  # other accounts from this address: still checked

    owner_device = client_at(app, "203.0.113.9")
    first = log_in(owner_device, owner.email, PASSWORD)
    assert first.status_code == 400 and "vérification anti-robot est nécessaire" in first.text
    assert "data-pow" in first.text and 'value="owner@example.org"' in first.text
    response = log_in(owner_device, owner.email, PASSWORD, solve=True)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"


def test_failures_from_many_addresses_ask_for_the_anti_robot_check(
    app: FastAPI, owner: User
) -> None:
    for number in range(public.LOGIN_FAILURES_BEFORE_POW[0]):
        response = post(
            client_at(app, f"198.51.100.{number + 1}"),
            "/login",
            {"email": owner.email, "password": WRONG},
        )
        assert response.status_code == 400
    owner_device = client_at(app, "203.0.113.9")
    # Without the check the password is not even verified, so a guess learns nothing.
    assert log_in(owner_device, owner.email, WRONG).status_code == 400
    assert log_in(owner_device, owner.email, PASSWORD, solve=True).status_code == 303


def answers(app: FastAPI, address: str, email: str) -> list[tuple[int, bool, bool, bool]]:
    """Wrong passwords: 5 without the check, one more (refused for want of it), then with it
    until the address is refused."""
    client = client_at(app, address)
    outcomes = []
    for attempt in range(12):
        response = log_in(client, email, WRONG, solve=attempt > 5)
        outcomes.append(
            (
                response.status_code,
                "data-pow" in response.text,
                "vérification anti-robot est nécessaire" in response.text,
                "Trop de tentatives" in response.text,
            )
        )
    return outcomes


def test_same_answers_whether_or_not_the_account_exists(app: FastAPI, owner: User) -> None:
    known = answers(app, "198.51.100.7", owner.email)
    unknown = answers(app, "198.51.100.8", "nobody@example.org")
    assert known == unknown
    assert {status for status, *_rest in known} == {400, 429}


def test_concurrent_wrong_passwords_stop_at_the_limit(
    app: FastAPI, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks and counts in one step: 40 wrong passwords at once from one address used to all
    get 400, against a limit of 10. Argon2 runs two at a time meanwhile."""
    monkeypatch.setattr(public, "LOGIN_FAILURES_BEFORE_POW", (1000, 900.0))
    monkeypatch.setattr(security, "_password_gate", WorkGate(2, 100, 30.0))
    running = {"now": 0, "peak": 0}
    lock = threading.Lock()
    real = security._password_hasher

    class Tracked:
        def verify(self, password_hash: str, password: str) -> bool:
            with lock:
                running["now"] += 1
                running["peak"] = max(running["peak"], running["now"])
            try:
                return real.verify(password_hash, password)
            finally:
                with lock:
                    running["now"] -= 1

    monkeypatch.setattr(security, "_password_hasher", Tracked())
    clients = [client_at(app, "198.51.100.7") for _ in range(40)]
    for client in clients:
        client.get("/health")  # CSRF cookie
    barrier = threading.Barrier(len(clients))
    statuses: list[int] = []

    def attempt(client: TestClient) -> None:
        barrier.wait()
        response = post(client, "/login", {"email": owner.email, "password": WRONG})
        statuses.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [400] * 10 + [429] * 30
    assert running["peak"] <= 2


def test_busy_password_hashing_answers_503_and_counts_nothing(
    app: FastAPI, client: TestClient, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = WorkGate(1, 0, 0.01)
    monkeypatch.setattr(security, "_password_gate", gate)
    with gate.slot():  # another login holds the only slot
        started = time.monotonic()
        response = log_in(client, owner.email, PASSWORD)
    assert time.monotonic() - started < 2
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert "Serveur occupé" in response.text
    limiter = app.state.limiter
    assert limiter.count("login-ip:testclient", 600) == 0
    email_key = f"login-pair:testclient:{hash_token(owner.email)}"
    assert limiter.count(email_key, 900) == 0
    assert log_in(client, owner.email, PASSWORD).status_code == 303
