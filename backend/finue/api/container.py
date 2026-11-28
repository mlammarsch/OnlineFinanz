"""Service-Container der Anwendung (D12).

Der Container buendelt die langlebigen Dienste einer App-Instanz und liegt in
``app.state.services``. Damit bekommen Routen ihre Abhaengigkeiten ueber
FastAPI-Dependencies (``finue.api.deps``) statt ueber globalen Zustand.

Enthalten sind die Session-Factory, der Ereignisbus, der SSE-Verteiler fuer
``GET /api/events`` (T-101), die Sync-Registry als Singleton je App, der
:class:`~finue.services.sync_service.SyncService` mit dem
Hintergrund-Runner (T-112, D3), der Upload-Zwischenspeicher fuer den
Datei-Import und die injizierbare Uhr. Ein Test kann ``create_app`` eine eigene
Uhr und eine eigene FinTS-Client-Fabrik mitgeben, ohne Systemzeit zu stubben
oder echte Bankzugriffe auszuloesen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.services.events import EventBus, SseBroadcaster
from finue.services.import_uploads import UploadStore
from finue.services.sync_registry import SyncRegistry
from finue.services.sync_runner import SyncRunner
from finue.services.sync_service import SyncService

__all__ = ["AppServices"]


@dataclass(frozen=True, slots=True)
class AppServices:
    """Gebundelte Laufzeitdienste einer Anwendung."""

    settings: Settings
    session_factory: sessionmaker[Session]
    event_bus: EventBus
    sse_broadcaster: SseBroadcaster
    sync_registry: SyncRegistry
    sync_service: SyncService
    sync_runner: SyncRunner
    upload_store: UploadStore
    clock: Callable[[], datetime]
