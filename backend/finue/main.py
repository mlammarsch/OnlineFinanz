"""FastAPI-App-Fabrik der Finanzuebersicht.

Implementiert N-DB-03 (Schema-Versionierung ueber Alembic; Migration beim Start)
und N-OPS-05 (Logging und Konfiguration). Der Lifespan richtet Logging ein,
erzeugt die Engine, hebt das Schema auf ``head`` und stellt Engine und
Session-Factory in ``app.state`` bereit.

Der Server laeuft gemaess D3 mit genau einem Worker-Prozess. Der Migrationslauf
beim Start ist zugleich idempotent (``upgrade_to_head`` auf einer aktuellen
Datenbank ist ein No-Op), weil Alembic die Revision vor dem Lauf prueft.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from finue.api.container import AppServices
from finue.api.errors import register_exception_handlers
from finue.api.routes import build_router
from finue.config import Settings, get_settings
from finue.db.engine import create_db_engine, make_session_factory
from finue.db.migrate import upgrade_to_head
from finue.logging_setup import configure_logging
from finue.services.events import EventBus
from finue.services.sync_registry import SyncRegistry
from finue.timeutil import utc_now

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, clock: Callable[[], datetime] | None = None) -> FastAPI:
    """Erzeugt und konfiguriert die FastAPI-Anwendung.

    Ohne ``settings`` werden die Standard-Settings aus Umgebungsvariablen
    geladen (``finue.config.get_settings``). Tests koennen eigene Settings mit
    einer temporaeren ``DATABASE_URL`` injizieren, ohne globale Umgebungsvariablen
    zu setzen.

    Der Lifespan richtet das JSON-Logging ein (N-OPS-05, N-SEC-02 maskiert
    Geheimnisse), erzeugt die SQLAlchemy-Engine, migriert das Schema auf ``head``
    (N-DB-03) und hinterlegt den Service-Container in ``app.state`` (D12):
    Engine/Session-Factory, Ereignisbus (F-CON-04), Sync-Registry als Singleton
    und die Uhr. Beim Herunterfahren wird die Engine geschlossen.

    Args:
        settings: Optionale Konfiguration; ohne Angabe die Projekt-Defaults.
        clock: Injizierbare Uhr (Default ``finue.timeutil.utc_now``) fuer Bus,
            Sync-Registry und Tests.

    Returns:
        Konfigurierte FastAPI-Instanz.
    """
    resolved = settings if settings is not None else get_settings()
    resolved_clock: Callable[[], datetime] = clock if clock is not None else utc_now

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved.log_level)
        logger.info("Anwendung startet")

        engine = create_db_engine(resolved.database_url)
        upgrade_to_head(resolved.database_url)
        logger.info("Schema auf head migriert")

        session_factory = make_session_factory(engine)
        event_bus = EventBus(clock=resolved_clock)
        sync_registry = SyncRegistry(clock=resolved_clock)
        services = AppServices(
            settings=resolved,
            session_factory=session_factory,
            event_bus=event_bus,
            sync_registry=sync_registry,
            clock=resolved_clock,
        )

        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.services = services
        app.state.event_bus = event_bus
        app.state.sync_registry = sync_registry
        app.state.clock = resolved_clock
        # Der Zeitplaner kommt erst in M8; bis dahin ist er nicht gestartet.
        app.state.scheduler_running = False
        try:
            yield
        finally:
            engine.dispose()
            logger.info("Anwendung beendet")

    app = FastAPI(title="Finanzuebersicht", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(build_router())
    return app


app = create_app()
"""Modul-Level-Instanz fuer ``uvicorn finue.main:app``."""
