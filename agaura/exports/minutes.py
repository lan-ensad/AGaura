"""PDF minutes of a session (FR-62), rendered with WeasyPrint from ``exports/minutes.html``.

Build the ``SessionRecord`` in a read transaction, then render after it has ended: rendering
takes a noticeable fraction of a second and must never hold the database.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from agaura.exports import attendance, proceedings
from agaura.pdf import render_pdf
from agaura.services.attendance import worth_reporting
from agaura.services.exports import SessionRecord
from agaura.web.templating import templates

TEMPLATE = "exports/minutes.html"


def context(session: SessionRecord, *, now: dt.datetime) -> dict[str, Any]:
    cancelled = sum(record.poll.status.value == "cancelled" for record in session.polls)
    return {
        "session": session,
        "assembly": session.assembly,
        "polls": session.polls,
        "cancelled_count": cancelled,
        "generated_at": now,
        # Only when it says more than "the session opened" (FR-28).
        "attendance": attendance.rows(session.attendance, session.numbers)
        if worth_reporting(session.attendance)
        else [],
        # Notes and polls by agenda item, when it says more than the list of polls.
        "proceedings": proceedings.groups(session) if proceedings.worth_showing(session) else [],
    }


def html(session: SessionRecord, *, now: dt.datetime) -> str:
    """The HTML given to WeasyPrint, for tests and proofreading."""
    return templates.env.get_template(TEMPLATE).render(context(session, now=now))


def render(session: SessionRecord, *, now: dt.datetime) -> bytes:
    return render_pdf(TEMPLATE, context(session, now=now))
