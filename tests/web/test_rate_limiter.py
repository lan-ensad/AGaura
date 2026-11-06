"""The rate limiter under concurrent requests (SPEC §9.3): checks and counts never race."""

from __future__ import annotations

import threading
from collections.abc import Callable

from starlette.requests import Request

from agaura.web import limits
from agaura.web.security import RateLimiter


def run_together(count: int, work: Callable[[int], object]) -> list[object]:
    """Run ``work(index)`` in ``count`` threads released at the same moment."""
    barrier = threading.Barrier(count)
    results: list[object] = [None] * count
    errors: list[BaseException] = []

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = work(index)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    return results


def test_charge_lets_exactly_the_limit_through() -> None:
    limiter = RateLimiter()
    passed = run_together(50, lambda _index: limiter.charge({"k": (10, 60), "other": (100, 60)}))
    assert passed.count(True) == 10
    assert limiter.count("k", 60) == 10 and limiter.count("other", 60) == 10


def test_charge_counts_nothing_when_one_key_is_at_its_limit() -> None:
    limiter = RateLimiter()
    for _ in range(3):
        limiter.record("full")
    assert not limiter.charge({"free": (5, 60), "full": (3, 60)})
    assert limiter.count("free", 60) == 0 and limiter.count("full", 60) == 3


def test_refund_takes_back_one_event() -> None:
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert limiter.charge({"a": (2, 10), "b": (2, 10)})
    now[0] = 1.0
    assert limiter.charge({"a": (2, 10)})
    limiter.refund("a", "b", "missing")
    assert limiter.count("a", 10) == 1 and limiter.count("b", 10) == 0
    now[0] = 10.5  # the remaining event is the first one: it has expired
    assert limiter.count("a", 10) == 0


def test_concurrent_expiry_and_records_never_fail() -> None:
    """Two threads emptying the same key used to delete it twice (KeyError, hence a 500)."""
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])

    def hammer(index: int) -> None:
        for step in range(2000):
            key = f"k{step % 3}"
            if index % 2:
                limiter.record(key)
                now[0] += 0.01
            else:
                limiter.exceeded(key, 5, 0.05)
                limiter.refund(key)

    run_together(8, hammer)


def request_from(address: str) -> Request:
    return Request({"type": "http", "client": (address, 50000), "headers": []})


def test_join_attempts_are_charged_atomically() -> None:
    """What the live join route can call instead of ``join_refused`` then
    ``record_join_failure``: concurrent wrong secrets from one address stop at the limit."""
    limiter = RateLimiter()
    keys = limits.join_keys(request_from("198.51.100.7"), "K7M4Q2")
    limit = limits.JOIN_FAILURES_PER_IP[0]
    passed = run_together(limit + 40, lambda _index: limits.charge_join_attempt(limiter, keys))
    assert passed.count(True) == limit
    assert limits.join_refused(limiter, keys)
    limits.refund_join_attempt(limiter, keys)  # e.g. the right password
    assert not limits.join_refused(limiter, keys)
    assert limits.join_failures(limiter, "K7M4Q2") == limit - 1


def test_join_charge_in_lockdown_refuses_only_addresses_that_failed() -> None:
    limiter = RateLimiter()
    session = limits.join_session_key("K7M4Q2")
    for _ in range(limits.JOIN_FAILURES_PER_SESSION[0]):
        limiter.record(session)
    attacker = limits.join_keys(request_from("198.51.100.7"), "K7M4Q2")
    voter = limits.join_keys(request_from("203.0.113.9"), "K7M4Q2")
    lockdown = limits.JOIN_FAILURES_IN_LOCKDOWN[0]
    passed = run_together(10, lambda _index: limits.charge_join_attempt(limiter, attacker))
    assert passed.count(True) == lockdown
    assert limits.charge_join_attempt(limiter, voter)


def test_join_alert_is_due_at_the_thresholds() -> None:
    limiter = RateLimiter()
    keys = limits.join_keys(request_from("198.51.100.7"), "K7M4Q2")
    for _ in range(limits.JOIN_ALERT - 1):
        limiter.record(keys.session)
    assert not limits.join_alert_due(limiter, keys)
    assert limits.charge_join_attempt(limiter, keys)
    assert limits.join_alert_due(limiter, keys)
