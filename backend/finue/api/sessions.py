"""REST-Router der Sitzungsuebersicht (F-CON-06, N-SEC-02).

- ``GET /api/sessions`` listet die aktiven Sitzungen (Geraet/Browser, letzter
  Zugriff) und kennzeichnet die Sitzung der laufenden Anfrage, sobald eine
  Anmeldung existiert (N-SEC-04, M10).
- ``DELETE /api/sessions/{id}`` meldet eine einzelne Sitzung ab; der Aufruf ist
  idempotent (ein unbekanntes ``id`` antwortet ebenfalls 204).

Die Fachlogik liegt in :mod:`finue.services.sessions`. ``token_hash`` verlaesst
das Backend nie (N-SEC-02): die DTOs tragen nur ID, Geraetetext und Zeitpunkte.

Der Router bringt ``prefix="/api"`` und die Auth-Naht ``require_user`` selbst
mit (T-088-Konvention).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.deps import CurrentUser, get_services, get_session, require_user
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.services.sessions import SessionSummary, list_active_sessions, revoke_session

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["sessions"], dependencies=[Depends(require_user)])


class SessionView(BaseModel):
    """Eine aktive Sitzung als Anzeige (F-CON-06)."""

    id: int
    device: str
    last_seen_at: datetime
    expires_at: datetime
    current: bool


class SessionListResponse(BaseModel):
    """Aktive Sitzungen mit Cursor (SPEC F-MCP-11)."""

    items: list[SessionView]
    next_cursor: str | None = None


def _to_view(summary: SessionSummary) -> SessionView:
    """Uebersetzt ein Service-Ergebnis in das API-DTO (F-CON-06)."""

    return SessionView(
        id=summary.id,
        device=summary.device,
        last_seen_at=summary.last_seen_at,
        expires_at=summary.expires_at,
        current=summary.current,
    )


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> SessionListResponse:
    """Aktive Sitzungen, zuletzt genutzte zuerst (F-CON-06).

    ``limit`` und ``cursor`` werden ueber :func:`resolve_page_params` aus der
    Anfrage gelesen und gegen die zentralen Grenzen geprueft (SPEC F-MCP-11); die
    gleichnamigen Parameter traegt die Signatur fuer den Vertragstest.
    """

    params = resolve_page_params(request, services.settings)
    summaries = list_active_sessions(
        session,
        now=services.clock(),
        current_session_id=user.session_id,
    )
    offset = decode_cursor(params.cursor) if params.cursor is not None else 0
    window = summaries[offset : offset + params.limit]
    next_offset = offset + params.limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(summaries) else None
    return SessionListResponse(items=[_to_view(item) for item in window], next_cursor=next_cursor)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: int,
    session: Session = Depends(get_session),
) -> Response:
    """Meldet eine einzelne Sitzung ab; wiederholte Aufrufe bleiben wirkungslos.

    Idempotent (F-CON-06): ein unbekanntes oder bereits abgemeldetes ``id``
    liefert ebenfalls 204. Es wird kein Sitzungsgeheimnis ausgeliefert (N-SEC-02).
    """

    revoke_session(session, session_id)
    return Response(status_code=204)
