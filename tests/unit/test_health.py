"""Test des Health-Endpunkts (T-034).

Referenziert die Anforderung N-OPS-04: ``GET /api/health`` liefert 200 mit
``status`` und ``version``, ohne auf die Datenbank zuzugreifen. Der TestClient
wird bewusst ohne Kontextmanager verwendet, damit der Lifespan (inkl. Migration)
nicht laeuft: es gibt dann keine Engine, keine Session-Factory und keine
Datenbank. Der Endpunkt muss dennoch antworten.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from finue import __version__
from finue.main import create_app


def test_health_endpoint_without_database() -> None:
    """GET /api/health liefert 200 mit status ok und version (N-OPS-04)."""
    app = create_app()

    # Ohne ``with`` wird der Lifespan nicht ausgefuehrt, also keine DB angelegt.
    client = TestClient(app)
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
