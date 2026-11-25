"""Wiederverwendbare Hilfsfunktionen fuer Migrationstests mit Daten (N-DB-03).

Diese Helfer kapseln das Muster "Datenbank auf eine Zwischenrevision hochziehen,
Beispieldaten einfuegen, auf head anheben, Daten pruefen". Sie sind bewusst
revisionsunabhaengig: ``from_revision`` ist ein Parameter, sodass dasselbe Muster
auch fuer kuenftige Zwischenrevisionen (0002, 0003, ...) gilt und nicht auf die
Anfangsrevision hartkodiert ist.

N-DB-01: Alle Datenzugriffe laufen ueber SQLAlchemy Core mit aus der Datenbank
reflektierten Tabellen (``Table(..., autoload_with=engine)``). Es gibt kein
rohes SQL und keine Abhaengigkeit auf die aktuellen ORM-Modelle - die Struktur
kommt aus dem tatsaechlichen Schema der jeweiligen Revision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, insert, select
from sqlalchemy.engine import Engine

from finue.db.migrate import upgrade_to_head

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ALEMBIC_INI: Final[Path] = PROJECT_ROOT / "alembic.ini"


def sqlite_url(db_path: Path) -> str:
    """Baut die SQLAlchemy-URL fuer eine Datei-basierte SQLite-Datenbank."""
    return f"sqlite:///{db_path.as_posix()}"


def build_alembic_config(db_path: Path) -> Config:
    """Laedt ``alembic.ini`` und richtet die DB-URL auf die temporaere Datei.

    Die URL wird als Alembic-Config-Option gesetzt; ``env.py`` liest sie mit
    Vorrang vor den Projekt-Settings, sodass der Lauf nicht die echte
    Standard-Datenbank beruehrt.
    """
    assert ALEMBIC_INI.is_file(), f"alembic.ini fehlt: {ALEMBIC_INI}"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", sqlite_url(db_path))
    return config


def upgrade_to_revision(db_path: Path, revision: str) -> None:
    """Hebt die Datenbank auf eine bestimmte Revision an (nicht nur ``head``).

    ``revision`` ist eine Revision-ID wie ``"0001"``. Fuer den Sonderfall
    ``"head"`` kann stattdessen ``finue.db.migrate.upgrade_to_head`` genutzt
    werden; hier wird die Revision bewusst explizit gesetzt, damit ein
    Zwischenstand ("Vorgaenger-Stand") auch nach kuenftigen Migrationen
    reproduzierbar bleibt.
    """
    command.upgrade(build_alembic_config(db_path), revision)


def reflect_table(engine: Engine, table_name: str) -> Table:
    """Reflektiert eine Tabelle direkt aus dem Schema der Datenbank.

    Kein Modell-Import: Die Spaltenstruktur kommt aus der migrierten DB und ist
    damit unabhaengig vom aktuellen ORM-Stand (N-DB-01).
    """
    metadata = MetaData()
    return Table(table_name, metadata, autoload_with=engine)


def insert_rows(db_path: Path, table_name: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Fuegt Zeilen ueber die reflektierte Tabelle ein (INSERT, kein rohes SQL).

    Die ``rows`` sind Dictionaries mit Spaltennamen als Schluessel. Fehlende
    Spalten bleiben auf ihrem DB-Vorgabewert (z. B. automatischer Integer-PK).
    """
    engine = create_engine(sqlite_url(db_path))
    try:
        table = reflect_table(engine, table_name)
        with engine.begin() as connection:
            connection.execute(insert(table), list(rows))
    finally:
        engine.dispose()


def fetch_rows(db_path: Path, table_name: str) -> list[dict[str, Any]]:
    """Liest alle Zeilen ueber die reflektierte Tabelle (SELECT, sortiert nach PK).

    Die Sortierung nach Primaerschluesselspalten macht das Ergebnis
    deterministisch, damit ``before`` und ``after`` direkt vergleichbar sind.
    """
    engine = create_engine(sqlite_url(db_path))
    try:
        table = reflect_table(engine, table_name)
        statement = select(table)
        pk_columns = list(table.primary_key.columns)
        if pk_columns:
            statement = statement.order_by(*pk_columns)
        with engine.connect() as connection:
            result = connection.execute(statement)
            return [dict(row._mapping) for row in result]
    finally:
        engine.dispose()


def upgrade_preserves_data(
    db_path: Path,
    *,
    from_revision: str,
    table_name: str,
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Prueft, dass Daten ein Upgrade von ``from_revision`` auf ``head`` ueberstehen.

    Ablauf: DB auf ``from_revision`` hochziehen, ``rows`` ueber die reflektierte
    Tabelle einfuegen, Daten einlesen, auf ``head`` anheben und erneut einlesen.
    Weichen die Daten ab oder fehlen Zeilen, schlaegt der Aufruf fehl.

    Gibt die nach dem Upgrade gelesenen Zeilen zurueck, damit der Test sie gegen
    die erwarteten Werte pruefen kann. Der Ablauf ist generisch: Fuer eine neue
    Zwischenrevision genuegt ein anderer ``from_revision``-Wert.
    """
    upgrade_to_revision(db_path, from_revision)
    insert_rows(db_path, table_name, rows)

    before = fetch_rows(db_path, table_name)
    assert len(before) == len(rows), (
        f"Vor dem Upgrade fehlen Zeilen in {table_name}: erwartet={len(rows)}, gefunden={len(before)}"
    )

    upgrade_to_head(sqlite_url(db_path))

    after = fetch_rows(db_path, table_name)
    assert after == before, (
        f"Daten in {table_name} haben sich beim Upgrade auf head veraendert: vorher={before}, nachher={after}"
    )
    return after
