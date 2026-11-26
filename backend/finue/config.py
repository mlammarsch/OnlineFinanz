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
