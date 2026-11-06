"""Ballot forms (SPEC §6.5): from submitted fields to ballot content, and back.

The same fields travel from the ballot form to the summary (as hidden inputs) and from the
summary to the submission, so every step works as a plain HTML form:

- single choice: ``choice=<position>``, or ``choice=00`` for the blank radio button;
- approval: ``approved=<position>`` repeated; nothing checked is a blank vote (SPEC §6.3);
- Majority Judgment: ``grade-<position>=<grade>`` for each graded option; ungraded options
  receive the worst grade when the ballot is validated (SPEC §6.4);
- Condorcet: ``rank-<position>=<rank>`` for every option, the same rank for options the voter
  puts level; ``00`` (the "Non classée" of the list) leaves the option unranked, after every
  ranked one when the ballot is validated (SPEC §6.5);
- ``blank=1``: the blank box of approval, Majority Judgment and Condorcet ballots, placed after
  the options like the blank radio button of single choice.

Positions and ranks always take two digits (``07``, ``rank-03``; at most 30 options), grades
one (at most 10): a browser without JavaScript sends its fields as they are, and without one
width ``choice=12`` would be a byte longer than ``choice=7`` (SPEC §9.2).

A blank vote excludes every other choice: the script of the page unchecks the others, and
without it the server refuses a blank box sent with options checked or graded (FR-44), giving
the ballot back with those options kept (``Choice.with_blank``).

Options are identified by their position, as in ballots and results.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agaura import voting
from agaura.models import Poll
from agaura.services.errors import InvalidInputError
from agaura.services.results import spec_of

MAX_NUMBER_LENGTH = 3  # positions and ranks (at most 30), grades (at most 10)
BLANK_WITH_CHOICE = "live_blank_with_choice"
NUMBER_WIDTH = 2  # digits of every position and rank a form sends
BLANK_CHOICE = "00"  # the blank radio button of single choice: as long as a position
UNRANKED = "00"  # "Non classée" in a Condorcet list: as long as a rank


def number_field(value: int) -> str:
    """A position or a rank as a form sends it: always ``NUMBER_WIDTH`` digits."""
    return f"{value:0{NUMBER_WIDTH}d}"


def grade_name(position: int) -> str:
    return f"grade-{number_field(position)}"


def rank_name(position: int) -> str:
    return f"rank-{number_field(position)}"


class FormFields(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def getlist(self, key: str) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class Choice:
    """What a voter selected, before confirmation. ``content=None`` is a blank vote."""

    method: str
    content: Mapping[str, Any] | None = None
    implicit_blank: bool = False  # an approval ballot with nothing checked
    with_blank: bool = False  # options chosen and the blank box checked too: refused

    @property
    def blank(self) -> bool:
        return self.content is None

    def chose(self, position: int) -> bool:
        return self.content is not None and self.content.get("choice") == position

    def approves(self, position: int) -> bool:
        return self.content is not None and position in self.content.get("approved", ())

    def grade_of(self, position: int) -> int | None:
        if self.content is None:
            return None
        return self.content.get("grades", {}).get(str(position))

    def ungraded(self, poll: Poll) -> list[str]:
        """Labels of the options a Majority Judgment ballot leaves without a grade."""
        if self.content is None or "grades" not in self.content:
            return []
        return [option.label for option in poll.options if self.grade_of(option.position) is None]

    def rank_of(self, position: int) -> int | None:
        if self.content is None:
            return None
        return self.content.get("ranks", {}).get(str(position))

    def by_rank(self, poll: Poll) -> list[tuple[int | None, Any]]:
        """(rank, option) of a Condorcet ballot, best first, the options left unranked last
        (``None``); options of one rank, and the unranked ones, in the order of the poll."""
        ranked = [(self.rank_of(option.position), option) for option in poll.options]
        return sorted(ranked, key=lambda pair: (pair[0] is None, pair[0] or 0))

    def unranked(self, poll: Poll) -> bool:
        """True when a Condorcet ballot leaves an option without a rank."""
        if self.content is None or "ranks" not in self.content:
            return False
        return any(self.rank_of(option.position) is None for option in poll.options)

    def fields(self, poll: Poll) -> list[tuple[str, str]]:
        """Form fields that reproduce this choice (hidden inputs of the summary), as the ballot
        form sends them: a Condorcet ballot carries every option, ranked or not."""
        if self.content is None:
            if self.implicit_blank:
                return []
            if self.method == "single_choice":
                return [("choice", BLANK_CHOICE)]
            return [("blank", "1")]
        if "choice" in self.content:
            return [("choice", number_field(self.content["choice"]))]
        if "approved" in self.content:
            return [("approved", number_field(position)) for position in self.content["approved"]]
        if "ranks" in self.content:
            return [
                (rank_name(option.position), number_field(rank) if rank else UNRANKED)
                for option in poll.options
                for rank in [self.rank_of(option.position)]
            ]
        return [(grade_name(int(key)), str(grade)) for key, grade in self.content["grades"].items()]


def _number(raw: Any) -> int:
    # ``isdigit`` accepts superscripts and other scripts' digits that ``int`` refuses.
    if (
        not isinstance(raw, str)
        or not (raw.isascii() and raw.isdigit())
        or len(raw) > MAX_NUMBER_LENGTH
    ):
        raise InvalidInputError("malformed_ballot")
    return int(raw)


def parse_choice(poll: Poll, form: FormFields) -> Choice:
    """Read a ballot form; raises ``InvalidInputError`` when nothing usable was selected.

    A blank box sent with options gives a ``with_blank`` choice that keeps the options, so the
    ballot can come back as it was; ``refuse_blank_with_choice`` turns it down.
    """
    method = poll.method
    blank = bool(form.get("blank"))
    if method == "single_choice":
        raw = form.get("choice")
        if raw == BLANK_CHOICE or (blank and not raw):
            return Choice(method)
        if not raw:
            raise InvalidInputError("empty_ballot")
        return Choice(method, {"choice": _number(raw)}, with_blank=blank)
    if method == "approval":
        approved = [_number(raw) for raw in form.getlist("approved")]
        if not approved:
            # Nothing checked counts as a blank vote, after the summary says so (SPEC §6.3).
            return Choice(method) if blank else Choice(method, implicit_blank=True)
        return Choice(method, {"approved": approved}, with_blank=blank)
    if method == "majority_judgment":
        grades: dict[str, int] = {}
        for option in poll.options:
            raw = form.get(grade_name(option.position))
            if raw:
                grades[str(option.position)] = _number(raw)
        if not grades:
            if blank:
                return Choice(method)
            raise InvalidInputError("live_no_grade")
        return Choice(method, {"grades": grades}, with_blank=blank)
    if method == "condorcet":
        ranks: dict[str, int] = {}
        for option in poll.options:
            raw = form.get(rank_name(option.position))
            if raw and raw != UNRANKED:
                ranks[str(option.position)] = _number(raw)
        if not ranks:
            if blank:
                return Choice(method)
            raise InvalidInputError("live_no_rank")
        return Choice(method, {"ranks": ranks}, with_blank=blank)
    raise InvalidInputError("malformed_ballot")


def refuse_blank_with_choice(choice: Choice) -> None:
    """A blank vote excludes every other choice (FR-44): refuse a ballot that has both."""
    if choice.with_blank:
        raise InvalidInputError(BLANK_WITH_CHOICE)


def check_choice(poll: Poll, choice: Choice) -> None:
    """Apply the method's rules before the summary (FR-44); ``cast_vote`` checks them again."""
    refuse_blank_with_choice(choice)
    if choice.content is None:
        return
    try:
        voting.validate_ballot(spec_of(poll), choice.content)
    except voting.InvalidBallotError as error:
        raise InvalidInputError(error.code, error.detail) from error
