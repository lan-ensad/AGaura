"""Load of an aura battle (SPEC §6.8, NFR-02): many phones tapping at once on a running server.

    uv run uvicorn agaura.main:app                      # in another terminal
    uv run python scripts/aura_load.py --phones 300     # then this

Prepares, in the database of the application settings, a session in session with as many
voter credentials as phones and an aura battle, pushed by the script itself; the phones then
send their batches of taps every ``--every`` seconds through the whole tap phase, while a
screen follows the frames of the battle and one more voter keeps loading the live page. Prints
the latency of the taps and of the live page (median, 95th percentile, maximum), the answers,
the frames the screen received and the ranking stored. Development only.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from dataclasses import dataclass, field

import httpx

from agaura.config import get_settings
from agaura.db import Database
from agaura.models import Poll, User
from agaura.security import Keyring
from agaura.services import access, assemblies, polls
from agaura.services.assemblies import AssemblyInput
from agaura.services.polls import PollInput
from agaura.voting.aura import CHOOSE_SECONDS, GRACE_SECONDS
from agaura.web.security import CSRF_COOKIE, VOTER_COOKIE

CSRF = "load-test-csrf-token-0123456789abcdef"  # 32 characters at least


@dataclass
class Stats:
    taps: list[float] = field(default_factory=list)
    live: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: int = 0
    frames: int = 0
    sent: int = 0


def summary(name: str, values: list[float]) -> str:
    if not values:
        return f"{name}: none"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (
        f"{name}: {len(values)} requests, median {statistics.median(values) * 1000:.0f} ms, "
        f"p95 {p95 * 1000:.0f} ms, max {ordered[-1] * 1000:.0f} ms"
    )


def prepare(phones: int, duration: int) -> tuple[str, list[str], int, str, float, float]:
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("development only")
    database = Database(settings.sqlalchemy_url, audit_key=settings.audit_key)
    keyring = Keyring(settings.secret_key.get_secret_value())
    with database.write() as db:
        owner = User(email=f"load-{time.time_ns()}@agaura.local", password_hash="-")  # noqa: S106
        db.add(owner)
        db.flush()
        assembly = assemblies.create_assembly(
            db,
            keyring,
            owner,
            AssemblyInput(title="Charge battle d'aura", registered_count=phones),
        )
        assemblies.open_assembly(db, owner, assembly.public_id)
        public_id = assembly.public_id
        password = access.session_password(db, keyring, owner, public_id)
        screen = access.screen_token(db, keyring, owner, public_id)
    tokens = []
    for _ in range(phones):
        with database.write() as db:
            tokens.append(access.join(db, keyring, public_id, password).token)
    data = PollInput("Charge", "aura", ["Rouge", "Vert", "Bleu", "Jaune"], {"duration": duration})
    with database.write() as db:
        poll = polls.create_poll(db, owner, public_id, data)
        polls.open_poll(db, owner, poll.id)
        poll_id = poll.id
    with database.read() as db:
        opened = db.get_one(Poll, poll_id).opened_at
    database.dispose()
    if opened is None:
        raise SystemExit("the battle was not pushed")
    start = opened.timestamp() + CHOOSE_SECONDS
    return public_id, tokens, poll_id, screen, start, start + duration


async def phone(
    base: str,
    public_id: str,
    token: str,
    poll_id: int,
    start: float,
    end: float,
    every: float,
    stats: Stats,
    wake: bool,
) -> None:
    cookies = {CSRF_COOKIE: CSRF, VOTER_COOKIE: token}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": CSRF,
        "X-Requested-With": "fetch",
    }
    url = f"{base}/{public_id}/aura/{poll_id}"
    option = random.randint(1, 4)  # noqa: S311 (a simulation, not a secret)
    async with httpx.AsyncClient(cookies=cookies, headers=headers, timeout=10) as client:
        if wake:  # one batch while choosing: the server takes the battle up (409, no tap)
            await client.post(url, content=f"o={option:02d}&n=01")
        spread = random.uniform(0, every)  # noqa: S311
        await asyncio.sleep(max(0.0, start - time.time()) + spread)
        while time.time() < end:
            n = random.randint(3, 7)  # noqa: S311
            began = time.perf_counter()
            try:
                response = await client.post(url, content=f"o={option:02d}&n={n:02d}")
            except httpx.HTTPError:
                stats.errors += 1
            else:
                stats.taps.append(time.perf_counter() - began)
                stats.statuses[response.status_code] = (
                    stats.statuses.get(response.status_code, 0) + 1
                )
                stats.sent += n
            await asyncio.sleep(every)


async def screen(base: str, public_id: str, token: str, end: float, stats: Stats) -> None:
    url = f"{base}/{public_id}/screen/{token}/events"
    try:
        timeout = httpx.Timeout(10, read=None)  # a stream: no limit between two events
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("GET", url) as stream,
        ):
            async for line in stream.aiter_lines():
                if line == "event: aura":
                    stats.frames += 1
                if time.time() > end + 1:
                    return
    except httpx.HTTPError:
        stats.errors += 1


async def watcher(base: str, public_id: str, token: str, end: float, stats: Stats) -> None:
    """Another voter loading the live page meanwhile: the rest of the service stays quick."""
    cookies = {CSRF_COOKIE: CSRF, VOTER_COOKIE: token}
    async with httpx.AsyncClient(cookies=cookies, timeout=10) as client:
        while time.time() < end:
            began = time.perf_counter()
            await client.get(f"{base}/{public_id}/live", headers={"X-Requested-With": "fetch"})
            stats.live.append(time.perf_counter() - began)
            await asyncio.sleep(0.5)


async def run(args: argparse.Namespace) -> None:
    public_id, tokens, poll_id, screen_token, start, end = prepare(args.phones, args.duration)
    print(f"session {public_id}, battle {poll_id}: taps from {start - time.time():.1f} s")
    stats = Stats()
    tasks = [
        phone(args.base, public_id, token, poll_id, start, end, args.every, stats, i == 0)
        for i, token in enumerate(tokens[:-1])
    ]
    tasks.append(screen(args.base, public_id, screen_token, end, stats))
    tasks.append(watcher(args.base, public_id, tokens[-1], end, stats))
    await asyncio.gather(*tasks)
    await asyncio.sleep(GRACE_SECONDS + 1.5)  # the ticker closes the battle
    print(summary("taps", stats.taps))
    print(summary("live page", stats.live))
    print(f"answers {stats.statuses}, network errors {stats.errors}, taps sent {stats.sent}")
    print(f"frames received by the screen: {stats.frames} ({stats.frames / args.duration:.1f}/s)")
    settings = get_settings()
    database = Database(settings.sqlalchemy_url, audit_key=settings.audit_key)
    with database.read() as db:
        poll = db.get_one(Poll, poll_id)
        result = poll.result
        if result is None:
            print(f"poll {poll.status.value}, no result yet")
        else:
            outcome = result.data["outcome"]
            print(
                f"stored: {outcome['taps']} taps from {outcome['phones']} phones, "
                f"winners {outcome['winners']}, interrupted {outcome['interrupted']}"
            )
    database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--phones", type=int, default=300)
    parser.add_argument("--duration", type=int, default=10, choices=(5, 10, 15, 20))
    parser.add_argument("--every", type=float, default=0.5, help="seconds between two batches")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
