"""Fixtures of the poll editor tests."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from agaura.models import Poll
from tests.conftest import World


def polls_of(world: World) -> list[Poll]:
    """Every poll of the database, options and session loaded, in creation order."""
    with world.database.read() as db:
        rows = list(db.scalars(select(Poll).order_by(Poll.id)))
        for poll in rows:
            _ = poll.options, poll.assembly
        return rows


@pytest.fixture
def form() -> dict[str, Any]:
    """A valid single choice poll as step 2 of the editor sends it, empty rows included, saved
    as a draft (``action``: the button pressed).

    Lists are repeated form fields (``post`` sends ``option_label=…&option_label=…``).
    """
    return {
        "title": "Lieu de la prochaine assemblée",
        "description": "",
        "kind": "single_choice",
        "action": "draft",
        "option_label": ["Salle des fêtes", "Médiathèque", "", ""],
        "option_description": ["Centre-ville", "", "", ""],
        "threshold": "auto",
        "threshold_percent": "",
        "base": "valid",
        "seats": "1",
        "max_choices": "seats",
        "max_choices_number": "",
        "min_share_percent": "",
    }
