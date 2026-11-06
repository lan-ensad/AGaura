"""Dashboard and session console (FR-10 to FR-17, FR-20 to FR-36, FR-60 to FR-63, FR-84).

Every route takes the current account and reaches sessions only through owner-scoped services,
so another account's session answers 404 (FR-07). Actions follow POST/redirect/GET; after each
change, voter pages, the projection screen and other console tabs are told to refresh through
the real-time hub.

All modules declare their routes on the one ``router`` of ``common``; importing them here
registers the routes, in this order.
"""

from agaura.web.manage import (  # noqa: F401 (imported for their routes)
    access,
    codes,
    dashboard,
    delegates,
    editor,
    exports,
    links,
    meeting,
    notes,
    polls,
    session,
    timer,
)
from agaura.web.manage.common import router
from agaura.web.manage.delegates import entry_router

__all__ = ["entry_router", "router"]
