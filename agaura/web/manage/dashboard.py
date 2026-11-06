"""Dashboard: the account's sessions and session creation (FR-10, FR-11).

Creating a session asks for its title, date, place and access mode only; the registered members
and the quorum are set afterwards in the details of the session ("Séance", ``session.py``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, Response

from agaura.models import AccessMode
from agaura.services import assemblies, console
from agaura.services.assemblies import AssemblyInput
from agaura.services.errors import InvalidInputError, ServiceError
from agaura.web.deps import CsrfProtected, CurrentUser, DatabaseDep, KeyringDep
from agaura.web.manage.common import optional_date, redirect, router
from agaura.web.messages import error_message
from agaura.web.templating import flash, render

SESSION_FIELDS = ("title", "date", "place", "access_mode")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: CurrentUser, database: DatabaseDep) -> Response:
    with database.read() as db:
        return render(
            request,
            "manage/dashboard.html",
            {"summaries": console.summaries(db, user), "form": dict.fromkeys(SESSION_FIELDS, "")},
        )


@router.post("/dashboard", dependencies=[CsrfProtected])
def create_session(
    request: Request,
    user: CurrentUser,
    database: DatabaseDep,
    keyring: KeyringDep,
    title: Annotated[str, Form()] = "",
    date: Annotated[str, Form()] = "",
    place: Annotated[str, Form()] = "",
    access_mode: Annotated[str, Form()] = "password",
) -> Response:
    form = {"title": title, "date": date, "place": place, "access_mode": access_mode}
    try:
        if user.email_verified_at is None:
            raise InvalidInputError("email_not_verified")
        data = AssemblyInput(title=title, date=optional_date(date), place=place)
        mode = AccessMode.CODES if access_mode == "codes" else AccessMode.PASSWORD
        with database.write() as db:
            public_id = assemblies.create_assembly(
                db, keyring, user, data, access_mode=mode
            ).public_id
    except ServiceError as error:
        with database.read() as db:
            return render(
                request,
                "manage/dashboard.html",
                {
                    "summaries": console.summaries(db, user),
                    "form": form,
                    "error": error_message(error),
                },
                status_code=400,
            )
    response = redirect(public_id)
    flash(response, request, "session_created")
    return response
