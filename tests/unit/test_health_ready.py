"""Tests des Readiness-Endpunkts (T-035).

Referenziert N-OPS-04 (Betriebsbereitschaft) und N-DB-03 (Schema-Versionierung).
Geprueft werden drei Faelle: alle Pruefungen bestehen (200), die Datenbank ist
nicht erreichbar (503) und das Schema ist nicht aktuell (503).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from finue.config import Settings
from finue.main import create_app


def _settings_for(tmp_path: Path) -> Settings:
    """Erzeugt Settings mit temporaerer SQLite-DATABASE_URL fuer Tests."""
    url = f"sqlite:///{(tmp_path / 'health_ready.db').as_posix()}"
    return Settings(database_url=url, data_dir=tmp_path)


class _UnreachableEngine:
    """Engine-Attrappe, deren Verbindungsaufbau immer scheitert (N-OPS-04)."""

    def connect(self) -> Any:
        """Simuliert eine nicht erreichbare Datenbank ohne echte Verbindung."""
        raise SQLAlchemyError("Verbindung nicht moeglich")


def test_ready_all_ok(tmp_path: Path) -> None:
    """Alle erforderlichen Pruefungen bestehen -> 200 mit Details (N-OPS-04)."""
    app = create_app(_settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["schema"] == "ok"
    # Bis M8 ist der Zeitplaner nur gemeldet, nicht erforderlich.
    assert body["checks"]["scheduler"] == "not_required"


def test_ready_database_unreachable(tmp_path: Path) -> None:
    """Nicht erreichbare Datenbank -> 503 ohne interne Details (N-OPS-04, N-SEC-02)."""
    app = create_app(_settings_for(tmp_path))

    with TestClient(app) as client:
        # Nach dem Start die Engine durch eine Attrappe ersetzen, die beim
        # Verbindungsaufbau scheitert. Deterministisch und ohne echte DB-Fehler.
        app.state.engine = _UnreachableEngine()
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["database"] == "error"
    assert "Datenbank nicht erreichbar" in body["message"]
    # Keine Geheimnisse oder internen Details in der Antwort (N-SEC-02).
    assert "sqlite" not in response.text.lower()
    assert "Traceback" not in response.text


def test_ready_schema_outdated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nicht aktuelles Schema -> 503 mit Klartext (N-DB-03)."""
    # Bei nur einer Migration ist eine echte alte Revision nicht sinnvoll
    # darstellbar; der Schemastand wird deshalb gezielt als veraltet gemeldet.
    monkeypatch.setattr("finue.api.health.is_schema_current", lambda engine: False)

    app = create_app(_settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["schema"] == "error"
    assert "Datenbankschema nicht aktuell" in body["message"]
