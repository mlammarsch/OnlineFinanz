"""Service-Container der Anwendung (D12).

Der Container buendelt die langlebigen Dienste einer App-Instanz und liegt in
``app.state.services``. Damit bekommen Routen ihre Abhaengigkeiten ueber
FastAPI-Dependencies (``finue.api.deps``) statt ueber globalen Zustand.

Enthalten sind die Session-Factory, der Ereignisbus, die Sync-Registry als
Singleton je App und die injizierbare Uhr. Ein Test kann ``create_app`` eine
eigene Uhr mitgeben, ohne Systemzeit zu stubben.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.services.events import EventBus
from finue.services.sync_registry import SyncRegistry

__all__ = ["AppServices"]


@dataclass(frozen=True, slots=True)
class AppServices:
    """Gebundelte Laufzeitdienste einer Anwendung."""

    settings: Settings
    session_factory: sessionmaker[Session]
    event_bus: EventBus
    sync_registry: SyncRegistry
    clock: Callable[[], datetime]
