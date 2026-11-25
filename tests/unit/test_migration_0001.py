"""Test fuer die erste Migration 0001 (T-029).

Referenziert N-DB-01 (Migrationen mit Alembic, kein rohes SQL) und N-DB-03
(Schema-Versionierung; jede Migration wird von einer leeren Datenbank getestet,
Migrationen laufen nur vorwaerts).

Der Test faehrt ``alembic upgrade head`` ueber die Alembic-Programm-API gegen
eine leere Datei-basierte SQLite-Datenbank in einem temporaeren Verzeichnis
(``tmp_path``, wird von pytest aufgeraeumt). Danach vergleicht er das Ergebnis
mit ``Base.metadata`` aus ``finue.models``: Tabellennamen, Spalten je Tabelle
und Indizes muessen uebereinstimmen. Ein zweiter Test belegt, dass
``downgrade()`` ohne Logik abbricht (N-DB-03).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import finue.models  # noqa: F401  (registriert alle Tabellen im Metadata)
from finue.db.base import Base

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ALEMBIC_INI: Final[Path] = PROJECT_ROOT / "alembic.ini"

# Beispiel fuer den Umfang, den die Migration abdecken muss (N-DB-01/N-DB-03).
EXPECTED_TABLE_COUNT: Final[int] = 15


def _build_alembic_config(db_path: Path) -> Config:
    """Laedt alembic.ini und richtet die DB-URL auf die temporaere Datei.

    Die URL wird als Alembic-Config-Option gesetzt; ``env.py`` liest sie mit
    Vorrang vor den Projekt-Settings, sodass kein Zugriff auf die echte
    Standard-Datenbank entsteht.
    """
    assert ALEMBIC_INI.is_file(), f"alembic.ini fehlt: {ALEMBIC_INI}"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def test_migration_0001_creates_all_tables(tmp_path: Path) -> None:
    """upgrade head auf leerer Datei-DB ergibt genau die Tabellen aus dem Metadata.

    N-DB-01: Schema entsteht ausschliesslich ueber Alembic-Operationen.
    N-DB-03: Die Migration wird von einer leeren Datenbank aus getestet.
    """
    db_path = tmp_path / "migration_0001.db"
    config = _build_alembic_config(db_path)

    command.upgrade(config, "head")

    assert db_path.is_file(), "Die Migration hat keine SQLite-Datei angelegt"

    metadata_tables = set(Base.metadata.tables)
    assert len(metadata_tables) == EXPECTED_TABLE_COUNT

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        inspector = inspect(engine)
        db_tables = set(inspector.get_table_names()) - {"alembic_version"}

        assert db_tables == metadata_tables, (
            f"Tabellen weichen ab: fehlt={metadata_tables - db_tables}, "
            f"zusaetzlich={db_tables - metadata_tables}"
        )

        for name, table in Base.metadata.tables.items():
            db_columns = {column["name"] for column in inspector.get_columns(name)}
            assert db_columns == set(table.columns.keys()), (
                f"Spalten weichen ab in {name}: fehlt={set(table.columns.keys()) - db_columns}, "
                f"zusaetzlich={db_columns - set(table.columns.keys())}"
            )

        db_indexes = {
            index["name"]
            for name in db_tables
            for index in inspector.get_indexes(name)
            if index["name"] and not index["name"].startswith("sqlite_autoindex")
        }
        metadata_indexes = {
            index.name
            for table in Base.metadata.tables.values()
            for index in table.indexes
        }
        assert db_indexes == metadata_indexes, (
            f"Indizes weichen ab: fehlt={metadata_indexes - db_indexes}, "
            f"zusaetzlich={db_indexes - metadata_indexes}"
        )
    finally:
        engine.dispose()


def test_migration_0001_downgrade_is_not_supported(tmp_path: Path) -> None:
    """downgrade() bricht ab, statt Tabellen zu loeschen (N-DB-03: nur vorwaerts)."""
    db_path = tmp_path / "downgrade_0001.db"
    config = _build_alembic_config(db_path)

    command.upgrade(config, "head")

    with pytest.raises(NotImplementedError):
        command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert remaining == set(Base.metadata.tables), (
        "Ein abgebrochener downgrade darf das Schema nicht veraendern"
    )
