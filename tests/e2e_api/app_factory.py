"""App-Fabrik fuer den API-E2E-Lauf gegen einen echten Uvicorn-Prozess (T-113).

Der Test startet den Server als eigenen Prozess mit genau einem Worker (D3)
ueber ``python -m uvicorn --factory tests.e2e_api.app_factory:create_app``. Die
Fabrik ersetzt ausschliesslich den FinTS-Client durch den skriptgesteuerten
``FakeFinTS3PinTanClient`` (D8, kein Netz, keine echten Zugangsdaten); alle
uebrigen Dienste sind die der Produktion. So laeuft das Szenario gegen den
echten ASGI-Stack (Routing, Middleware, SSE) statt gegen einen ``TestClient``.

Konfiguration ueber Umgebungsvariablen (vom E2E-``conftest`` gesetzt):

- ``FINUE_E2E_DATA_DIR``: Pflicht; temporaeres Datenverzeichnis (SQLite-Datei,
  Schluesseldatei, Logs). Fehlt der Wert, bricht der Start mit klarer Meldung ab.
- ``FINUE_E2E_SCRIPT``: Name des Fake-FinTS-Skripts (Standard ``happy_path``),
  z. B. ``sync_tan`` fuer den Sync-TAN-Fluss in Szenario Teil 2 (T-115).

Referenzierte Anforderungen: D3 (ein Worker), D8 (Fake-Client), N-SEC-02 (keine
Geheimnisse im Prozess).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from finue.config import Settings
from finue.main import create_app as _create_app

__all__ = ["create_app"]

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"

#: Platzhalter-Produkt-ID (nur Signatur; der Fake liest sie nicht). Der Wert ist
#: kein Geheimnis und wird nur gesetzt, damit keine fehlende Konfiguration den
#: reinen API-Lauf stoert (Szenario Teil 1 synct nicht).
_PRODUCT_ID_PLACEHOLDER = "0" * 27


def _load_fake_module() -> ModuleType:
    """Laedt den Fake-FinTS-Baustein aus der Fixture-Datei (wie die Tests)."""

    spec = importlib.util.spec_from_file_location("e2e_fints_fake", _FIXTURES)
    if spec is None or spec.loader is None:  # pragma: no cover - Ladefehler der Umgebung
        raise RuntimeError(f"Fake-FinTS-Modul nicht ladbar: {_FIXTURES}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings() -> Settings:
    """Baut die E2E-Settings aus dem temporaeren Datenverzeichnis."""

    data_dir = os.environ.get("FINUE_E2E_DATA_DIR")
    if not data_dir:
        raise RuntimeError("FINUE_E2E_DATA_DIR ist nicht gesetzt.")
    return Settings(
        _env_file=None,
        data_dir=Path(data_dir),
        fints_product_id=os.environ.get("FINUE_E2E_PRODUCT_ID", _PRODUCT_ID_PLACEHOLDER),
        # Kurzer Heartbeat/Abrufabstand: die SSE-Zustellung wird dadurch auch
        # fuer den Test zeitnah, ohne die Produktions-Semantik zu aendern.
        api_sse_heartbeat_seconds=float(os.environ.get("FINUE_E2E_SSE_HEARTBEAT", "0.5")),
    )


def create_app() -> Any:
    """Uvicorn-Fabrik: echte App, nur der FinTS-Client ist der Fake (D3, D8)."""

    fake = _load_fake_module()
    script_name = os.environ.get("FINUE_E2E_SCRIPT", "happy_path")

    # Wie in den FinTS-Tests (SharedClientFactory) wird je Serverprozess eine
    # Fake-Client-Instanz wiederverwendet. Der Fake haelt den TAN-Dialog
    # (``_pending_tan``/``_paused``) in der Instanz; die Bibliothek baut bei
    # ``from_data`` zwar einen neuen Client, der Fake kann den Dialog aber nur
    # auf derselben Instanz fortsetzen. Die E2E-Szenarien nutzen genau einen
    # Bankzugang, daher ist eine Instanz je Prozess ausreichend (D8).
    holder: dict[str, Any] = {}

    def client_factory(**kwargs: Any) -> Any:
        client = holder.get("client")
        if client is not None:
            return client
        # Der erste Aufbau kommt ohne ``from_data``; ein etwaiges ``from_data``
        # waere nur die Fortsetzung und trifft immer die gecachte Instanz.
        kwargs.pop("from_data", None)
        client = fake.FakeFinTS3PinTanClient(script=fake.SCRIPT_FACTORIES[script_name](), **kwargs)
        holder["client"] = client
        return client

    return _create_app(_settings(), sync_client_factory=client_factory)
