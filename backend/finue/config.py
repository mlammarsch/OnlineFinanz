"""Konfigurationsverwaltung für Finanzübersicht Backend.

Implementiert N-DB-01 (kein rohes SQL), N-OPS-05 (Konfigurationsstandards) und N-OPS-06
(sichere Pfadverwaltung). Settings aus Umgebungsvariablen, Defaults basierend auf DATA_DIR.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Anwendungskonfiguration aus Umgebungsvariablen und Defaults.

    Implementiert N-DB-01, N-OPS-05, N-OPS-06. Alle Pfade als Path-Objekte,
    keine Secrets im Code. DATABASE_URL wird von DATA_DIR abgeleitet, falls nicht
    explizit gesetzt.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    data_dir: Path = Field(default=Path("./data"), description="Datenverzeichnis")
    database_url: str = Field(default="", description="Datenbank-URL oder leer für SQLite-Default")
    log_level: str = Field(default="INFO", description="Logging-Level")
    tz: str = Field(default="Europe/Berlin", description="Zeitzone")
    key_file: Path = Field(default=Path("./key"), description="Pfad zur Schlüsseldatei (nur Pfad, keine Secrets)")
    fints_product_id: str | None = Field(
        default=None,
        description=(
            "FinTS-Produkt-ID aus FINTS_PRODUCT_ID. Kein Standardwert: ohne gesetzte "
            "Produkt-ID ist kein Bankzugriff erlaubt."
        ),
    )
    scheduler_required: bool = Field(
        default=False,
        description="Zeitplaner muss laufen, damit /api/health/ready bereit meldet (ab M8)",
    )
    auth_required: bool = Field(
        default=False,
        description=(
            "Anmeldung erzwingen. In M4 ist die Auth-Naht require_user ohne Pruefung aktiv; "
            "die tatsaechliche Anmeldung folgt mit N-SEC-04 in M10."
        ),
    )
    api_default_page_size: int = Field(
        default=50,
        ge=1,
        description="Standard-Seitengroesse bei Listen (SPEC F-MCP-11)",
    )
    api_max_page_size: int = Field(
        default=200,
        ge=1,
        description="Hoechste Seitengroesse bei Listen (SPEC F-MCP-11)",
    )
    api_max_batch_size: int = Field(
        default=1000,
        ge=1,
        description="Hoechste Zahl von Eintraegen je Batch-Aktion (F-CON-03)",
    )
    api_max_upload_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1,
        description="Hoechste Groesse einer hochgeladenen Datei in Bytes (Import, M4)",
    )
    api_import_upload_ttl_seconds: int = Field(
        default=300,
        ge=1,
        description=(
            "Lebensdauer eines zwischengespeicherten Import-Uploads in Sekunden (F-IMP-01, M4). "
            "Der Upload liegt nur im Arbeitsspeicher."
        ),
    )
    export_max_rows: int = Field(
        default=100_000,
        ge=1,
        description=(
            "Hoechstzahl der Buchungen je Export (F-EXP-01/02). Begrenzt zugleich die "
            "Laenge einer Auswahl-ID-Liste; groessere Exporte werden mit Klartext abgelehnt."
        ),
    )
    api_sse_heartbeat_seconds: float = Field(
        default=15.0,
        gt=0,
        description=("Abstand des Heartbeat-Kommentars im SSE-Strom /api/events in Sekunden (F-CON-04, T-101)."),
    )
    api_sse_queue_size: int = Field(
        default=256,
        ge=1,
        description=(
            "Hoechste Zahl gepufferter Ereignisse je SSE-Client; ein Ueberlauf loest "
            "ein resync aus statt Speicherwachstum (F-CON-04, T-101)."
        ),
    )

    def __init__(self, **data: Any) -> None:
        """Initialisierung mit Standard-DATABASE_URL-Berechnung."""
        super().__init__(**data)
        # DATABASE_URL hat Standard: sqlite:///<DATA_DIR>/finue.db
        if not self.database_url:
            # Normalisiere Pfad zu posix-Pfad (forward slashes) für SQLite URL
            db_path = self.data_dir.as_posix()
            self.database_url = f"sqlite:///{db_path}/finue.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Gibt gecachte Settings-Instanz zurück.

    Cache wird verwendet, um mehrfaches Laden aus Umgebungsvariablen zu vermeiden.
    Für Tests: Cache über get_settings.cache_clear() leeren.
    """
    return Settings()
