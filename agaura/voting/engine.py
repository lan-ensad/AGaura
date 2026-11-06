"""Entry points of the tallying engine: poll definition checks, ballot validation, tallying."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agaura.voting.approval import Approval
from agaura.voting.base import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    Ballot,
    Counts,
    InvalidBallotError,
    InvalidSpecError,
    OptionId,
    is_int,
)
from agaura.voting.condorcet import Condorcet
from agaura.voting.majority_judgment import MajorityJudgment
from agaura.voting.single_choice import SingleChoice


class Method(Protocol):
    name: str

    def parse_params(self, options: tuple[OptionId, ...], raw: Mapping[str, Any]) -> Any: ...

    def validate(
        self, params: Any, options: tuple[OptionId, ...], content: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def tally(
        self,
        params: Any,
        options: tuple[OptionId, ...],
        contents: list[Mapping[str, Any]],
        counts: Counts,
        attendees: int | None,
    ) -> Any: ...


METHODS: dict[str, Method] = {
    method.name: method for method in (SingleChoice(), Approval(), MajorityJudgment(), Condorcet())
}


@dataclass(frozen=True, slots=True)
class PollSpec:
    """What the engine needs to know about a poll."""

    method: str
    options: tuple[OptionId, ...]
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PollResult:
    method: str
    counts: Counts
    outcome: Any  # the method's Outcome dataclass


def _resolve(spec: PollSpec) -> tuple[Method, Any]:
    method = METHODS.get(spec.method)
    if method is None:
        raise InvalidSpecError("unknown_method", spec.method)
    options = spec.options
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise InvalidSpecError("option_count")
    if not all(is_int(option) for option in options) or len(set(options)) != len(options):
        raise InvalidSpecError("invalid_options")
    if not isinstance(spec.params, Mapping):
        raise InvalidSpecError("invalid_params")
    return method, method.parse_params(options, spec.params)


def _validate(method: Method, params: Any, spec: PollSpec, content: Any) -> dict[str, Any]:
    if not isinstance(content, Mapping):
        raise InvalidBallotError("malformed_ballot")
    return method.validate(params, spec.options, content)


def check_spec(spec: PollSpec) -> None:
    """Raise ``InvalidSpecError`` unless the poll definition is usable."""
    _resolve(spec)


def validate_ballot(spec: PollSpec, content: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized content of a non-blank ballot, or raise ``InvalidBallotError``."""
    method, params = _resolve(spec)
    return _validate(method, params, spec, content)


def tally(spec: PollSpec, ballots: Iterable[Ballot], *, attendees: int | None = None) -> PollResult:
    """Count blank and valid ballots, then tally the valid ones with the poll's method.

    Every non-blank ballot is validated again, so a corrupted ballot can never be counted.
    ``attendees`` is required only by single choice polls whose base is ``"attendees"``.
    """
    method, params = _resolve(spec)
    ballots = list(ballots)
    counts = Counts.of(ballots)
    contents = [
        _validate(method, params, spec, ballot.content)
        for ballot in ballots
        if ballot.content is not None
    ]
    outcome = method.tally(params, spec.options, contents, counts, attendees)
    return PollResult(method=spec.method, counts=counts, outcome=outcome)
