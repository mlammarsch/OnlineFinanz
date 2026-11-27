"""Test fuer Migration 0005 (T-103).

Referenziert N-DB-01 (Migrationen ueber SQLAlchemy/Alembic, kein rohes SQL),
N-DB-03 (Migrationen nur vorwaerts) sowie N-PERF-01 und F-BUC-06 (abdeckende
Indizes fuer Suche und Buchungsliste).

Der Test faehrt echte ``upgrade``/``downgrade``-Laeufe gegen eine Datei-basierte
SQLite-Datenbank in ``tmp_path``:

* nach ``upgrade head`` existieren die beiden abdeckenden Performance-Indizes mit
  den erwarteten Spalten,
* das Schema weicht nach ``upgrade head`` nicht von den Modellen ab,
* ``downgrade`` bricht ohne Aenderung ab (N-DB-03).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Inspector

import finue.models  # noqa: F401  registriert alle Tabellen in Base.metadata
from finue.db.base import Base
from finue.db.engine import create_db_engine
from finue.db.migrate import upgrade_to_head
from helpers_migration import build_alembic_config, sqlite_url

#: Erwartete Indizes mit Spaltenreihenfolge (T-103, Migration 0005).
EXPECTED_INDEXES: Final[dict[str, list[str]]] = {
    "ix_transaction_account_booking_search": [
        "account_id",
        "booking_date",
        "merged",
        "search_text",
        "amount_cents",
    ],
    "ix_transaction_account_status_booking_cover": [
        "account_id",
        "status",
        "merged",
        "booking_date",
        "id",
        "amount_cents",
    ],
}


def _index_columns(inspector: Inspector, table: str, name: str) -> list[str] | None:
    """Liefert die Spalten eines benannten Index oder ``None``."""

    for index in inspector.get_indexes(table):
        if index["name"] == name:
            columns: Any = index["column_names"]
            return list(columns)
    return None


def test_migration_0005_creates_performance_indexes(tmp_path: Path) -> None:
    """Nach upgrade head existieren die abdeckenden Indizes (N-PERF-01, F-BUC-06)."""
    db_path = tmp_path / "migration_0005_indexes.db"
    url = sqlite_url(db_path)
    upgrade_to_head(url)

    engine = create_db_engine(url)
    try:
        inspector = inspect(engine)
        for name, columns in EXPECTED_INDEXES.items():
            found = _index_columns(inspector, "transaction", name)
            assert found is not None, f"Index {name} fehlt nach upgrade head"
            assert found == columns, f"Spalten von {name} weichen ab: {found}"
    finally:
        engine.dispose()


def test_migration_0005_schema_matches_models(tmp_path: Path) -> None:
    """upgrade head laesst das Schema ohne Drift (N-DB-01, N-DB-03)."""
    db_path = tmp_path / "migration_0005_drift.db"
    url = sqlite_url(db_path)
    upgrade_to_head(url)

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
        assert diff == [], f"Drift zwischen Modellen und migriertem Schema: {diff}"
    finally:
        engine.dispose()


def test_migration_0005_downgrade_is_not_supported(tmp_path: Path) -> None:
    """downgrade() bricht ohne Aenderung ab (N-DB-03: nur vorwaerts)."""
    db_path = tmp_path / "migration_0005_downgrade.db"
    upgrade_to_head(sqlite_url(db_path))

    with pytest.raises(NotImplementedError):
        command.downgrade(build_alembic_config(db_path), "base")
