"""Voter access: session password, access mode, entry lock, headcount (FR-20, FR-27, FR-28).

The card "Entrées" of Access and attendance (``_entries.html``) locks entries with the same
form as the status bar and explains the access cap (``access_entries``, the owner's only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from agaura.models import AccessMode, Assembly
from agaura.security import Keyring
from agaura.services import access
from agaura.services.accounts import quotas
from agaura.web.deps import CsrfProtected, KeyringDep
from agaura.web.manage.common import (
    ConsoleAccess,
    ConsoleUser,
    HeadcountSession,
    OwnedSession,
    console_context,
    optional_int,
    router,
    run_action,
)


@dataclass(frozen=True, slots=True)
class AccessCap:
    """The access cap of a password session (FR-27), and what it rests on, for the card:
    ``basis`` is "members" (the registered members), "headcount" (the people counted raised
    it), "quota" (no member count: the account's codes quota) or "demo" (FR-09a)."""

    cap: int
    basis: str
    members: int | None
    headcount: int | None


def access_cap_of(assembly: Assembly) -> AccessCap | None:
    """None in codes mode, which has no cap: its codes are bounded already. A demo session
    (FR-09a) has ``access.DEMO_VOTERS`` in either mode, basis "demo"."""
    members, headcount = assembly.registered_count, assembly.headcount
    if assembly.is_demo:
        return AccessCap(cap=access.DEMO_VOTERS, basis="demo", members=members, headcount=headcount)
    if assembly.access_mode is not AccessMode.PASSWORD:
        return None
    cap = access.access_cap(assembly)
    if members is None:
        # The headcount raises the cap above the quota only when it gives more.
        raised = headcount is not None and cap > quotas(assembly.owner).codes_per_assembly
        basis = "headcount" if raised else "quota"
    else:
        # The margin grows with the number of people, so the larger number gives the cap.
        basis = "headcount" if headcount is not None and headcount > members else "members"
    return AccessCap(cap=cap, basis=basis, members=members, headcount=headcount)


@console_context
def access_context(
    request: Request, db: Session, console: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """The access cap for the card "Entrées", the owner's only, like the entry lock."""
    return {"access_entries": access_cap_of(assembly) if console.is_owner else None}


@router.post("/manage/{public_id}/password", dependencies=[CsrfProtected])
def change_password(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    keyring: KeyringDep,
    password: Annotated[str, Form()] = "",
) -> Response:
    raw = password.strip() or None  # empty: generate a new one
    return run_action(
        request,
        public_id,
        lambda db: access.set_session_password(db, keyring, user, public_id, raw),
        notice="password_changed",
        anchor="#access",
    )


@router.post("/manage/{public_id}/mode", dependencies=[CsrfProtected])
def change_mode(
    request: Request, public_id: OwnedSession, user: ConsoleUser, mode: Annotated[str, Form()] = ""
) -> Response:
    new_mode = AccessMode.CODES if mode == "codes" else AccessMode.PASSWORD
    return run_action(
        request,
        public_id,
        lambda db: access.set_access_mode(db, user, public_id, new_mode),
        notice="mode_changed",
        voters="session",
        anchor="#access",
    )


@router.post("/manage/{public_id}/entries", dependencies=[CsrfProtected])
def lock_entries(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    locked: Annotated[str, Form()] = "",
) -> Response:
    lock = locked == "1"
    return run_action(
        request,
        public_id,
        lambda db: access.set_entries_locked(db, user, public_id, lock),
        notice="entries_locked" if lock else "entries_unlocked",
    )


@router.post("/manage/{public_id}/headcount", dependencies=[CsrfProtected])
def set_headcount(
    request: Request,
    public_id: HeadcountSession,
    user: ConsoleUser,
    headcount: Annotated[str, Form()] = "",
) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: access.set_headcount(db, user, public_id, optional_int(headcount)),
        notice="headcount_saved",
        anchor="",  # the card (#presence) and the command (#cmd-presence) carry their own
    )
