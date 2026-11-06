import enum
import json
from dataclasses import dataclass
from fractions import Fraction

import pytest

from agaura.voting.base import (
    Ballot,
    Counts,
    InvalidSpecError,
    Quota,
    VotingError,
    check_keys,
    option_from,
    parse_quota,
    rank_by,
    share,
    to_jsonable,
)


class TestQuota:
    def test_strict_excludes_the_boundary(self) -> None:
        quota = Quota(Fraction(1, 2), strict=True)
        assert not quota.is_met(1, 2)
        assert quota.is_met(2, 3)

    def test_non_strict_includes_the_boundary(self) -> None:
        assert Quota(Fraction(2, 3), strict=False).is_met(2, 3)

    def test_never_met_with_an_empty_base(self) -> None:
        assert not Quota(Fraction(1, 2), strict=False).is_met(0, 0)


class TestParseQuota:
    def test_parses_string_fraction(self) -> None:
        assert parse_quota({"fraction": "2/3"}, "t") == Quota(Fraction(2, 3), strict=False)

    def test_parses_integer_and_strict_flag(self) -> None:
        assert parse_quota({"fraction": 1}, "t") == Quota(Fraction(1), strict=False)
        assert parse_quota({"fraction": "1/2", "strict": True}, "t").strict

    @pytest.mark.parametrize(
        "raw",
        [
            "1/2",  # not a mapping
            {"fraction": 0.5},  # float
            {"fraction": True},  # bool
            {"fraction": "abc"},
            {"fraction": "1/0"},
            {"fraction": "0"},
            {"fraction": "3/2"},
            {"fraction": "1", "strict": True},  # could never be met
            {"fraction": "1/2", "strict": "yes"},
        ],
    )
    def test_rejects_invalid_quotas(self, raw: object) -> None:
        with pytest.raises(InvalidSpecError) as error:
            parse_quota(raw, "threshold")
        assert error.value.code == "invalid_param"

    def test_rejects_missing_and_unknown_keys(self) -> None:
        with pytest.raises(InvalidSpecError, match="missing_param"):
            parse_quota({}, "threshold")
        with pytest.raises(InvalidSpecError, match="unknown_param"):
            parse_quota({"fraction": "1/2", "extra": 1}, "threshold")


class TestHelpers:
    def test_check_keys_accepts_required_and_optional(self) -> None:
        check_keys({"a": 1, "b": 2}, required={"a"}, optional={"b"}, what="x")

    def test_option_from_accepts_ints_and_digit_strings(self) -> None:
        options = frozenset({1, 12})
        assert option_from(12, options) == 12
        assert option_from("12", options) == 12

    @pytest.mark.parametrize("value", [3, "3", True, "-1", None, 1.0, "1a"])
    def test_option_from_rejects_unknown_or_non_integer_values(self, value: object) -> None:
        assert option_from(value, frozenset({1, 2})) is None

    def test_share_is_exact_and_none_without_base(self) -> None:
        assert share(1, 3) == Fraction(1, 3)
        assert share(1, 0) is None

    def test_rank_by_groups_ties_and_keeps_input_order(self) -> None:
        scores = {1: 5, 2: 7, 3: 5, 4: 9}
        assert rank_by([1, 2, 3, 4], key=lambda o: -scores[o]) == ((4,), (2,), (1, 3))

    def test_counts_of_ballots(self) -> None:
        ballots = [Ballot({"choice": 1}), Ballot(), Ballot({"choice": 2})]
        assert Counts.of(ballots) == Counts(cast=3, blank=1, valid=2)
        assert Ballot().is_blank and not ballots[0].is_blank

    def test_voting_error_message_and_code(self) -> None:
        assert str(VotingError("code")) == "code"
        error = VotingError("code", "detail")
        assert (str(error), error.code, error.detail) == ("code: detail", "code", "detail")


class Colour(enum.StrEnum):
    RED = "red"


@dataclass(frozen=True)
class Inner:
    value: Fraction


@dataclass(frozen=True)
class Outer:
    colour: Colour
    inner: Inner
    items: tuple[int, ...]
    mapping: dict[int, list[Fraction]]
    label: str


def test_to_jsonable_converts_nested_structures_exactly() -> None:
    value = Outer(Colour.RED, Inner(Fraction(2, 3)), (1, 2), {5: [Fraction(1, 2)]}, "x")
    converted = to_jsonable(value)
    assert converted == {
        "colour": "red",
        "inner": {"value": "2/3"},
        "items": [1, 2],
        "mapping": {"5": ["1/2"]},
        "label": "x",
    }
    json.dumps(converted)  # must be serializable as is
