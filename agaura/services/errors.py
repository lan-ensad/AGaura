"""Service errors. Each carries a stable ``code`` that the web layer maps to a French message."""

from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class NotFoundError(ServiceError):
    """Missing, or owned by another account: both look the same from outside (FR-07)."""

    def __init__(self, what: str) -> None:
        super().__init__("not_found", what)


class InvalidInputError(ServiceError):
    """Rejected user input (form field, ballot)."""


class InvalidStateError(ServiceError):
    """The action is not allowed in the current state of the session or poll (SPEC §7)."""


class AccessDeniedError(ServiceError):
    """A voter cannot join or act: wrong secret, entries locked, session not in session…"""


class AlreadyVotedError(ServiceError):
    def __init__(self) -> None:
        super().__init__("already_voted")


class QuotaExceededError(ServiceError):
    """An account quota is reached (FR-08)."""
