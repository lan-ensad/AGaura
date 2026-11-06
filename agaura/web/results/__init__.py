"""Public results page (FR-17): ``/{session_id}/results/{token}``.

The secret link is enabled, regenerated or disabled from the console; without a valid token
(link disabled, regenerated, or wrong) the page answers 404, exactly like a missing session. It
shows, in the session order, the polls whose result was published at least once and the
cancelled polls with their reason; never drafts, open polls, or results not yet published. No
voter data, no live updates (members refresh the page), never cached.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agaura.services import access, assemblies, exports
from agaura.services.errors import NotFoundError
from agaura.web.deps import DatabaseDep, KeyringDep
from agaura.web.templating import render

router = APIRouter()


@router.get("/{session_id}/results/{token}", response_class=HTMLResponse)
def public_results(
    request: Request, session_id: str, token: str, database: DatabaseDep, keyring: KeyringDep
) -> Response:
    public_id = assemblies.normalize_public_id(session_id)
    if public_id is None:
        raise NotFoundError("assembly")
    with database.read() as db:
        assembly = access.check_link_token(db, keyring, public_id, token, public=True)
        if session_id != assembly.public_id:
            response: Response = RedirectResponse(
                f"/{assembly.public_id}/results/{token}", status_code=303
            )
        else:
            polls = exports.public_results(db, assembly)
            response = render(request, "results/page.html", {"assembly": assembly, "polls": polls})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
