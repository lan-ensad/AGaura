"""Individual codes: generation (shown once, as card PDF or printable page), revocation (FR-23).

Codes exist in clear only in the response that creates them: the database keeps their HMAC
digests. They are never logged, never stored, and the response is marked ``no-store``.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Annotated

from fastapi import Form, Request
from fastapi.responses import Response

from agaura.pdf import render_pdf
from agaura.services import access, assemblies
from agaura.services.errors import NotFoundError, ServiceError
from agaura.web.deps import CsrfProtected, DatabaseDep, KeyringDep, RowId
from agaura.web.manage.common import (
    ConsoleUser,
    OwnedSession,
    optional_int,
    pdf_rendering,
    qr_svg,
    redirect,
    router,
    run_action,
    voter_url,
)
from agaura.web.templating import flash_error, render

CARDS_PER_PAGE = 10  # 2 columns by 5 rows on A4 (FR-23)
NO_STORE = {"Cache-Control": "no-store"}


@router.post("/manage/{public_id}/codes", dependencies=[CsrfProtected])
def generate_codes(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    database: DatabaseDep,
    keyring: KeyringDep,
    count: Annotated[str, Form()] = "",
    output: Annotated[str, Form()] = "",
) -> Response:
    """Generate a batch and return its cards: ``output=pdf`` downloads the card PDF, anything
    else shows the printable page. Only the digests are stored (FR-23).

    For the PDF, the renderer is reserved before the codes exist: when it is busy or the account
    is over its limit, the answer is 503 or 429 and no batch is created, so no code is lost."""
    pdf = output == "pdf"
    with pdf_rendering(request, user) if pdf else nullcontext():
        try:
            size = optional_int(count, minimum=1) or 0
            with database.write() as db:
                created = access.create_code_batch(db, keyring, user, public_id, size)
                batch_id = created.batch.id
                title = assemblies.get_assembly(db, user, public_id).title
        except NotFoundError:
            raise
        except ServiceError as error:
            response = redirect(public_id, "#codes")
            flash_error(response, request, error.code)
            return response
        # The write transaction is over: rendering (slow for a PDF) holds no database lock.
        url = voter_url(request, public_id)
        cards = [{"code": code, "qr": qr_svg(f"{url}#k={code}")} for code in created.codes]
        context = {
            "title": title,
            "voter_url": url,
            "display_url": url.split("://", 1)[-1],
            "public_id": public_id,
            "cards": cards,
        }
        if pdf:
            pages = [
                cards[start : start + CARDS_PER_PAGE]
                for start in range(0, len(cards), CARDS_PER_PAGE)
            ]
            document = render_pdf("manage/cards_pdf.html", {**context, "pages": pages})
            filename = f"agaura-{public_id}-codes-{batch_id}.pdf"
            return Response(
                document,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"', **NO_STORE},
            )
    response = render(request, "manage/codes.html", context)
    response.headers.update(NO_STORE)
    return response


@router.post("/manage/{public_id}/codes/{batch_id}/revoke", dependencies=[CsrfProtected])
def revoke_codes(
    request: Request, public_id: OwnedSession, user: ConsoleUser, batch_id: RowId
) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: access.revoke_code_batch(db, user, public_id, batch_id),
        notice="codes_revoked",
        voters="session",
        anchor="#codes",
    )
