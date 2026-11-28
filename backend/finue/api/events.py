"""Server-Sent-Events-Route ``GET /api/events`` (T-101, F-CON-04).

Liefert alle Ereignisse des In-Prozess-Busses an die verbundenen Sitzungen;
die UI laedt daraufhin die betroffenen Daten nach (F-CON-04, F-UI-07). Der
Strom traegt nur Typ und IDs (plus den Challenge-Text, wo F-SYN-04 ihn in allen
Sitzungen anzeigen will); PIN, TAN und Dialogdaten gelangen nie hinein
(N-SEC-02/03). Die Route haelt **keine** DB-Session offen (kein ``get_session``)
und arbeitet mit dem einen Worker-Prozess (D3). Der Router bringt
``prefix="/api"`` und die Auth-Naht ``require_user`` selbst mit (T-088).

Verhalten:

- Heartbeat-Kommentar alle ``api_sse_heartbeat_seconds`` (Standard 15 s).
- Fortlaufende Event-ID je Ereignis (SSE-Feld ``id``).
- ``Last-Event-ID`` wird nachgeliefert, solange der Ringpuffer reicht; sonst
  folgt ein ``resync`` (UI laedt neu).
- Begrenzte Warteschlange je Client; Ueberlauf liefert ``resync`` statt
  Speicherwachstum.
- Disconnect meldet die Verbindung ab (kein Listener-Leck).

Die Rahmen-Erzeugung und der Verteiler liegen in ``finue.services.events``
(EventBus aus T-086). Die Sync-Ereignisse ``sync.status``/``sync.tan_required``/
``sync.tan_answered`` stammen aus dem Runner (T-112) und erreichen ueber den Bus
alle Clients.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from finue.api.container import AppServices
from finue.api.deps import get_services, require_user
from finue.services.events import SseBroadcaster, stream_sse

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["events"], dependencies=[Depends(require_user)])

#: SSE-Antwortkopf: kein Zwischenspeichern, kein Puffern durch einen Proxy (T-101).
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def _parse_last_event_id(raw: str | None) -> int | None:
    """Liest den ``Last-Event-ID``-Kopf; ungueltige Werte gelten als frisch."""

    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


@router.get("/events")
def stream_events(request: Request, services: AppServices = Depends(get_services)) -> StreamingResponse:
    """Oeffnet den SSE-Strom aller Aenderungsereignisse (F-CON-04)."""

    broadcaster: SseBroadcaster = services.sse_broadcaster
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
    connection = broadcaster.open(last_event_id)
    return StreamingResponse(
        stream_sse(broadcaster, connection, disconnected=request.is_disconnected),
        media_type="text/event-stream",
        headers=dict(_SSE_HEADERS),
        # Faengt das Leck ab, falls der Client trennt, bevor der Generator startet
        # (`finally` in `stream_sse` liefe dann nie); `discard` ist idempotent.
        background=BackgroundTask(broadcaster.discard, connection),
    )
