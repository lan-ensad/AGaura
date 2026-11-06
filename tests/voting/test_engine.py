import json

import pytest

from agaura.voting import (
    METHODS,
    Ballot,
    InvalidBallotError,
    InvalidSpecError,
    PollSpec,
    check_spec,
    tally,
    to_jsonable,
    validate_ballot,
)


def test_registered_methods() -> None:
    assert set(METHODS) == {"single_choice", "approval", "majority_judgment", "condorcet"}


class TestSpec:
    def test_valid_spec(self) -> None:
        check_spec(PollSpec("approval", (1, 2)))

    def test_unknown_method(self) -> None:
        with pytest.raises(InvalidSpecError, match="unknown_method"):
            check_spec(PollSpec("borda", (1, 2)))

    @pytest.mark.parametrize("options", [(1,), tuple(range(31))])
    def test_option_count(self, options: tuple[int, ...]) -> None:
        with pytest.raises(InvalidSpecError, match="option_count"):
            check_spec(PollSpec("approval", options))

    @pytest.mark.parametrize("options", [(1, 1), (1, "2"), (1, True)])
    def test_invalid_options(self, options: tuple[object, ...]) -> None:
        with pytest.raises(InvalidSpecError, match="invalid_options"):
            check_spec(PollSpec("approval", options))  # type: ignore[arg-type]

    def test_params_must_be_a_mapping(self) -> None:
        with pytest.raises(InvalidSpecError, match="invalid_params"):
            check_spec(PollSpec("approval", (1, 2), ["seats"]))  # type: ignore[arg-type]


class TestBallots:
    SPEC = PollSpec("single_choice", (1, 2))

    def test_content_must_be_a_mapping(self) -> None:
        with pytest.raises(InvalidBallotError, match="malformed_ballot"):
            validate_ballot(self.SPEC, [1])  # type: ignore[arg-type]

    @pytest.mark.parametrize("content", [{"choice": 9}, [1]])
    def test_tally_rejects_corrupted_stored_ballots(self, content: object) -> None:
        with pytest.raises(InvalidBallotError):
            tally(self.SPEC, [Ballot({"choice": 1}), Ballot(content)])  # type: ignore[arg-type]


def test_result_serializes_to_json() -> None:
    spec = PollSpec("single_choice", (1, 2), {"adopt_option": 1})
    result = tally(spec, [Ballot({"choice": 1}), Ballot({"choice": 1}), Ballot()])
    data = json.loads(json.dumps(to_jsonable(result)))
    assert data["method"] == "single_choice"
    assert data["counts"] == {"cast": 3, "blank": 1, "valid": 2}
    assert data["outcome"]["status"] == "decided"
    assert data["outcome"]["options"][0]["share"] == "1/1"
    assert data["outcome"]["ranking"] == [[1], [2]]
