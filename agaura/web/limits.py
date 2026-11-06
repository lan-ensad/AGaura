"""Limits against guessing and scanning on the voter side (FR-25, SPEC §9.3).

Addresses: the counters of the rate limiter take ``limit_address()``, which groups an IPv6
address by its /64 network (a single connection often owns a whole /64) and keeps an IPv4
address, or an IPv4-mapped IPv6 one, as it is.

Joining a session, with its password or an individual code:

- an address is refused after ``JOIN_FAILURES_PER_IP`` failures in 10 minutes, all sessions
  together (mobile carriers put many people behind one address, hence a generous limit);
- a session under attack (``JOIN_FAILURES_PER_SESSION`` failures in 10 minutes, all addresses
  together) is not closed to everyone: only the addresses that already failed
  ``JOIN_FAILURES_IN_LOCKDOWN`` times on it are refused. A voter arriving with the right password
  (QR code or typed) still joins, and each new address of a botnet gets a few tries only;
- from ``JOIN_ALERT`` failures in 10 minutes, the console of the session warns the tribune, which
  can change the password or lock the entries.

Scanning session IDs: an address that asked for ``UNKNOWN_IDS_PER_IP`` well-formed IDs that do
not exist, in 10 minutes, is scanning. Its devices without a voter credential for the session get
a neutral join form on every session address, existing or not: no title, no state, the same page
for every ID. The right password or code still joins from it, under the join limits above. Every
other outcome (unknown ID, session not in session, wrong secret, locked entries, access cap
reached, the session's lockdown) gets one and the same error, and an address over its own join
limit gets "too many attempts" for every ID, so the answers no longer tell an existing session
from a missing one.

None of these limits counts successful joins: a whole room joins from one address within
minutes, so no limit per address could tell it from a script. A flood of joins with the right
password stops at the access cap of the session instead (``access.access_cap``), which is never
counted as a failure.
Unknown IDs keep counting. Many phones share one address in a venue (Wi-Fi NAT, carrier CGNAT):
a scan, or a few dozen typos of the ID, must never stop the room from joining.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from fastapi import Request

from agaura.web.security import RateLimiter, client_ip

WINDOW = 600.0  # seconds
JOIN_FAILURES_PER_IP = (60, WINDOW)
JOIN_FAILURES_PER_SESSION = (300, WINDOW)
JOIN_FAILURES_IN_LOCKDOWN = (3, WINDOW)
JOIN_ALERT = 30
UNKNOWN_IDS_PER_IP = (40, WINDOW)


def limit_address(request: Request) -> str:
    """The client address as counted by the rate limiter: IPv6 grouped by /64."""
    address = client_ip(request)
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{ip}/64", strict=False))
    return str(ip)


@dataclass(frozen=True)
class JoinKeys:
    address: str  # every session
    session: str  # every address
    pair: str  # this address on this session


def join_keys(request: Request, public_id: str) -> JoinKeys:
    address = limit_address(request)
    return JoinKeys(
        address=f"join-ip:{address}",
        session=join_session_key(public_id),
        pair=f"join-pair:{public_id}:{address}",
    )


def join_session_key(public_id: str) -> str:
    return f"join-session:{public_id}"


def address_refused(limiter: RateLimiter, keys: JoinKeys) -> bool:
    """Too many failures from this address, all sessions together."""
    return limiter.exceeded(keys.address, *JOIN_FAILURES_PER_IP)


def join_refused(limiter: RateLimiter, keys: JoinKeys) -> bool:
    if address_refused(limiter, keys):
        return True
    if limiter.exceeded(keys.session, *JOIN_FAILURES_PER_SESSION):
        return limiter.exceeded(keys.pair, *JOIN_FAILURES_IN_LOCKDOWN)
    return False


def record_join_failure(limiter: RateLimiter, keys: JoinKeys) -> bool:
    """Count a refused password or code; True when the session's count reaches the alert or the
    lockdown threshold, so the console of the session refreshes at once."""
    for key in (keys.address, keys.session, keys.pair):
        limiter.record(key)
    return join_alert_due(limiter, keys)


def charge_join_attempt(limiter: RateLimiter, keys: JoinKeys) -> bool:
    """``join_refused`` and the counting of the attempt in one step: False when refused, with
    nothing counted; otherwise the attempt counts as a failure until ``refund_join_attempt``.

    Check-then-``record_join_failure`` lets concurrent wrong secrets all pass the check before
    any of them is counted; charging before the secret is checked does not.
    """
    with limiter.atomic():
        if join_refused(limiter, keys):
            return False
        for key in (keys.address, keys.session, keys.pair):
            limiter.record(key)
        return True


def refund_join_attempt(limiter: RateLimiter, keys: JoinKeys) -> None:
    """Take back a charged attempt that was not a failure: the right secret, the access cap,
    a session that left the in-session state meanwhile."""
    limiter.refund(keys.address, keys.session, keys.pair)


def join_alert_due(limiter: RateLimiter, keys: JoinKeys) -> bool:
    """After a counted failure: True when the session's count is at the alert or the lockdown
    threshold, so the console of the session refreshes at once."""
    count = limiter.count(keys.session, WINDOW)
    return count in (JOIN_ALERT, JOIN_FAILURES_PER_SESSION[0])


def join_failures(limiter: RateLimiter, public_id: str) -> int:
    """Refused passwords or codes on a session in the last 10 minutes."""
    return limiter.count(join_session_key(public_id), WINDOW)


def _unknown_key(request: Request) -> str:
    return f"unknown-id:{limit_address(request)}"


def scanning(limiter: RateLimiter, request: Request) -> bool:
    return limiter.exceeded(_unknown_key(request), *UNKNOWN_IDS_PER_IP)


def record_unknown_id(limiter: RateLimiter, request: Request) -> None:
    limiter.record(_unknown_key(request))
