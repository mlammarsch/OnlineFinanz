"""Tests des API-Grundgeruests (T-086).

Referenziert F-CON-02 (Versionsfeld, 409 mit aktuellem Stand), F-CON-03
(Batch-Ergebnisumschlag), F-CON-04 (Ereignisbus im Container), N-SEC-02 (keine
Geheimnisse in Fehlern), N-DB-01/N-DB-04 (kein rohes Daten-SQL, SQLite WAL) sowie
SPEC F-MCP-11 (Seitengrenzen). Geprueft werden Fehlerformat, Paginierung, Batch,
Herkunft, die Auth-Naht, der Service-Container und die App-Fixture.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from starlette.requests import Request

from finue.api.conventions import (
    REST_ORIGIN,
    BatchItemResult,
    BatchOutcome,
    BatchResponse,
    VersionedRequest,
    check_batch_size,
    check_upload_size,
)
from finue.api.deps import CurrentUser, require_user
from finue.api.errors import (
    ConflictError,
    DomainError,
    PayloadTooLargeError,
    ValidationApiError,
    error_body,
    localize_validation_errors,
)
from finue.api.pagination import Page, decode_cursor, encode_cursor, resolve_page_params
from finue.config import Settings
from finue.main import create_app
from finue.services.events import EventBus
from finue.services.sync_registry import SyncRegistry


def _request(query: str = "") -> Request:
    """Baut einen minimalen Starlette-Request mit Query-String fuer Unit-Tests."""

    return Request({"type": "http", "query_string": query.encode("ascii"), "headers": []})


def _settings(**overrides: Any) -> Settings:
    """Settings fuer Unit-Tests ohne Zugriff auf Umgebungsvariablen."""

    return Settings(database_url="sqlite:///:memory:", **overrides)


class _AccountDTO(BaseModel):
    """Kleines DTO, das den 409-Stand vertritt (F-CON-02)."""

    id: int
    name: str
    version: int


def _probe_app(**settings_overrides: Any) -> FastAPI:
    """App mit Test-Routen fuer Fehler-, Auth- und Container-Pfade."""

    app = create_app(_settings(**settings_overrides))

    @app.get("/api/_probe/validation")
    def probe_validation(limit: int) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/api/_probe/conflict")
    def probe_conflict() -> dict[str, bool]:
        raise ConflictError(current=_AccountDTO(id=1, name="Giro", version=7))

    @app.get("/api/_probe/domain")
    def probe_domain() -> dict[str, bool]:
        raise DomainError("Die Aktion ist im aktuellen Zustand nicht moeglich.")

    @app.get("/api/_probe/boom")
    def probe_boom() -> dict[str, bool]:
        raise RuntimeError("interner Fehler mit moeglichem Geheimnis pin=1234")

    @app.get("/api/_probe/whoami")
    def probe_whoami(user: CurrentUser = Depends(require_user)) -> dict[str, object]:
        return {"name": user.name, "authenticated": user.authenticated}

    @app.get("/api/_probe/container")
    def probe_container(request: Request) -> dict[str, object]:
        services = request.app.state.services
        return {
            "has_bus": isinstance(services.event_bus, EventBus),
            "has_registry": isinstance(services.sync_registry, SyncRegistry),
            "clock_aware": services.clock().tzinfo is not None,
        }

    return app


def test_unbekannte_route_liefert_404_im_format(client: TestClient) -> None:
    """Unbekannte Route: 404 mit {code, message, details} und deutschem Klartext (F-CON-02)."""
    response = client.get("/api/gibt-es-nicht")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"code", "message", "details"}
    assert body["code"] == "not_found"
    assert "nicht gefunden" in body["message"]


def test_falsche_methode_liefert_405(client: TestClient) -> None:
    """Falsche Methode auf /api/health: 405 im einheitlichen Format (F-CON-02)."""
    response = client.post("/api/health")

    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_validierungsfehler_deutsch() -> None:
    """422-Validierung liefert deutsche Feldmeldungen (F-CON-02)."""
    with TestClient(_probe_app()) as client:
        fehlend = client.get("/api/_probe/validation")
        ungueltig = client.get("/api/_probe/validation", params={"limit": "abc"})

    assert fehlend.status_code == 422
    body = fehlend.json()
    assert body["code"] == "validation_error"
    fehler = body["details"]["errors"]
    assert fehler and fehler[0]["field"] == "limit"
    assert fehler[0]["message"] == "Feld ist erforderlich"

    assert ungueltig.status_code == 422
    assert ungueltig.json()["details"]["errors"][0]["message"] == "Erwartet wird eine ganze Zahl"


def test_konflikt_liefert_aktuellen_stand() -> None:
    """409 traegt zusaetzlich ``current`` mit dem aktuellen Stand (F-CON-02)."""
    with TestClient(_probe_app()) as client:
        response = client.get("/api/_probe/conflict")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"] == {"id": 1, "name": "Giro", "version": 7}
    assert "zwischenzeitlich" in body["message"]


def test_fachfehler_im_format() -> None:
    """Fachfehler nutzen dasselbe Format mit eigenem Code (F-CON-02)."""
    with TestClient(_probe_app()) as client:
        response = client.get("/api/_probe/domain")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "domain_error"
    assert body["message"] == "Die Aktion ist im aktuellen Zustand nicht moeglich."


def test_unerwarteter_fehler_ohne_details() -> None:
    """500 liefert generischen Klartext ohne interne Details oder Secrets (N-SEC-02)."""
    with TestClient(_probe_app(), raise_server_exceptions=False) as client:
        response = client.get("/api/_probe/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["details"] == {}
    assert "pin" not in response.text.lower()
    assert "Traceback" not in response.text


def test_health_bleibt_ohne_anmeldung_erreichbar(client: TestClient) -> None:
    """Health-Routen liegen ausserhalb der Auth-Naht (N-OPS-04)."""
    assert client.get("/api/health").status_code == 200


def test_require_user_ohne_pruefung_im_m4() -> None:
    """Auth-Naht liefert in M4 den Owner ohne Nachweis (N-SEC-04, noch nicht geprueft)."""
    with TestClient(_probe_app()) as client:
        response = client.get("/api/_probe/whoami")

    assert response.status_code == 200
    assert response.json() == {"name": "owner", "authenticated": False}


def test_require_user_erzwingt_401_wenn_aktiv() -> None:
    """Mit aktivem auth_required liefert die Naht 401 im Format (N-SEC-04)."""
    with TestClient(_probe_app(auth_required=True)) as client:
        response = client.get("/api/_probe/whoami")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_service_container_im_app_state() -> None:
    """Der Container traegt Bus, Registry und Uhr (D12, F-CON-04)."""
    with TestClient(_probe_app()) as client:
        response = client.get("/api/_probe/container")

    assert response.status_code == 200
    assert response.json() == {"has_bus": True, "has_registry": True, "clock_aware": True}


def test_app_state_container_felder(client: TestClient) -> None:
    """app.state stellt Bus, Registry, Uhr und Session-Factory bereit (D12)."""
    services = client.app.state.services
    assert isinstance(services.event_bus, EventBus)
    assert isinstance(services.sync_registry, SyncRegistry)
    assert callable(services.clock)
    assert client.app.state.event_bus is services.event_bus
    assert client.app.state.sync_registry is services.sync_registry


def test_sqlite_laeuft_im_wal_modus(client: TestClient) -> None:
    """Die temporaere Datei-SQLite nutzt WAL (N-DB-04, D12)."""
    engine = client.app.state.engine
    with engine.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"


def test_paginierung_standard_und_grenzen() -> None:
    """limit: Standard 50, hoechstens 200; Verletzungen sind deutsch (SPEC F-MCP-11)."""
    settings = _settings()

    assert resolve_page_params(_request(), settings).limit == 50
    assert resolve_page_params(_request("limit=10"), settings).limit == 10
    assert resolve_page_params(_request("limit=200"), settings).limit == 200

    for bad in ("limit=0", "limit=201", "limit=abc"):
        with pytest.raises(ValidationApiError) as excinfo:
            resolve_page_params(_request(bad), settings)
        assert excinfo.value.status_code == 422


def test_cursor_rundreise_und_pruefung() -> None:
    """Cursor kodieren/dekodieren und ungueltige Werte werden abgelehnt (F-CON-02)."""
    for offset in (0, 50, 12345):
        assert decode_cursor(encode_cursor(offset)) == offset

    for bad in ("", "!!!", encode_cursor(1)[:-1] + "x"):
        with pytest.raises(ValidationApiError):
            decode_cursor(bad)

    assert resolve_page_params(_request(f"cursor={encode_cursor(50)}"), _settings()).cursor is not None


def test_seitenumschlag() -> None:
    """Der Umschlag ist {items, next_cursor} (SPEC F-MCP-11)."""
    page = Page[int](items=[1, 2, 3], next_cursor="abc")
    assert page.model_dump() == {"items": [1, 2, 3], "next_cursor": "abc"}
    assert Page[int](items=[]).next_cursor is None


def test_batch_umschlag_mit_ergebnissen() -> None:
    """Batch-Ergebnisse nutzen die deutschen Ausgaenge geaendert|unveraendert|konflikt|fehler (F-CON-03)."""
    response = BatchResponse(
        results=[
            BatchItemResult(id=1, outcome=BatchOutcome.GEAENDERT),
            BatchItemResult(id=2, outcome=BatchOutcome.UNVERAENDERT),
            BatchItemResult(id=3, outcome=BatchOutcome.KONFLIKT, current={"id": 3, "version": 9}, message="Konflikt"),
            BatchItemResult(id=4, outcome=BatchOutcome.FEHLER, message="Unbekanntes Konto"),
        ]
    )
    data = response.model_dump()
    assert [item["outcome"] for item in data["results"]] == [
        "geaendert",
        "unveraendert",
        "konflikt",
        "fehler",
    ]
    assert data["results"][2]["current"] == {"id": 3, "version": 9}


def test_batch_groesse_und_upload_grenze() -> None:
    """Zentrale Grenzen: Batch 1000 Eintraege, Upload-Groesse aus config (F-CON-03)."""
    settings = _settings()
    check_batch_size(1000, settings)
    with pytest.raises(ValidationApiError):
        check_batch_size(1001, settings)

    check_upload_size(settings.api_max_upload_bytes, settings)
    with pytest.raises(PayloadTooLargeError) as excinfo:
        check_upload_size(settings.api_max_upload_bytes + 1, settings)
    assert excinfo.value.status_code == 413


def test_versionsfeld_im_body() -> None:
    """VersionedRequest verlangt eine Version >= 1 (F-CON-02)."""
    assert VersionedRequest(version=3).version == 3
    with pytest.raises(ValidationError):
        VersionedRequest(version=0)


def test_rest_herkunft_ist_ui() -> None:
    """REST-Aenderungen tragen die Herkunft ``ui`` (F-ABS-01/02)."""
    assert REST_ORIGIN == "ui"


def test_error_body_und_lokalisierung() -> None:
    """error_body und die Uebersetzung liefern deutsches Klartext-Format (F-CON-02)."""
    assert error_body("not_found", "weg") == {"code": "not_found", "message": "weg", "details": {}}
    localized = localize_validation_errors(
        [
            {"type": "missing", "loc": ("body", "name")},
            {"type": "value_error", "loc": ("body", "konto"), "ctx": {"error": "Konto unbekannt"}},
        ]
    )
    assert localized[0] == {"field": "name", "message": "Feld ist erforderlich"}
    assert localized[1] == {"field": "konto", "message": "Konto unbekannt"}


def test_health_ready_bleibt_gruen(client: TestClient) -> None:
    """Der Readiness-Endpunkt funktioniert mit dem Container weiter (N-OPS-04)."""
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] == "ok"


def test_eigene_uhr_wird_durchgereicht() -> None:
    """create_app nimmt eine injizierbare Uhr entgegen (D12)."""
    from datetime import datetime, timezone

    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    app = create_app(_settings(), clock=lambda: fixed)
    with TestClient(app) as client:
        assert client.app.state.clock() == fixed
        assert client.app.state.sync_registry is not None
