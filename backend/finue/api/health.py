"""Health-Endpunkte der Finanzuebersicht.

Implementiert N-OPS-04: ``GET /api/health`` meldet die Betriebsbereitschaft des
Prozesses als reiner Liveness-Check. Der Endpunkt greift bewusst nicht auf die
Datenbank zu (keine ``get_session``-Dependency aus ``finue.api.deps``), damit er
auch bei nicht erreichbarer Datenbank eine Antwort liefert. Die Version stammt
aus ``finue.__version__`` und wird nicht im Endpunkt dupliziert.

``GET /api/health/ready`` ergaenzt den Readiness-Check (N-OPS-04, N-DB-03): er
prueft die Erreichbarkeit der Datenbank, den Schemastand und den Zeitplaner.
Der Zeitplaner ist bis M8 nur gemeldet, nicht erforderlich (``SCHEDULER_REQUIRED``
aus ``finue.config``). Fehlermeldungen bleiben generisch und enthalten keine
Verbindungsdaten oder Exception-Details (N-SEC-02).
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from finue import __version__
from finue.config import get_settings
from finue.db.migrate import is_schema_current

router = APIRouter(prefix="/api", tags=["health"])

# Statuswerte je Einzelpruefung. Bewusst als Konstanten, damit die Endpunkt-Logik
# und die Tests dieselben Zeichenketten verwenden.
STATUS_OK: Final = "ok"
STATUS_ERROR: Final = "error"
STATUS_NOT_REQUIRED: Final = "not_required"
STATUS_RUNNING: Final = "running"
STATUS_NOT_CHECKED: Final = "not_checked"


@router.get("/health")
def get_health() -> dict[str, str]:
    """Meldet Status und Version der Anwendung (N-OPS-04, ohne DB-Zugriff).

    Returns:
        JSON-Objekt mit ``status`` (immer ``"ok"``) und ``version``.
    """
    return {"status": "ok", "version": __version__}


def _database_reachable(engine: Any) -> bool:
    """Prueft die Datenbank per ``select(1)`` ueber SQLAlchemy (N-DB-03).

    Verwendet das SQLAlchemy-Core-Konstrukt ``select(1)`` statt eines rohen
    SQL-Strings (N-DB-01). Datenbankfehler werden abgefangen und als ``False``
    gemeldet; Details gelangen nicht nach aussen (N-SEC-02).

    Args:
        engine: SQLAlchemy-Engine aus ``app.state``.

    Returns:
        ``True``, wenn eine Verbindung aufgebaut und die Abfrage ausgefuehrt
        werden konnte, sonst ``False``.
    """
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
    except SQLAlchemyError:
        return False
    return True


@router.get("/health/ready", response_model=None)
def get_health_ready(request: Request) -> JSONResponse:
    """Meldet die Betriebsbereitschaft mit Detailstatus je Pruefung.

    Prueft die Datenbank (``select(1)``), den Schemastand
    (``is_schema_current``) und den Zeitplaner (``app.state.scheduler_running``).
    Der Zeitplaner ist nur erforderlich, wenn ``SCHEDULER_REQUIRED`` gesetzt ist;
    bis M8 ist das nicht der Fall.

    Args:
        request: Aktueller Request; liefert Engine und Zeitplaner-Status ueber
            ``app.state``.

    Returns:
        ``200`` mit dem Status jeder Pruefung, wenn alle erforderlichen
        Pruefungen bestehen; sonst ``503`` mit einer generischen
        Klartext-Meldung ohne interne Details (N-SEC-02).
    """
    engine: Engine | None = getattr(request.app.state, "engine", None)

    database_state = STATUS_OK if engine is not None and _database_reachable(engine) else STATUS_ERROR

    if database_state == STATUS_OK and engine is not None:
        try:
            schema_state = STATUS_OK if is_schema_current(engine) else STATUS_ERROR
        except SQLAlchemyError:
            schema_state = STATUS_ERROR
    else:
        # Ohne Datenbankverbindung laesst sich der Schemastand nicht pruefen.
        schema_state = STATUS_NOT_CHECKED

    settings = get_settings()
    scheduler_running = bool(getattr(request.app.state, "scheduler_running", False))
    if settings.scheduler_required:
        scheduler_state = STATUS_OK if scheduler_running else STATUS_ERROR
    else:
        scheduler_state = STATUS_RUNNING if scheduler_running else STATUS_NOT_REQUIRED

    checks = {
        "database": database_state,
        "schema": schema_state,
        "scheduler": scheduler_state,
    }

    reasons: list[str] = []
    if database_state == STATUS_ERROR:
        reasons.append("Datenbank nicht erreichbar")
    if schema_state == STATUS_ERROR:
        reasons.append("Datenbankschema nicht aktuell")
    if scheduler_state == STATUS_ERROR:
        reasons.append("Zeitplaner nicht gestartet")

    if reasons:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": "; ".join(reasons),
                "checks": checks,
            },
        )
    return JSONResponse(status_code=200, content={"status": "ok", "checks": checks})
