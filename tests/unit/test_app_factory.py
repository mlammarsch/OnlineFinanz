"""Test der FastAPI-App-Fabrik (T-033).

Referenziert N-DB-03 (Schema-Versionierung ueber Alembic; Migration beim Start)
und N-OPS-05 (Logging und Konfiguration beim Start).

Startet die App mit einer temporaeren, datei-basierten SQLite-Datenbank ueber den
Lifespan (``with TestClient(app) as client:``) und prueft, dass der Start ohne
Fehler gelingt und die Datenbank danach auf ``head`` migriert ist. Ein zweiter
Test prueft die ``get_session``-Dependency aus ``finue.api.deps``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from finue.api.deps import get_session
from finue.config import Settings
from finue.db.engine import create_db_engine
from finue.db.migrate import is_schema_current
from finue.log_filter import LogFilter
from finue.logging_setup import JSONFormatter
from finue.main import create_app


def _settings_for(tmp_path: Path) -> Settings:
    """Erzeugt Settings mit temporaerer SQLite-DATABASE_URL fuer Tests."""
    url = f"sqlite:///{(tmp_path / 'app_factory.db').as_posix()}"
    return Settings(database_url=url, data_dir=tmp_path)


def test_app_startup_migrates_schema(tmp_path: Path) -> None:
    """Start der App gelingt und migriert die DB auf head (N-DB-03, N-OPS-05)."""
    settings = _settings_for(tmp_path)
    app = create_app(settings)

    # Der Kontextmanager fuehrt den Lifespan (inkl. Migration) aus.
    with TestClient(app) as client:
        assert client is not None
        assert app.state.engine is not None
        assert app.state.session_factory is not None

    # Nach dem Start ist die temporaere Datenbank auf head.
    engine = create_db_engine(settings.database_url)
    try:
        assert is_schema_current(engine) is True
    finally:
        engine.dispose()


def test_get_session_dependency(tmp_path: Path) -> None:
    """get_session liefert eine Session aus app.state.session_factory (N-DB-01)."""
    app = create_app(_settings_for(tmp_path))

    @app.get("/_probe")
    def probe(session: Session = Depends(get_session)) -> dict[str, bool]:
        """Test-Route, die die injizierte Session nutzt."""
        return {"has_session": session is not None}

    with TestClient(app) as client:
        response = client.get("/_probe")

    assert response.status_code == 200
    assert response.json() == {"has_session": True}


def test_app_startup_keeps_project_logging(tmp_path: Path) -> None:
    """Nach dem App-Start traegt der Root-Logger Projekt-Filter und JSON (N-SEC-02).

    Regression zu T-033: Die Migration im Lifespan rief Alembics ``fileConfig``
    auf und ersetzte den Projekt-Handler durch den Standard-Handler (stderr,
    ohne Maskierung). Geprueft wird der Root-Logger waehrend des laufenden
    Lifespans, also nach ``configure_logging`` und ``upgrade_to_head``.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        app = create_app(_settings_for(tmp_path))
        with TestClient(app):
            handlers = root.handlers
            assert len(handlers) == 1, f"Erwartet 1 Root-Handler, gefunden {len(handlers)}"
            handler = handlers[0]
            assert isinstance(handler.formatter, JSONFormatter)
            assert any(isinstance(log_filter, LogFilter) for log_filter in handler.filters)
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
