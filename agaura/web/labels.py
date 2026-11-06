"""Labels of codes shown in several places (methods, statuses, grades, ranks), translated on use."""

from __future__ import annotations

from typing import Any

from agaura.i18n import N_, _

# Majority Judgment scale (D8), best grade first: index = grade value.
GRADES = (
    N_("Very good"),
    N_("Good"),
    N_("Fairly good"),
    N_("Passable"),
    N_("Insufficient"),
    N_("Reject"),
)

METHODS = {
    "single_choice": N_("Single choice"),
    "approval": N_("Approval vote"),
    "majority_judgment": N_("Majority judgment"),
    "condorcet": N_("Condorcet method"),
}

ASSEMBLY_STATUSES = {
    "preparation": N_("In preparation"),
    "in_session": N_("In session"),
    "closed": N_("Closed"),
}

POLL_STATUSES = {
    "draft": N_("Draft"),
    "open": N_("Open"),
    "closed": N_("Voting closed"),  # not "Closed": that one is a session's, feminine in French
    "published": N_("Published"),
    "cancelled": N_("Cancelled"),
}

OUTCOMES = {
    "decided": N_("Decided"),
    "tie": N_("Tie"),
    "threshold_not_met": N_("No option reaches the required threshold"),
    "seats_unfilled": N_("Some seats remain unfilled"),
    "no_valid_votes": N_("No valid vote"),
}

# Rights of a delegate link (FR-92), named like the console rubrics and cards they open.
DELEGATE_RIGHTS = {
    "agenda": N_("Agenda"),
    "notes": N_("Session notes"),
    "polls": N_("Polls"),
    "timer": N_("Chronometer"),
    "screen": N_("What the room sees"),
    "headcount": N_("Headcount"),
}

BASES = {
    "valid": N_("of valid votes"),
    "cast": N_("of ballots cast"),
    "attendees": N_("of attendees"),
}


def grade_label(grade: int | None, grade_count: int = len(GRADES)) -> str:
    if grade is None:
        return "\u2013"
    if grade_count == len(GRADES):
        return _(GRADES[grade])
    return _("Grade %(number)s") % {"number": grade + 1}


def rank_label(rank: int | None) -> str:
    """A rank of a Condorcet ballot: "1er choix", "2e choix"…; "Non classée" without one."""
    if rank is None:
        return _("Not ranked")
    if rank == 1:
        return _("1st choice")
    return _("Choice %(rank)s") % {"rank": rank}


def method_label(method: str) -> str:
    return _(METHODS.get(method, method))


def poll_method_label(poll: Any) -> str:
    """The voting method of a poll as the editor names it: a single choice that adopts an
    option is a "Résolution" (a form of the "Choix unique" card, FR-31), everywhere the poll is
    described."""
    if poll.method == "single_choice" and "adopt_option" in (poll.params or {}):
        return _("Resolution")
    return method_label(poll.method)


def assembly_status_label(status: Any) -> str:
    value = getattr(status, "value", status)
    return _(ASSEMBLY_STATUSES.get(value, value))


def poll_status_label(status: Any) -> str:
    value = getattr(status, "value", status)
    return _(POLL_STATUSES.get(value, value))


def outcome_label(status: str) -> str:
    return _(OUTCOMES.get(status, status))


def base_label(base: str | None) -> str:
    return _(BASES.get(base or "valid", BASES["valid"]))


def blank_label(poll: Any) -> str:
    return poll.blank_label or _("Blank vote")


def blank_count_label(poll: Any) -> str:
    """The blank ballots in the counts of a result: the very words of the ballot's blank choice
    ("Abstention" for a resolution, "Vote blanc" otherwise), so the counts read as the voters
    chose (``blank_label``)."""
    return blank_label(poll)


def right_label(right: str) -> str:
    return _(DELEGATE_RIGHTS.get(right, right))


def option_labels(poll: Any) -> dict[int, str]:
    """Option labels by position, the identifier used in ballots and results."""
    return {option.position: option.label for option in poll.options}


TEMPLATE_GLOBALS = {
    "grade_label": grade_label,
    "rank_label": rank_label,
    "method_label": method_label,
    "poll_method_label": poll_method_label,
    "assembly_status_label": assembly_status_label,
    "poll_status_label": poll_status_label,
    "outcome_label": outcome_label,
    "base_label": base_label,
    "blank_label": blank_label,
    "blank_count_label": blank_count_label,
    "option_labels": option_labels,
    "right_label": right_label,
}
