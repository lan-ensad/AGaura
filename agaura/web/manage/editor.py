"""Poll creation, "the method first" (FR-30, FR-31, FR-33, D-C), draft editing (FR-32), draft
order and copy from a past session (FR-15).

One flow, on its own page, for every way of creating a poll, in two server-rendered steps:

- step 1 (``GET …/polls/new``): the voting modes as cards (``GROUPS``), each a link to step 2
  (``?kind=…``); the owner may also copy the polls of a past session from there;
- step 2 (``GET …/polls/new?kind=…``, and ``…/polls/{poll_id}/edit`` for a draft, whose mode
  ``kind_of`` tells): only that mode's settings, the rule summed up in one sentence
  (``rule_sentence``) with its settings folded under "Modifier la règle"; then "Lancer le vote"
  (in session) or "Enregistrer le brouillon".

A card may offer several forms at the top of its step 2 (``FORMS``): "Choix unique" opens on a
resolution ("Pour", "Contre", blank labelled "Abstention"), the other form being free options.
A resolution stays a mode of its own here (``KINDS["resolution"]``: stored polls, drafts list,
Steering), with no card of its own.

Buttons of step 2 that render a page and save nothing (status 200), like the "+" of the
tribune: ``more`` (more option rows), ``change`` (step 1 again, as radio cards that carry every
value typed), ``switch=<kind>`` (step 2 of that mode, keeping what fits: ``switch_kind``; the
forms of a card are such buttons). ``action`` is ``launch`` (create or save, then push at once,
with the optional ``minutes``) or ``draft``; Enter saves a draft, never launches.

Everything works without JavaScript; ``editor.js`` rewrites the rule sentence as the settings
change, adds or removes option rows and switches between the forms of a card in place. After an
error, the form comes back (status 400) with every value as typed and one message next to each
faulty field; the fold that holds a faulty field opens.

The blank vote has a fixed label: "Abstention" for a resolution, none ("Vote blanc") otherwise.

Form fields hold raw text; ``poll_input`` turns them into a ``PollInput`` and the service checks
it again (``polls.poll_problems``), so the browser is never trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import zip_longest
from typing import Annotated, Any, ClassVar

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agaura.i18n import N_, _, ngettext
from agaura.models import Assembly, AssemblyStatus, Poll, PollStatus
from agaura.services import assemblies, polls
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    ServiceError,
)
from agaura.services.polls import OptionInput, PollInput
from agaura.voting.base import MAX_OPTIONS
from agaura.web.deps import CsrfProtected, DatabaseDep, RowId
from agaura.web.labels import base_label
from agaura.web.manage.common import (
    ConsoleUser,
    CurrentAccess,
    OwnedSession,
    PollsSession,
    console_write,
    optional_int,
    owned_poll,
    poll_action,
    redirect,
    router,
    run_action,
)
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.realtime import live_channel, manage_channel
from agaura.web.templating import flash, flash_error, render, templates

ERRORS.update(
    {
        "editor_option_label_required": N_("Give this option a label, or empty its description."),
        "editor_invalid_percent": N_("Enter a percentage above 0 and up to 100, e.g. 60 or 66.67."),
        "editor_invalid_max_choices": N_("Enter a whole number of at least 1."),
        "editor_invalid_adopt": N_("The option to adopt does not exist."),
        "editor_not_draft": N_(
            "This poll has already been pushed and can no longer be edited. "
            "Copy it as a new draft to change it."
        ),
        "editor_copy_same_session": N_("Choose another session to copy from."),
        "editor_copy_empty": N_("That session has no poll to copy."),
        "editor_unknown_kind": N_("Choose a voting method."),
    }
)
NOTICES.update(
    {
        "editor_poll_saved": N_("Draft saved."),
        "editor_polls_copied": N_("Polls copied as drafts, after the existing ones."),
        "editor_draft_moved": N_("Order of the drafts changed."),
    }
)


# --- Voting modes (step 1) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Kind:
    """A voting mode: one card of step 1, then its own form at step 2; or, with ``card``, a
    form offered at the top of that card's step 2, with no card of its own. Texts are message
    ids, translated where they are shown; a form needs only its icon and name (drafts list,
    Steering)."""

    name: str
    method: str  # the tallying method (§6)
    icon: str
    label: str
    summary: str = ""  # the ballot in a few words
    line: str = ""  # what voters do, in one line of plain words
    example: str = ""
    when: str = ""
    card: str = ""  # the card whose step 2 offers this mode, when it has none of its own


KINDS: dict[str, Kind] = {
    kind.name: kind
    for kind in (
        Kind("resolution", "single_choice", "file-check", N_("Resolution"), card="single_choice"),
        Kind(
            "single_choice",
            "single_choice",
            "circle-dot",
            N_("Single choice"),
            N_("One option only, resolutions included (For, Against, Abstention)"),
            N_("Each voter picks one option: for or against a resolution, or one among several."),
            N_("Approve the annual report"),
            N_(
                "Reports, accounts, budget, bylaws; or two or three options (beyond, prefer "
                "majority judgment)."
            ),
        ),
        Kind(
            "approval",
            "approval",
            "list-checks",
            N_("Approval vote"),
            N_("Election"),
            N_("Each voter ticks the names they accept, up to the number of seats."),
            N_("Elect 2 people to the board"),
            N_("Elections to one seat or several."),
        ),
        Kind(
            "majority_judgment",
            "majority_judgment",
            "chart-bar-stacked",
            N_("Majority judgment"),
            N_("Grades, from “Very good” to “Reject”"),
            N_("Each voter grades every option."),
            N_("Rank projects"),
            N_("To rank, or to choose among three options or more without splitting the votes."),
        ),
        Kind(
            "condorcet",
            "condorcet",
            "podium",
            N_("Condorcet method"),
            N_("Ranking, from the 1st choice to the last"),
            N_("Each voter ranks the options in order of preference."),
            N_("Choose the name of the association"),
            N_(
                "To choose among three options or more: the option preferred to each of the "
                "others, one on one, wins."
            ),
        ),
    )
}

# Step 1 lists groups of cards: (key, name of the group, its modes). A second group, set apart
# and styled apart, may join under the voting modes later (an "Animation" group with no voting
# value: nothing in the ballot box, the minutes or the results); there is one group for now.
GROUPS: tuple[tuple[str, str, tuple[Kind, ...]], ...] = (
    ("vote", N_("Voting methods"), tuple(kind for kind in KINDS.values() if not kind.card)),
)

# The forms a card offers at the top of its step 2, as (mode, name, what it gives voters); the
# card opens on the first one. "Choix unique" opens on a resolution, so that a resolution in
# session stays a card, a title and "Lancer le vote" (§4.4).
FORMS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "single_choice": (
        (
            "resolution",
            N_("Resolution"),
            N_("For, Against and Abstention: the assembly adopts or rejects a text."),
        ),
        ("single_choice", N_("Free options"), N_("Each voter picks one of the options below.")),
    ),
}


def card_of(kind: Kind) -> Kind:
    """The card of step 1 that leads to a mode: its own, or the one it is a form of."""
    return KINDS[kind.card] if kind.card else kind


def opening(card: Kind) -> str:
    """The mode that the link of a card opens: its first form, if it has forms."""
    forms = FORMS.get(card.name)
    return forms[0][0] if forms else card.name


def kind_of(poll: Poll) -> Kind:
    """The mode of a stored poll: a single choice that adopts an option is a resolution."""
    if poll.method == "single_choice" and "adopt_option" in (poll.params or {}):
        return KINDS["resolution"]
    return KINDS.get(poll.method, KINDS["single_choice"])


# The drafts list and Steering show each draft with the icon and name of its mode.
templates.env.globals["poll_kind"] = kind_of


# --- Form -----------------------------------------------------------------------------------

# Thresholds offered by the rule of each mode, in this order; a stored value outside the list
# (a resolution decided by the most votes, say) is offered too, so saving keeps it.
THRESHOLD_CHOICES = {
    "resolution": ("majority", "two_thirds", "three_quarters", "custom"),
    "single_choice": ("auto", "plurality", "majority", "two_thirds", "three_quarters", "custom"),
}
BASES = ("valid", "cast", "attendees")
# Presets of single choice thresholds, as (fraction, strict), to recognize them in stored polls.
PRESET_QUOTAS = {
    (Fraction(1, 2), True): "majority",
    (Fraction(2, 3), False): "two_thirds",
    (Fraction(3, 4), False): "three_quarters",
}
MIN_ROWS = 2  # option rows shown at least, filled or not
EXTRA_ROWS = 2  # empty rows after the filled ones
MORE_ROWS = 5  # rows added by the "more options" button without JavaScript
PERCENT = re.compile(r"[0-9]{1,3}(?:[.,][0-9]{1,2})?")
WHOLE = re.compile(r"[0-9]{1,6}")
PLACEHOLDER = re.compile(r"\{(option|base|share|n)\}")
MISSING = "…"  # in the rule sentence, a value not typed yet

# Fields shown with their own message; other problems go to the summary at the top.
INLINE_FIELDS = frozenset(
    {
        "title",
        "description",
        "method",
        "options",
        "base",
        "threshold",
        "adopt_option",
        "seats",
        "max_choices",
        "min_share",
        "minutes",
    }
)
# Fields folded under "Modifier la règle": an error there opens the fold.
RULE_FIELDS = frozenset({"base", "threshold", "adopt_option", "max_choices", "min_share"})
# Engine parameter errors (``invalid_param``) shown with a message specific to the field.
PARAM_MESSAGES = {
    "seats": "invalid_seats",
    "max_choices": "editor_invalid_max_choices",
    "threshold": "editor_invalid_percent",
    "min_share": "editor_invalid_percent",
    "adopt_option": "editor_invalid_adopt",
}


class EditorFields(BaseModel):
    """The form of step 2 as typed: raw text, shown again unchanged after an error."""

    BUTTONS: ClassVar[frozenset[str]] = frozenset({"action", "more", "change", "switch"})

    title: str = ""
    description: str = ""
    kind: str = ""
    option_label: list[str] = []
    option_description: list[str] = []
    # Resolution: the free options typed before it, given back on a switch to free options.
    kept_label: list[str] = []
    kept_description: list[str] = []
    # Single choice and resolution
    base: str = "valid"
    threshold: str = "auto"
    threshold_percent: str = ""
    threshold_strict: str = ""  # "1": strictly more than the percentage
    adopt_option: str = ""  # resolution: position of the option to adopt, "1" by default
    # Approval
    seats: str = "1"
    max_choices: str = "seats"  # "seats", "none" or "number"
    max_choices_number: str = ""
    min_share_percent: str = ""
    min_share_strict: str = ""
    # Majority Judgment: a non-default scale is kept as is, never offered.
    grade_count: str = ""
    # Launch at once (in session): optional auto-close, in minutes.
    minutes: str = ""
    # Buttons
    action: str = ""  # "launch" or "draft"
    more: str = ""
    change: str = ""
    switch: str = ""

    @property
    def method(self) -> str:
        """The tallying method of the mode; an unknown mode is sent as is, and refused."""
        kind = KINDS.get(self.kind)
        return kind.method if kind else self.kind

    @property
    def launch(self) -> bool:
        return self.action == "launch"

    def rows(self) -> list[tuple[str, str]]:
        """Option rows with some text, in order; empty rows are ignored."""
        return filled(self.option_label, self.option_description)

    def kept(self) -> list[tuple[str, str]]:
        """The free options a resolution keeps aside, as (label, description) rows."""
        return filled(self.kept_label, self.kept_description)

    def carried(self) -> list[tuple[str, str]]:
        """Every value typed, as (name, value) pairs, buttons left out: the hidden fields of
        step 1 after "Changer de mode", so that coming back to step 2 loses nothing."""
        pairs: list[tuple[str, str]] = []
        for name, value in self.model_dump(exclude=set(self.BUTTONS)).items():
            if isinstance(value, list):
                pairs.extend((name, item) for item in value)
            else:
                pairs.append((name, value))
        return pairs


def filled(labels: list[str], descriptions: list[str]) -> list[tuple[str, str]]:
    """(label, description) rows with some text, in order."""
    pairs = zip_longest(labels, descriptions, fillvalue="")
    return [(label, text) for label, text in pairs if label.strip() or text.strip()]


def fields_from_kind(kind: str) -> EditorFields:
    """The explicit starting point of each mode (FR-31); never the rule of the last poll."""
    if kind == "resolution":
        return EditorFields(
            kind=kind,
            option_label=[_("For"), _("Against")],
            option_description=["", ""],
            adopt_option="1",
            threshold="majority",
        )
    return EditorFields(kind=kind)


def switch_kind(fields: EditorFields, kind: str) -> EditorFields:
    """Step 2 of another mode with what fits: the title, the text and the timer always; the
    options between modes of free options, kept aside by a resolution (it has its own) and given
    back after it; between a resolution and free options of a single choice, the base and a
    threshold chosen that the other form offers; the defaults of the new mode for the rest."""
    if kind == fields.kind:
        return fields.model_copy(update=dict.fromkeys(EditorFields.BUTTONS, ""))
    fresh = fields_from_kind(kind)
    fresh.title, fresh.description, fresh.minutes = fields.title, fields.description, fields.minutes
    if fields.kind not in KINDS:
        return fresh
    rows = fields.kept() if fields.kind == "resolution" else fields.rows()
    labels = [label for label, _text in rows]
    descriptions = [text for _label, text in rows]
    if kind == "resolution":
        fresh.kept_label, fresh.kept_description = labels, descriptions
    elif fields.kind == "resolution":
        fresh.option_label, fresh.option_description = labels, descriptions
    else:  # as typed, empty rows included
        fresh.option_label = list(fields.option_label)
        fresh.option_description = list(fields.option_description)
    if {kind, fields.kind} == {"resolution", "single_choice"}:
        fresh.base = fields.base
        fresh.threshold_percent = fields.threshold_percent
        fresh.threshold_strict = fields.threshold_strict
        chosen = fields.threshold != fields_from_kind(fields.kind).threshold
        if chosen and fields.threshold in THRESHOLD_CHOICES[kind]:
            fresh.threshold = fields.threshold
    return fresh


def display_rows(
    rows: list[tuple[str, str]], minimum: int = MIN_ROWS, extra: int = EXTRA_ROWS
) -> list[tuple[str, str]]:
    """Filled rows first (so that ``option:<n>`` errors match), then empty rows to fill in."""
    total = min(max(len(rows) + extra, minimum), MAX_OPTIONS)
    return rows + [("", "")] * max(total - len(rows), 0)


# --- Percentages ------------------------------------------------------------------------------


def parse_percent(raw: str) -> Fraction:
    """``"66,67"`` → ``Fraction(6667, 10000)``: a share in (0, 1], exact (no float)."""
    text = raw.strip().removesuffix("%").strip()
    if not PERCENT.fullmatch(text):
        raise InvalidInputError("editor_invalid_percent")
    share = Fraction(Decimal(text.replace(",", "."))) / 100
    if not 0 < share <= 1:
        raise InvalidInputError("editor_invalid_percent")
    return share


def format_percent(share: Fraction) -> str:
    """``Fraction(2, 3)`` → ``"66,67"`` (rounded, for display only); ``Fraction(1, 2)`` → ``"50"``.

    Rounding never changes a stored threshold unless the form is saved with the rounded value.
    """
    value = share * 100
    if value.denominator == 1:
        return str(value.numerator)
    rounded = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.01"))
    return str(rounded).rstrip("0").rstrip(".").replace(".", ",")


def quota_param(percent: str, strict: str) -> dict[str, Any]:
    share = parse_percent(percent)
    return {"fraction": f"{share.numerator}/{share.denominator}", "strict": strict == "1"}


def stored_quota(raw: Any) -> tuple[Fraction, bool] | None:
    """A quota as stored in a poll's parameters: ``{"fraction": "2/3", "strict": false}``."""
    try:
        return Fraction(str(raw["fraction"])), bool(raw.get("strict", False))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        return None


def quota_fields(raw: Any) -> tuple[str, str]:
    """Stored quota → (percentage text, strict flag) for the form."""
    quota = stored_quota(raw)
    if quota is None:
        return "", ""
    return format_percent(quota[0]), "1" if quota[1] else ""


def whole_number(raw: str) -> int | None:
    """A whole number typed in a field (ASCII digits only), else None."""
    text = raw.strip()
    return int(text) if WHOLE.fullmatch(text) else None


# --- Form ⇄ poll --------------------------------------------------------------------------------


def _single_choice_params(fields: EditorFields, errors: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if fields.base != "valid":
        params["base"] = fields.base
    if fields.threshold == "custom":
        try:
            params["threshold"] = quota_param(fields.threshold_percent, fields.threshold_strict)
        except InvalidInputError as error:
            errors["threshold"] = error_message(error)
    elif fields.threshold != "auto":
        params["threshold"] = fields.threshold
    if fields.kind == "resolution":
        if (position := whole_number(fields.adopt_option or "1")) is not None:
            params["adopt_option"] = position
        else:
            errors["adopt_option"] = error_message("editor_invalid_adopt")
    return params


def _approval_params(fields: EditorFields, errors: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    seats = whole_number(fields.seats)
    if seats:
        params["seats"] = seats
    else:
        errors["seats"] = error_message("invalid_seats")
    if fields.max_choices == "seats" and seats:
        params["max_choices"] = seats
    elif fields.max_choices == "number":
        if number := whole_number(fields.max_choices_number):
            params["max_choices"] = number
        else:
            errors["max_choices"] = error_message("editor_invalid_max_choices")
    if fields.min_share_percent.strip():
        try:
            params["min_share"] = quota_param(fields.min_share_percent, fields.min_share_strict)
        except InvalidInputError as error:
            errors["min_share"] = error_message(error)
    return params


def poll_input(fields: EditorFields) -> tuple[PollInput, dict[str, str]]:
    """The poll typed in the form, and the problems found while reading it (field → message)."""
    errors: dict[str, str] = {}
    params: dict[str, Any] = {}
    if fields.method == "single_choice":
        params = _single_choice_params(fields, errors)
    elif fields.method == "approval":
        params = _approval_params(fields, errors)
    elif fields.method == "majority_judgment" and fields.grade_count:
        params = {"grade_count": whole_number(fields.grade_count)}
    if fields.launch and fields.minutes.strip() and whole_number(fields.minutes) is None:
        errors["minutes"] = error_message("invalid_number")
    data = PollInput(
        title=fields.title,
        method=fields.method,
        options=[OptionInput(label, text) for label, text in fields.rows()],
        params=params,
        description=fields.description,
        # Fixed: "Abstention" for a resolution, "Vote blanc" (none stored) otherwise.
        blank_label=_("Abstention") if fields.kind == "resolution" else None,
    )
    for problem in polls.poll_problems(data):
        errors.setdefault(problem.detail or "", problem_message(problem))
    return data, errors


def problem_message(problem: ServiceError) -> str:
    field = problem.detail or ""
    if field.startswith("option:") and problem.code == "required":
        return error_message("editor_option_label_required")
    if problem.code == "invalid_param" and field in PARAM_MESSAGES:
        return error_message(PARAM_MESSAGES[field])
    return error_message(problem)


def _stored_threshold(raw: Any, option_count: int, kind: str) -> tuple[str, str, str]:
    """Stored threshold → (choice, percentage text, strict flag) for the form."""
    if raw is None:
        # The engine's default (§6.2). A resolution names it: more than half with 2 options.
        if kind != "resolution":
            return "auto", "", ""
        return ("majority" if option_count == 2 else "plurality"), "", ""
    if isinstance(raw, str):
        return raw, "", ""
    if (quota := stored_quota(raw)) and quota in PRESET_QUOTAS:
        return PRESET_QUOTAS[quota], "", ""
    return "custom", *quota_fields(raw)


def fields_from_poll(poll: Poll) -> EditorFields:
    params = dict(poll.params or {})
    kind = kind_of(poll).name
    fields = EditorFields(
        title=poll.title,
        description=poll.description or "",
        kind=kind,
        option_label=[option.label for option in poll.options],
        option_description=[option.description or "" for option in poll.options],
    )
    if poll.method == "single_choice":
        fields.base = str(params.get("base", "valid"))
        fields.threshold, fields.threshold_percent, fields.threshold_strict = _stored_threshold(
            params.get("threshold"), len(poll.options), kind
        )
        if "adopt_option" in params:
            fields.adopt_option = str(params["adopt_option"])
    elif poll.method == "approval":
        seats = params.get("seats", 1)
        fields.seats = str(seats)
        max_choices = params.get("max_choices")
        if max_choices is None:
            fields.max_choices = "none"
        elif max_choices == seats:
            fields.max_choices = "seats"
        else:
            fields.max_choices, fields.max_choices_number = "number", str(max_choices)
        if params.get("min_share"):
            fields.min_share_percent, fields.min_share_strict = quota_fields(params["min_share"])
    elif "grade_count" in params:
        fields.grade_count = str(params["grade_count"])
    return fields


# --- The rule in one sentence ---------------------------------------------------------------


def rule_texts() -> dict[str, str]:
    """Every sentence of a rule, translated, with ``{option}``, ``{base}``, ``{share}`` and
    ``{n}`` standing for what the form decides. The server fills them in (``rule_sentence``);
    editor.js does the same from the copy the page carries, so it holds no text of its own.
    The words follow ``single_choice_rule`` and ``approval_rule`` of macros/results.html."""
    holders = {"option": "{option}", "base": "{base}", "share": "{share}", "num": "{n}"}
    texts = {
        "resolution-majority": _("Adopted if “%(option)s” gets more than half %(base)s."),
        "resolution-two_thirds": _("Adopted if “%(option)s” gets at least two thirds %(base)s."),
        "resolution-three_quarters": _(
            "Adopted if “%(option)s” gets at least three quarters %(base)s."
        ),
        "resolution-more": _("Adopted if “%(option)s” gets more than %(share)s %(base)s."),
        "resolution-least": _("Adopted if “%(option)s” gets at least %(share)s %(base)s."),
        "resolution-plurality": _("Adopted if “%(option)s” gets the most votes, alone at the top."),
        "single-plurality": _("The option with the most votes wins, if it is alone at the top."),
        "single-majority": _("The option that gets more than half %(base)s wins."),
        "single-two_thirds": _("The option that gets at least two thirds %(base)s wins."),
        "single-three_quarters": _("The option that gets at least three quarters %(base)s wins."),
        "single-more": _("The option that gets more than %(share)s %(base)s wins."),
        "single-least": _("The option that gets at least %(share)s %(base)s wins."),
        # Both forms of each count, for editor.js (French: the singular up to 1).
        "seats-one": ngettext(
            "The most ticked candidate is elected.",
            "The %(num)s most ticked candidates are elected.",
            1,
        ),
        "seats-many": ngettext(
            "The most ticked candidate is elected.",
            "The %(num)s most ticked candidates are elected.",
            2,
        ),
        "ticks-one": ngettext(
            "Each voter ticks one name at most.", "Each voter ticks %(num)s names at most.", 1
        ),
        "ticks-many": ngettext(
            "Each voter ticks one name at most.", "Each voter ticks %(num)s names at most.", 2
        ),
        "ticks-free": _("Each voter ticks as many names as they wish."),
        "minimum-more": _("Elected candidates need more than %(share)s of valid votes."),
        "minimum-least": _("Elected candidates need at least %(share)s of valid votes."),
        "judgment": _("The option with the best majority grade wins."),
        "condorcet": _(
            "The option preferred to each of the others, one on one, wins; a cycle is settled "
            "by the strongest chains of duels won (Schulze)."
        ),
        **{f"base-{base}": base_label(base) for base in BASES},
    }
    return {key: text % holders for key, text in texts.items()}


def share_text(raw: str) -> str:
    """A percentage typed in the form, as the sentence shows it: ``"60"`` → ``"60 %"``."""
    try:
        return f"{format_percent(parse_percent(raw))}\u00a0%"
    except InvalidInputError:
        return MISSING


def fill(text: str, **values: str) -> str:
    """Put the values in a sentence of ``rule_texts``, in one pass: an option label that holds
    ``{n}`` stays as typed."""
    return PLACEHOLDER.sub(lambda match: values.get(match[1], MISSING), text)


def rule_sentence(fields: EditorFields) -> str:
    """The rule of the poll typed in the form, in one sentence: "Adopted if “For” gets more
    than half of valid votes." (FR-30). Mirrored by editor.js."""
    texts = rule_texts()
    if fields.kind == "majority_judgment":
        return texts["judgment"]
    if fields.kind == "condorcet":
        return texts["condorcet"]
    if fields.kind == "approval":
        seats = whole_number(fields.seats)
        key = "seats-many" if (seats or 0) > 1 else "seats-one"
        parts = [fill(texts[key], n=str(seats) if seats else MISSING)]
        limit = None
        if fields.max_choices == "seats":
            limit = seats
        elif fields.max_choices == "number":
            limit = whole_number(fields.max_choices_number)
        if fields.max_choices == "none":
            parts.append(texts["ticks-free"])
        elif limit:
            parts.append(fill(texts["ticks-many" if limit > 1 else "ticks-one"], n=str(limit)))
        if fields.min_share_percent.strip():
            key = "minimum-more" if fields.min_share_strict == "1" else "minimum-least"
            parts.append(fill(texts[key], share=share_text(fields.min_share_percent)))
        return " ".join(parts)
    labels = [label.strip() for label in fields.option_label if label.strip()]
    threshold = fields.threshold
    if threshold == "auto":
        threshold = "majority" if len(labels) == 2 else "plurality"
    elif threshold == "custom":
        threshold = "more" if fields.threshold_strict == "1" else "least"
    prefix = "resolution" if fields.kind == "resolution" else "single"
    text = texts.get(f"{prefix}-{threshold}", texts[f"{prefix}-plurality"])
    # The option to adopt by its row, as typed: an emptied "Pour" reads "…", not "Contre".
    position = whole_number(fields.adopt_option or "1") or 0
    rows = fields.option_label
    typed = rows[position - 1].strip() if 0 < position <= len(rows) else ""
    option = typed or MISSING
    base = texts.get(f"base-{fields.base}", texts["base-valid"])
    return fill(text, option=option, base=base, share=share_text(fields.threshold_percent))


# --- Pages --------------------------------------------------------------------------------------


def render_methods(
    request: Request,
    assembly: Assembly,
    poll: Poll | None,
    *,
    carry: EditorFields | None = None,
    copy_sources: list[polls.CopySource] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """Step 1: the voting modes. Fresh, each card is a link to step 2 (``opening``); after
    "Changer de mode" (``carry``), the cards are radio buttons of a form that carries what was
    typed, the card of the mode typed checked: its radio button names that mode, so a resolution
    comes back as it was, and the others their own mode."""
    current = KINDS.get(carry.kind) if carry else None
    return render(
        request,
        "manage/editor.html",
        {
            "assembly": assembly,
            "poll": poll,
            "step": 1,
            "groups": GROUPS,
            "opening": opening,
            "carry": carry,
            "current_kind": current.name if current else "",
            "current_card": card_of(current).name if current else "",
            "copy_sources": copy_sources or [],
            "step_error": error,
        },
        status_code=status_code,
    )


def render_editor(
    request: Request,
    assembly: Assembly,
    poll: Poll | None,
    fields: EditorFields,
    *,
    errors: dict[str, str] | None = None,
    minimum_rows: int = MIN_ROWS,
    status_code: int = 200,
) -> Response:
    """Step 2: the form of one mode only, under the card it comes from and its forms."""
    errors = errors or {}
    kind = KINDS[fields.kind]
    card = card_of(kind)
    summary = [
        message
        for field, message in errors.items()
        if field not in INLINE_FIELDS and not field.startswith("option")
    ]
    if kind.name == "resolution":  # For and Against: no empty row to fill in
        rows = display_rows(fields.rows(), max(len(fields.rows()), 2), extra=0)
    else:
        rows = display_rows(fields.rows(), minimum_rows)
    thresholds = THRESHOLD_CHOICES.get(kind.name, ())
    if thresholds and fields.threshold not in thresholds and fields.threshold != "auto":
        thresholds = (*thresholds, fields.threshold)  # kept as stored, or as sent
    return render(
        request,
        "manage/editor.html",
        {
            "assembly": assembly,
            "poll": poll,
            "step": 2,
            "kind": kind,
            "card": card,
            "forms": [(KINDS[name], label, text) for name, label, text in FORMS.get(card.name, ())],
            "form": fields,
            "rows": rows,
            "errors": errors,
            "summary": summary,
            "thresholds": thresholds,
            "rule": rule_sentence(fields),
            "rule_texts": rule_texts(),
            "rule_open": any(field in RULE_FIELDS for field in errors),
            "labels_open": any(field.startswith("option") for field in errors),
            "max_options": MAX_OPTIONS,
            "can_launch": assembly.status is AssemblyStatus.IN_SESSION,
        },
        status_code=status_code,
    )


def _rows_wanted(fields: EditorFields) -> int:
    """Rows to show again: as many as sent, plus more when the "more options" button was used."""
    return max(len(fields.option_label) + (MORE_ROWS if fields.more else 0), MIN_ROWS)


def _unavailable(request: Request, public_id: str, code: str) -> Response:
    response = redirect(public_id, "#drafts")
    flash_error(response, request, code)
    return response


def _editable(poll: Poll) -> str | None:
    """Error code when the poll cannot be edited, else None."""
    if poll.status is not PollStatus.DRAFT:
        return "editor_not_draft"
    if poll.assembly.status is AssemblyStatus.CLOSED:
        return "assembly_closed"
    return None


def _saved(request: Request, public_id: str, *, notice: str, launched: bool) -> Response:
    hub = request.app.state.hub
    if launched:
        hub.publish(live_channel(public_id), "update", "polls")
    hub.publish(manage_channel(public_id), "turnout", "console")
    # Launched: back to Polls, whose "Votes en cours" shows it, as after pushing a draft there.
    response = RedirectResponse(
        f"/manage/{public_id}{'#tab-polls' if launched else '#drafts'}", status_code=303
    )
    flash(response, request, notice)
    return response


def _save_problems(error: ServiceError) -> dict[str, str]:
    """Errors raised while saving, shown in the form (quota, state, timer…)."""
    field = error.detail if error.detail in INLINE_FIELDS else None
    if error.code in ("invalid_timer", "invalid_number"):
        field = "minutes"
    return {field or "save": problem_message(error)}


def _draft_page(
    request: Request, assembly: Assembly, poll: Poll | None, fields: EditorFields
) -> Response | None:
    """The page that a button of step 2 asks for without saving, else None (a save)."""
    if fields.change:
        return render_methods(request, assembly, poll, carry=fields)
    if fields.switch:
        if fields.switch not in KINDS:
            return render_methods(request, assembly, poll, carry=fields)
        return render_editor(request, assembly, poll, switch_kind(fields, fields.switch))
    if fields.kind not in KINDS:
        error = error_message("editor_unknown_kind")
        return render_methods(request, assembly, poll, carry=fields, error=error, status_code=400)
    if fields.more:
        return render_editor(request, assembly, poll, fields, minimum_rows=_rows_wanted(fields))
    return None


def _refused(
    request: Request,
    assembly: Assembly,
    poll: Poll | None,
    fields: EditorFields,
    errors: dict[str, str],
) -> Response:
    return render_editor(
        request,
        assembly,
        poll,
        fields,
        errors=errors,
        minimum_rows=_rows_wanted(fields),
        status_code=400,
    )


@router.get("/manage/{public_id}/polls/new", response_class=HTMLResponse)
def new_poll_form(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    access: CurrentAccess,
    database: DatabaseDep,
    kind: str = "",
) -> Response:
    """Step 1, or step 2 of the mode named by ``kind`` (an unknown one gives step 1)."""
    with database.read() as db:
        assembly = assemblies.get_assembly(db, user, public_id)
        if assembly.status is AssemblyStatus.CLOSED:
            return _unavailable(request, public_id, "assembly_closed")
        if kind in KINDS:
            return render_editor(request, assembly, None, fields_from_kind(kind))
        # The polls of the account's other sessions: the owner's only, since a delegate link
        # reaches one session and nothing else of the account.
        sources = polls.copy_sources(db, user, public_id) if access.is_owner else []
        return render_methods(request, assembly, None, copy_sources=sources)


@router.post("/manage/{public_id}/polls/new", dependencies=[CsrfProtected])
def create_poll(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    database: DatabaseDep,
    fields: Annotated[EditorFields, Form()],
) -> Response:
    """Create a draft (``action=draft``), or create and push it at once while the session is in
    session (``action=launch``, with the optional ``minutes``) (FR-30, FR-33)."""
    with database.read() as db:
        assembly = assemblies.get_assembly(db, user, public_id)
        if (page := _draft_page(request, assembly, None, fields)) is not None:
            return page
    data, errors = poll_input(fields)
    if not errors:
        try:
            with console_write(request) as db:
                poll = polls.create_poll(db, user, public_id, data)
                if fields.launch:
                    minutes = optional_int(fields.minutes, minimum=1)
                    polls.open_poll(db, user, poll.id, minutes=minutes)
        except NotFoundError:
            raise
        except ServiceError as error:
            errors = _save_problems(error)
        else:
            notice = "poll_opened" if fields.launch else "poll_created"
            return _saved(request, public_id, notice=notice, launched=fields.launch)
    with database.read() as db:
        assembly = assemblies.get_assembly(db, user, public_id)
        return _refused(request, assembly, None, fields, errors)


@router.get("/manage/{public_id}/polls/{poll_id}/edit", response_class=HTMLResponse)
def edit_poll_form(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    database: DatabaseDep,
    poll_id: RowId,
) -> Response:
    """Step 2 of the draft's mode, "Changer de mode" at hand."""
    with database.read() as db:
        poll = owned_poll(db, user, public_id, poll_id)
        if code := _editable(poll):
            return _unavailable(request, public_id, code)
        return render_editor(request, poll.assembly, poll, fields_from_poll(poll))


@router.post("/manage/{public_id}/polls/{poll_id}/edit", dependencies=[CsrfProtected])
def update_poll(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    database: DatabaseDep,
    poll_id: RowId,
    fields: Annotated[EditorFields, Form()],
) -> Response:
    """Save a draft (FR-32), and push it at once with ``action=launch``; a poll pushed
    meanwhile goes back to the console with a message."""
    with database.read() as db:
        poll = owned_poll(db, user, public_id, poll_id)
        if code := _editable(poll):
            return _unavailable(request, public_id, code)
        if (page := _draft_page(request, poll.assembly, poll, fields)) is not None:
            return page
    data, errors = poll_input(fields)
    if not errors:
        try:
            with console_write(request) as db:
                owned_poll(db, user, public_id, poll_id)
                polls.update_poll(db, user, poll_id, data)
                if fields.launch:
                    minutes = optional_int(fields.minutes, minimum=1)
                    polls.open_poll(db, user, poll_id, minutes=minutes)
        except NotFoundError:
            raise
        except InvalidStateError as error:  # pushed, or session closed or not open
            if error.code.startswith("poll_"):
                return _unavailable(request, public_id, "editor_not_draft")
            if error.code == "assembly_closed":
                return _unavailable(request, public_id, error.code)
            errors = _save_problems(error)
        except ServiceError as error:
            errors = _save_problems(error)
        else:
            notice = "poll_opened" if fields.launch else "editor_poll_saved"
            return _saved(request, public_id, notice=notice, launched=fields.launch)
    with database.read() as db:
        poll = owned_poll(db, user, public_id, poll_id)
        return _refused(request, poll.assembly, poll, fields, errors)


# --- Drafts list and copy -----------------------------------------------------------------------


@router.post("/manage/{public_id}/polls/{poll_id}/move", dependencies=[CsrfProtected])
def move_poll(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    poll_id: RowId,
    direction: Annotated[str, Form()] = "",
) -> Response:
    """Move a draft one place up or down (``direction``: ``up`` or ``down``)."""
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.move_draft(db, user, poll.id, direction),
        notice="editor_draft_moved",
        anchor=f"#draft-{poll_id}",
    )


@router.post("/manage/{public_id}/polls/copy", dependencies=[CsrfProtected])
def copy_polls(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    source: Annotated[str, Form()] = "",
) -> Response:
    """Copy the polls of another session of the account as drafts (FR-15), from step 1.

    A session of another account answers 404, like one that does not exist.
    """

    def copy(db: Session) -> None:
        polls.copy_polls(db, user, source, public_id)

    return run_action(request, public_id, copy, notice="editor_polls_copied", anchor="#drafts")
