"""Smoke-Test der M1-Kette (T-039).

Referenziert N-OPS-01 (Demo-Daten per Befehl), N-OPS-04 (Health- und
Readiness-Endpunkt), N-OPS-05 (JSON-Logging) und N-DB-03
(Schema-Versionierung ueber Alembic).

Startet die App mit einer temporaeren SQLite-Datenbank ueber den Lifespan,
prueft Liveness und Readiness, legt die Demo-Daten an, kontrolliert die
Zeilenzahlen ueber das ORM (kein rohes SQL, N-DB-01) und prueft, dass die
waehrend des Starts erzeugte Log-Ausgabe als JSON auf stdout erscheint.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finue.config import Settings
from finue.demo import seed_demo
from finue.main import create_app
from finue.models.account import Account
from finue.models.bank import Bank
from finue.models.standing_order import StandingOrder
from finue.models.transaction import Transaction

pytestmark = pytest.mark.smoke

# Pflichtfelder jeder JSON-Log-Zeile laut ``finue.logging_setup.JSONFormatter``.
_REQUIRED_LOG_FIELDS = ("ts", "level", "logger", "message")


def _settings_for(tmp_path: Path) -> Settings:
    """Erzeugt Settings mit temporaerer SQLite-DATABASE_URL fuer den Smoke-Test."""
    url = f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}"
    return Settings(database_url=url, data_dir=tmp_path)


def _count_rows(session: Session, model: type[Any]) -> int:
    """Zaehlt die Zeilen einer Tabelle ueber das ORM (N-DB-01, kein rohes SQL)."""
    count = session.scalar(select(func.count()).select_from(model))
    return int(count or 0)


def _parse_log_lines(output: str) -> list[dict[str, Any]]:
    """Parst jede nicht-leere Log-Zeile als JSON-Objekt (N-OPS-05).

    Eine Zeile, die kein JSON-Objekt ist, laesst den Test scheitern: die
    Projekt-Logging-Konfiguration schreibt ausschliesslich JSON.
    """
    entries: list[dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Log-Zeile ist kein JSON: {stripped!r}") from exc
        assert isinstance(parsed, dict), f"Log-Zeile ist kein JSON-Objekt: {stripped!r}"
        entries.append(parsed)
    return entries


def test_smoke_m1_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """M1-Smoke: Start, Health, Seed und JSON-Logging greifen zusammen.

    Prueft in einem Durchlauf:
    - ``GET /api/health`` liefert 200 (N-OPS-04).
    - ``GET /api/health/ready`` liefert 200, weil die frische DB auf ``head``
      migriert ist (N-OPS-04, N-DB-03).
    - ``seed_demo`` befuellt die Datenbank (N-OPS-01); die Zeilenzahlen mehrerer
      Tabellen sind danach groesser als 0, geprueft ueber das ORM (N-DB-01).
    - Die waehrend des Starts erzeugte Log-Ausgabe auf stdout ist JSON mit den
      Feldern ``ts``, ``level``, ``logger`` und ``message`` (N-OPS-05).
    """
    # Ausgabe vor dem Test verwerfen, damit nur die App-Logs geprueft werden.
    capsys.readouterr()

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        app = create_app(_settings_for(tmp_path))

        # Der Kontextmanager fuehrt den Lifespan aus (Logging, Migration, Engine).
        with TestClient(app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            ready = client.get("/api/health/ready")
            assert ready.status_code == 200
            ready_body = ready.json()
            assert ready_body["status"] == "ok"
            assert ready_body["checks"]["database"] == "ok"
            assert ready_body["checks"]["schema"] == "ok"
            assert ready_body["checks"]["scheduler"] in {"not_required", "running"}

            # Demo-Daten ueber die Session-Factory derselben Engine anlegen.
            session_factory = app.state.session_factory
            with session_factory() as session:
                seed_demo(session, seed=42)

            # Zeilenzahlen ueber das ORM pruefen, nicht ueber rohes SQL.
            with session_factory() as session:
                counts = {
                    "account": _count_rows(session, Account),
                    "bank": _count_rows(session, Bank),
                    "transaction": _count_rows(session, Transaction),
                    "standing_order": _count_rows(session, StandingOrder),
                }
            for table, count in counts.items():
                assert count > 0, f"Tabelle {table!r} ist nach seed_demo leer: {counts!r}"
    finally:
        # Root-Handler wiederherstellen, damit der Test keine Fremdlogger bindet.
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

    captured = capsys.readouterr()
    entries = _parse_log_lines(captured.out)
    assert entries, "Keine Log-Ausgabe auf stdout waehrend des App-Starts"

    for entry in entries:
        for field in _REQUIRED_LOG_FIELDS:
            assert field in entry, f"Feld {field!r} fehlt im Log-Eintrag {entry!r}"

    main_entries = [entry for entry in entries if entry["logger"] == "finue.main"]
    messages = {entry["message"] for entry in main_entries}
    assert "Anwendung startet" in messages
    assert "Schema auf head migriert" in messages
