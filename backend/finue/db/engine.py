"""Datenbank-Engine und Session-Verwaltung.

Implementiert N-DB-01 (kein rohes SQL für Daten) und N-DB-04 (SQLite WAL mit busy_timeout).
PRAGMA-Aufrufe für Initialisierung sind keine Daten-SQL, sondern Konfiguration.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(url: str) -> Engine:
    """Erstellt und konfiguriert eine SQLAlchemy Engine.

    Für SQLite-Datenbanken (erkannt am URL-Schema 'sqlite'):
    - Setzt PRAGMA journal_mode=WAL für bessere Concurrency (N-DB-04, D-12)
    - Setzt PRAGMA busy_timeout=5000 ms (N-DB-04, D-12)
    - Setzt PRAGMA foreign_keys=ON für referenzielle Integrität
    - Erstellt Elternverzeichnis, falls eine Datei-URL verwendet wird

    Für :memory: SQLite-Datenbanken wird WAL übersprungen (nicht unterstützt).

    Für andere Datenbanken (PostgreSQL, MySQL) werden keine PRAGMA-Befehle ausgeführt.

    Args:
        url: SQLAlchemy Database URL (z.B. 'sqlite:///./data/finue.db', 'postgresql://...')

    Returns:
        Konfigurierte SQLAlchemy Engine
    """
    is_sqlite = url.startswith("sqlite")
    is_memory = url == "sqlite:///:memory:" or url == "sqlite://"

    # Für Datei-basierte SQLite-DBs: Elternverzeichnis erstellen
    if is_sqlite and not is_memory:
        # Extrahiere Datei-Pfad aus URL (sqlite:///path/to/db.db -> /path/to/db.db)
        db_path_str = url.replace("sqlite:///", "")
        db_path = Path(db_path_str)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # Engine erstellen
    engine = create_engine(url, echo=False)

    # PRAGMA nur für SQLite setzen
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
            """Setzt SQLite-spezifische PRAGMAs für Concurrency und Integrität."""
            cursor = dbapi_connection.cursor()

            # Nur WAL für Datei-basierte DBs (nicht für :memory:)
            if not is_memory:
                cursor.execute("PRAGMA journal_mode=WAL")

            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Erstellt eine Session-Factory für Datenbankzugriffe.

    Die Factory ist konfiguriert mit expire_on_commit=False, um nach einem
    Commit Objekte nicht aus dem Session-Cache zu entfernen (N-DB-01).

    Args:
        engine: Konfigurierte SQLAlchemy Engine

    Returns:
        sessionmaker[Session], kann aufgerufen werden um neue Sessions zu erhalten
    """
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
