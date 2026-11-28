"""FastAPI-Dependencies fuer die API-Schicht.

Stellt die Datenbank-Session sowie die uebrigen Dienste aus dem
Service-Container bereit. Engine, Session-Factory, Ereignisbus, Sync-Registry
und Uhr werden beim App-Start im Lifespan gesetzt (``finue.main``) und hier aus
``app.state`` gelesen, damit kein globaler Zustand noetig ist (N-DB-01,
N-OPS-05).

``require_user`` ist die Authentifizierungs-Naht (F-CON-02, N-SEC-04): sie haengt
in M4 an jeder ``/api``-Route ausser ``/api/health*``, prueft aber noch nichts.
Ist ``auth_required`` gesetzt, meldet sie bis zur echten Anmeldung (M10) 401.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from finue.api.container import AppServices
from finue.api.errors import UnauthorizedError
from finue.services.events import EventBus, SseBroadcaster
from finue.services.import_uploads import UploadStore
from finue.services.sync_registry import SyncRegistry

__all__ = [
    "CurrentUser",
    "get_clock",
    "get_event_bus",
    "get_services",
    "get_session",
    "get_sse_broadcaster",
    "get_sync_registry",
    "get_upload_store",
    "require_user",
]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Angemeldeter Nutzer.

    In M4 ist immer der Owner angemeldet (``authenticated`` bleibt ``False``);
    M10 fuellt die Sitzung und setzt ``authenticated`` (N-SEC-04). ``session_id``
    ist die Naht fuer die Sitzungsuebersicht (F-CON-06): die Route
    ``GET /api/sessions`` kennzeichnet damit die laufende Sitzung. In M4 bleibt
    der Wert ``None``, weil es noch keine Anmeldung gibt.
    """

    name: str = "owner"
    authenticated: bool = False
    session_id: int | None = None


def get_services(request: Request) -> AppServices:
    """Liefert den Service-Container aus ``app.state`` (D12)."""

    services: AppServices = request.app.state.services
    return services


def get_session(request: Request) -> Iterator[Session]:
    """Liefert eine SQLAlchemy-Session als FastAPI-Dependency.

    Liest die Session-Factory aus ``app.state.session_factory`` (im Lifespan von
    ``finue.main`` gesetzt), oeffnet eine Session und schliesst sie nach dem
    Request wieder. Der Generator-Aufbau stellt das garantierte Schliessen auch
    bei Fehlern in der Route sicher (N-DB-01).

    Args:
        request: Aktueller FastAPI-Request, ueber den ``app.state`` erreichbar ist.

    Yields:
        Eine offene SQLAlchemy-Session.
    """
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_event_bus(request: Request) -> EventBus:
    """Liefert den Ereignisbus aus dem Service-Container (F-CON-04)."""

    return get_services(request).event_bus


def get_sse_broadcaster(request: Request) -> SseBroadcaster:
    """Liefert den SSE-Verteiler aus dem Service-Container (F-CON-04, T-101)."""

    return get_services(request).sse_broadcaster


def get_sync_registry(request: Request) -> SyncRegistry:
    """Liefert die Sync-Registry (Singleton je App) aus dem Container (D12)."""

    return get_services(request).sync_registry


def get_upload_store(request: Request) -> UploadStore:
    """Liefert den Zwischenspeicher fuer Import-Uploads aus dem Container (F-IMP-01)."""

    return get_services(request).upload_store


def get_clock(request: Request) -> Callable[[], datetime]:
    """Liefert die injizierbare Uhr aus dem Service-Container."""

    return get_services(request).clock


def require_user(request: Request) -> CurrentUser:
    """Auth-Naht an allen ``/api``-Routen ausser ``/api/health*`` (N-SEC-04).

    In M4 ohne Pruefung: die Anmeldung ist noch nicht umgesetzt (Entscheidung in
    ``decisions.md``), daher kommt der Owner ohne Nachweis durch. Mit gesetztem
    ``auth_required`` liefert die Naht bereits 401, bis M10 die Sitzung verdrahtet.
    """

    settings = get_services(request).settings
    if settings.auth_required:
        raise UnauthorizedError()
    return CurrentUser()
