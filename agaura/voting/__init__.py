"""Tallying engine: pure functions, no I/O.

Typical use::

    spec = PollSpec(method="single_choice", options=(1, 2), params={"adopt_option": 1})
    content = validate_ballot(spec, {"choice": 1})       # when a ballot is submitted
    result = tally(spec, [Ballot(content), Ballot()])    # when the poll closes
    to_jsonable(result)                                  # for storage and exports

Ballot formats and parameters are documented in each method's module.
"""

from agaura.voting.base import (
    Ballot,
    Counts,
    InvalidBallotError,
    InvalidSpecError,
    OptionId,
    Quota,
    Ranking,
    Status,
    VotingError,
    to_jsonable,
)
from agaura.voting.engine import METHODS, PollResult, PollSpec, check_spec, tally, validate_ballot

__all__ = [
    "METHODS",
    "Ballot",
    "Counts",
    "InvalidBallotError",
    "InvalidSpecError",
    "OptionId",
    "PollResult",
    "PollSpec",
    "Quota",
    "Ranking",
    "Status",
    "VotingError",
    "check_spec",
    "tally",
    "to_jsonable",
    "validate_ballot",
]
