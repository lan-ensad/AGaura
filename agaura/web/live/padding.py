"""Ballot steps of one size per poll (SPEC §9.2).

TLS hides what a page says, not how long it is: whoever watches the network (the venue's
Wi-Fi, a proxy, a log of response sizes) sees the size of every request and answer. The
summary of a ballot names the voter's choice and a ballot sent back keeps it checked, so their
sizes would tell the choices apart. Every answer of a ballot step (``routes._section``:
summary, ballot sent back to be changed or with an error, vote recorded, poll closed) therefore
ends its poll's section with an HTML comment that brings it to one size for every choice and
every step of that poll: the largest the section can be, measured by rendering it for the
choices that reach each maximum (``_extremes``). The rest of the page does not depend on the
ballot. Compression would undo this: neither the application nor Caddy compresses these routes
(``deploy/Caddyfile``).

Requests: the forms of a ballot step carry ``data-pad``, the size live.js brings each request
body to with a ``pad`` field the server never reads (``request_size``). A browser without
JavaScript sends its fields as they are, every position and rank on two digits
(``ballot_forms.number_field``): single choice and Condorcet requests then have one size, the
blank radio button of single choice included; what still shows is the blank box of the other
methods and how many options were approved or graded (an unchecked box sends nothing).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import combinations
from typing import Any
from urllib.parse import urlencode

from markupsafe import Markup, escape

from agaura.i18n import _
from agaura.models import Poll
from agaura.web.labels import grade_label, rank_label
from agaura.web.live.ballot_forms import (
    BLANK_CHOICE,
    UNRANKED,
    Choice,
    grade_name,
    number_field,
    rank_name,
)
from agaura.web.messages import ERRORS, GENERIC_ERROR, NOTICES
from agaura.web.templating import templates

PAD_FIELD = "pad"
MACROS = "live/macros.html"


def _size(text: str) -> int:
    """Bytes of ``text`` once escaped in a page."""
    return len(str(escape(text)).encode())


def _longest_message() -> str:
    """The longest message a section can show (error, notice): every section rendered to find
    the poll's maximum shows it, so no message can make a step longer than that maximum. Read
    on each call: modules add their messages when they are imported."""
    return max(
        (_(message) for message in (*ERRORS.values(), *NOTICES.values(), GENERIC_ERROR)), key=_size
    )


def _grade_count(poll: Poll) -> int:
    return int(poll.params.get("grade_count", 6))  # as the templates read it


def _extremes(poll: Poll) -> Iterator[tuple[str, Choice | None]]:
    """Steps and choices whose sections, together, reach the size of any section of ``poll``.

    What depends on the choice: the ``checked`` inputs of a ballot sent back (the blank box
    among them), its ``selected`` ranks with Condorcet and, with Majority Judgment, its count of
    graded options; in a summary, the hidden fields that carry the choice, the labels it names
    and, with Majority Judgment, each option's grade or the worst grade drawn dashed
    (``is-default``) and marked "not graded", and the warning that names the options left
    without a grade; with Condorcet, each option's rank or "Non classée", and the hint that
    follows when an option is left unranked.

    What depends on the step only, the same for every choice: the steps of the ballot box and
    the flag "Bulletin pas encore déposé" of a summary, the steps and the empty "Vote suivant"
    link of a vote recorded. The approval counter, the counter of ranked options, the boxes
    blocked at the maximum, the target of "Vote suivant" and the "À confirmer" of the summary
    of the open polls are written by live.js in the browser, never in an answer: the server
    sends the same markup whatever the choice and whatever the other polls.
    """
    method = poll.method
    positions = [option.position for option in poll.options]
    blank = Choice(method)
    yield "voted", None
    yield "closed", None
    if method == "single_choice":
        # One radio button checked whatever the choice; one label and one field in a summary.
        longest = max(
            poll.options, key=lambda option: _size(option.label) + len(str(option.position))
        )
        yield "ballot", blank
        yield "review", blank
        yield "review", Choice(method, {"choice": longest.position})
    elif method == "approval":
        # Each approved option adds a checked box, a line and a field.
        everything = Choice(method, {"approved": positions})
        yield "ballot", everything
        yield "review", everything
        yield "review", blank
        yield "review", Choice(method, implicit_blank=True)
    elif method == "condorcet":
        # A ballot sent back: one ``selected`` per ranked option. A summary: each option once,
        # with its rank's label and a hidden field, or "Non classée" without a field, and the
        # hint shown when an option is left unranked. Each option's share depends on its rank
        # alone, the same for every option, so the longest rank stands for all of them; what
        # leaving an option unranked changes depends only on the digits of its position. If it
        # saves bytes, the longest summaries with the hint leave one option unranked, of some
        # width; if it adds bytes, they leave every option of some widths unranked. Both kinds
        # are tried; "None ranked" is never shown but bounds them all.
        longest = max(
            range(1, len(positions) + 1), key=lambda rank: _size(rank_label(rank)) + len(str(rank))
        )
        everything = dict.fromkeys(map(str, positions), longest)
        yield "ballot", Choice(method, {"ranks": dict.fromkeys(map(str, positions), 1)})
        yield "ballot", blank
        yield "review", blank
        yield "review", Choice(method, {"ranks": everything})
        widths: dict[int, int] = {}
        for position in positions:
            widths.setdefault(len(str(position)), position)
        for position in widths.values():
            ranks = {key: rank for key, rank in everything.items() if key != str(position)}
            yield "review", Choice(method, {"ranks": ranks})
        for size in range(len(widths) + 1):
            for ranked in combinations(sorted(widths), size):
                ranks = {str(p): longest for p in positions if len(str(p)) in ranked}
                yield "review", Choice(method, {"ranks": ranks})
    else:
        count = _grade_count(poll)
        best = max(range(count), key=lambda grade: _size(grade_label(grade, count)))
        yield "ballot", Choice(method, {"grades": dict.fromkeys(map(str, positions), best)})
        yield "review", blank
        # Each option shows its grade with a hidden field, or the worst grade marked "not
        # graded" and its label in the warning that lists such options, with a separator.
        # Without the warning, which is longer depends only on the number of digits of its
        # position; the label and separator in the warning outweigh that difference, so the
        # longest summaries grade every option of some position widths and none of the
        # others. "None graded" is never shown (one grade is required) but bounds them all.
        widths = sorted({len(str(position)) for position in positions})
        for size in range(len(widths) + 1):
            for graded in combinations(widths, size):
                grades = {str(p): best for p in positions if len(str(p)) in graded}
                yield "review", Choice(method, {"grades": grades})


def _section_renderer(
    public_id: str, csrf_token: str, poll: Poll, focus: bool
) -> Callable[..., str]:
    macros: Any = templates.env.get_template(MACROS).make_module(
        {"public_id": public_id, "csrf_token": csrf_token}
    )

    def render(
        state: str,
        choice: Choice | None = None,
        error: str | None = None,
        message: str | None = None,
    ) -> str:
        return str(
            macros.poll_section(
                poll, state, choice=choice, error=error, message=message, focus=focus
            )
        )

    return render


def section_size(public_id: str, csrf_token: str, poll: Poll, *, focus: bool) -> int:
    """Bytes of the largest section any ballot step of ``poll`` can answer with."""
    render = _section_renderer(public_id, csrf_token, poll, focus)
    longest = _longest_message()
    return max(
        len(
            render(
                state,
                choice,
                error=longest if state == "ballot" else None,
                message=longest if state in ("voted", "closed") else None,
            ).encode()
        )
        for state, choice in _extremes(poll)
    )


def padded_section(
    public_id: str,
    csrf_token: str,
    poll: Poll,
    state: str,
    *,
    choice: Choice | None = None,
    error: str | None = None,
    message: str | None = None,
    focus: bool = False,
) -> Markup:
    """The poll's section for one ballot step, followed by a comment that brings it to the
    poll's ``section_size`` plus the 7 bytes of an empty comment."""
    html = _section_renderer(public_id, csrf_token, poll, focus)(state, choice, error, message)
    size = len(html.encode())
    target = section_size(public_id, csrf_token, poll, focus=focus)
    # A case ``_extremes`` missed would show in its size (never logged: that would date the
    # choice); tests/web/live/test_padding.py walks every choice of small polls.
    target = max(target, size)
    return Markup(html + "<!--" + " " * (target - size) + "-->")  # noqa: S704 (rendered, escaped)


def request_size(poll: Poll, csrf_token: str) -> int:
    """Size live.js brings every ballot-step request body of ``poll`` to (``data-pad``): every
    field such a request can carry, at its longest, and an empty ``pad`` field."""
    positions = [option.position for option in poll.options]
    if poll.method == "single_choice":
        fields = [("choice", BLANK_CHOICE)]  # as long as every position
    elif poll.method == "approval":
        fields = [("approved", number_field(position)) for position in positions]
    elif poll.method == "condorcet":
        # Every list is sent, "Non classée" as long as a rank.
        fields = [(rank_name(position), UNRANKED) for position in positions]
    else:
        worst = str(_grade_count(poll) - 1)
        fields = [(grade_name(position), worst) for position in positions]
    fields += [("blank", "1"), ("action", "edit"), ("csrf_token", csrf_token), (PAD_FIELD, "")]
    return len(urlencode(fields))


templates.env.globals["ballot_request_size"] = request_size
templates.env.globals.update(
    ballot_number=number_field,
    ballot_grade_name=grade_name,
    ballot_rank_name=rank_name,
    ballot_blank_choice=BLANK_CHOICE,
    ballot_unranked=UNRANKED,
)
