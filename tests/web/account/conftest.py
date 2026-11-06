"""Helpers for the accounts area: solved anti-bot challenges, signups, emails sent."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from agaura.models import AuditEntry, OutboxEmail
from agaura.web import pow as proof_of_work
from tests.conftest import World
from tests.web.conftest import PASSWORD, post

LINK = re.compile(r"http://testserver(/(?:verify|reset|account/email)/[A-Za-z0-9_-]+)")


@pytest.fixture(autouse=True)
def easy_proof_of_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof_of_work, "MAX_NUMBER", 2000)


def altcha(client: TestClient) -> str:
    """Fetch a challenge and solve it, like ``pow.js`` does."""
    challenge = client.get("/altcha/challenge").json()
    number = proof_of_work.solve(challenge)
    assert number is not None
    return proof_of_work.encode_solution(challenge, number)


def sign_up(
    client: TestClient, email: str = "new@example.org", password: str = PASSWORD, **fields: Any
) -> Response:
    data = {
        "email": email,
        "password": password,
        "password_confirm": password,
        "display_name": "",
        **fields,
    }
    if "altcha" not in fields:
        data["altcha"] = altcha(client)
    return post(client, "/signup", data)


def emails_to(world: World, address: str) -> list[OutboxEmail]:
    with world.database.read() as db:
        return list(
            db.scalars(
                select(OutboxEmail)
                .where(OutboxEmail.to_address == address)
                .order_by(OutboxEmail.id)
            )
        )


def last_link(world: World, address: str) -> str:
    """Path of the link in the latest email sent to ``address``."""
    sent = emails_to(world, address)
    assert sent, f"no email to {address}"
    match = LINK.search(sent[-1].body_text)
    assert match, sent[-1].body_text
    return match.group(1)


def actions(world: World, user_id: int) -> list[str]:
    with world.database.read() as db:
        return list(
            db.scalars(
                select(AuditEntry.action)
                .where(AuditEntry.user_id == user_id)
                .order_by(AuditEntry.id)
            )
        )


def audit_text(world: World) -> str:
    with world.database.read() as db:
        return " ".join(
            f"{entry.action} {entry.details}" for entry in db.scalars(select(AuditEntry))
        )
