"""Session notes: what the tribune writes down during the assembly, for the minutes.

Notes are written by the tribune, never by voters: their timestamps say nothing about anyone's
vote. A note keeps the agenda item that was under discussion when it was written, as its
position *and* its title, so that it can still be placed after the agenda is edited (see
``services/meeting.locate_item``).
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agaura.models.base import Base, utcnow

if TYPE_CHECKING:
    from agaura.models.assembly import Assembly


class Note(Base):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    assembly_id: Mapped[int] = mapped_column(
        ForeignKey("assembly.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)  # plain text, line breaks kept
    # Internal notes stay in the console: never in the minutes or the exports.
    internal: Mapped[bool] = mapped_column(default=False)
    # Agenda item the note belongs to: under discussion when it was written, or chosen later.
    item_position: Mapped[int | None]
    item_title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime | None]  # last edit; None if never edited

    assembly: Mapped[Assembly] = relationship()
