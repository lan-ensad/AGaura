"""Attendance readings as table rows, for the console and the PDF minutes (FR-28, FR-62).

Each row says when (Paris time), what happened ("Vote n° 2 lancé"), the headcount entered by the
tribune and the connections at that moment; a missing count (older entries) stays None.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agaura.i18n import N_, _
from agaura.services.attendance import Reading
from agaura.web.templating import DISPLAY_TIMEZONE, short_datetime, time_of_day

LABELS = {
    "session_opened": N_("Session opened"),
    "headcount_set": N_("Headcount entered"),
    "poll_opened": N_("Poll no. %(number)s pushed"),
    "poll_closed": N_("Poll no. %(number)s closed"),
    "session_closed": N_("Session closed"),
}
POLL_EVENTS = ("poll_opened", "poll_closed")


@dataclass(frozen=True, slots=True)
class Row:
    at: dt.datetime
    when: str  # "14:05", or "20 juin 2026, 14:05" when the readings span several days
    label: str
    headcount: int | None
    attendees: int | None


def label(reading: Reading, numbers: Mapping[int, int]) -> str:
    text = _(LABELS[reading.event])
    if reading.event in POLL_EVENTS:
        number = numbers.get(reading.poll_id) if reading.poll_id is not None else None
        return text % {"number": "?" if number is None else number}
    return text


def rows(readings: Sequence[Reading], numbers: Mapping[int, int]) -> list[Row]:
    """One row per reading, in the order given; ``numbers`` maps poll ids to poll numbers."""
    days = {reading.at.astimezone(DISPLAY_TIMEZONE).date() for reading in readings}
    when = time_of_day if len(days) <= 1 else short_datetime
    return [
        Row(
            at=reading.at,
            when=when(reading.at),
            label=label(reading, numbers),
            headcount=reading.headcount,
            attendees=reading.attendees,
        )
        for reading in readings
    ]
