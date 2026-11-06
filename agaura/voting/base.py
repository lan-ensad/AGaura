"""Shared types and helpers of the tallying engine.

Conventions used across all methods:

- Options are identified by integer ids (``OptionId``), normally database ids.
- Shares and thresholds are exact ``Fraction`` values; floats never enter a tally.
- A ranking is a tuple of groups, best first; options in the same group are tied.
- Ties are reported (``Status.TIE``), never broken.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from itertools import groupby
from typing import Any

OptionId = int
Ranking = tuple[tuple[OptionId, ...], ...]

MIN_OPTIONS = 2
MAX_OPTIONS = 30


class VotingError(ValueError):
    """Base error. ``code`` is a stable identifier that the UI maps to a translated message."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class InvalidSpecError(VotingError):
    """The poll definition (options, parameters or tally context) is invalid."""


class InvalidBallotError(VotingError):
    """A ballot does not follow the rules of the poll's method."""


class Status(enum.StrEnum):
    """Overall outcome of a tally."""

    DECIDED = "decided"
    TIE = "tie"
    THRESHOLD_NOT_MET = "threshold_not_met"
    SEATS_UNFILLED = "seats_unfilled"
    NO_VALID_VOTES = "no_valid_votes"


@dataclass(frozen=True, slots=True)
class Ballot:
    """A submitted ballot. ``content`` is ``None`` for a blank ballot."""

    content: Mapping[str, Any] | None = None

    @property
    def is_blank(self) -> bool:
        return self.content is None


@dataclass(frozen=True, slots=True)
class Counts:
    """Ballot counts shared by every method: ``valid = cast - blank``."""

    cast: int
    blank: int
    valid: int

    @classmethod
    def of(cls, ballots: Iterable[Ballot]) -> Counts:
        cast = blank = 0
        for ballot in ballots:
            cast += 1
            blank += ballot.is_blank
        return cls(cast=cast, blank=blank, valid=cast - blank)


@dataclass(frozen=True, slots=True)
class Quota:
    """Share threshold: met when ``count / base > fraction`` (strict) or ``>= fraction``."""

    fraction: Fraction
    strict: bool

    def is_met(self, count: int, base: int) -> bool:
        if base <= 0:
            return False
        share = Fraction(count, base)
        return share > self.fraction if self.strict else share >= self.fraction


def parse_quota(raw: Any, param: str) -> Quota:
    """Parse ``{"fraction": "2/3", "strict": false}`` into a ``Quota``.

    The fraction is a string (``"2/3"``) or an integer, lies in (0, 1], and a strict quota
    cannot be 1 (it could never be met).
    """
    if not isinstance(raw, Mapping):
        raise InvalidSpecError("invalid_param", param)
    check_keys(raw, required={"fraction"}, optional={"strict"}, what=param)
    strict = raw.get("strict", False)
    if not isinstance(strict, bool):
        raise InvalidSpecError("invalid_param", f"{param}.strict")
    value = raw["fraction"]
    # Floats are refused: 0.1 has no exact binary representation.
    if not (isinstance(value, str) or is_int(value)):
        raise InvalidSpecError("invalid_param", f"{param}.fraction")
    try:
        fraction = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise InvalidSpecError("invalid_param", f"{param}.fraction") from None
    if not 0 < fraction <= 1 or (strict and fraction == 1):
        raise InvalidSpecError("invalid_param", f"{param}.fraction")
    return Quota(fraction=fraction, strict=strict)


def check_keys(
    raw: Mapping[str, Any], *, required: set[str], optional: set[str], what: str
) -> None:
    """Reject missing or unknown keys, so that typos never pass silently."""
    keys = set(raw)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise InvalidSpecError("missing_param", f"{what}: {', '.join(sorted(missing))}")
    if unknown:
        raise InvalidSpecError("unknown_param", f"{what}: {', '.join(sorted(unknown))}")


def is_int(value: Any) -> bool:
    """True for real integers only (``bool`` is excluded although it subclasses ``int``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def option_from(value: Any, options: frozenset[OptionId]) -> OptionId | None:
    """Return the option id designated by ``value`` (int, or digit string as in JSON keys)."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if is_int(value) and value in options:
        return value
    return None


def share(count: int, base: int) -> Fraction | None:
    return Fraction(count, base) if base > 0 else None


def rank_by(options: Iterable[OptionId], key: Callable[[OptionId], Hashable]) -> Ranking:
    """Group options by ascending ``key``; equal keys form one tied group (input order kept)."""
    ordered = sorted(options, key=key)  # sorted() is stable
    return tuple(tuple(group) for _, group in groupby(ordered, key=key))


def to_jsonable(value: Any) -> Any:
    """Convert results (dataclasses, enums, fractions, tuples) into JSON-compatible values.

    Fractions become ``"numerator/denominator"`` strings so that exports stay exact.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value
