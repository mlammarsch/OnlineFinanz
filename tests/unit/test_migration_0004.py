"""Test fuer Migration 0004 (T-087).

Referenziert N-DB-01 (Migrationen ueber SQLAlchemy/Alembic, kein rohes SQL),
N-DB-03 (Migrationen nur vorwaerts, Bestandsdaten bleiben erhalten) und
F-SUC-01/F-SUC-06 (normalisierte Suchspalte).

Der Test faehrt echte ``upgrade``-Laeufe gegen eine Datei-basierte
SQLite-Datenbank in ``tmp_path``:

* Bestandsbuchungen aus Revision 0003 erhalten die aus ihren Feldern gebaute
  Suchspalte (Backfill),
* die Spalte stimmt mit :func:`finue.services.search_text.build_search_text`
  ueberein (Paritaet der eingefrorenen Migrationsnormalisierung mit der
  Laufzeitregel),
* mehr als ein Batch wird verarbeitet,
* das Schema weicht nach ``upgrade head`` nicht von den Modellen ab,
* ``downgrade`` bricht ohne Schema- oder Datenaenderung ab (N-DB-03).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

import finue.models  # noqa: F401  registriert alle Tabellen in Base.metadata
from finue.db.base import Base
from finue.db.engine import create_db_engine
from finue.db.migrate import upgrade_to_head
from finue.services.search_text import build_search_text
from helpers_migration import (
    build_alembic_config,
    fetch_rows,
    insert_rows,
    sqlite_url,
    upgrade_to_revision,
)

PREVIOUS_REVISION: Final[str] = "0003"
TEST_TIMESTAMP: Final[datetime] = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TEST_BOOKING_DATE: Final[date] = date(2026, 1, 3)

ACCOUNT_ROW: Final[dict[str, Any]] = {
    "name": "Testkonto",
    "currency": "EUR",
    "source": "fints",
    "include_in_total": True,
    "opening_balance_cents": 0,
    "active": True,
    "version": 1,
    "created_at": TEST_TIMESTAMP,
    "updated_at": TEST_TIMESTAMP,
}


def transaction_row(
    *,
    fingerprint: str,
    counterparty: str | None = None,
    purpose: str | None = None,
    note: str | None = None,
    counterparty_iban: str | None = None,
    external_ref: str | None = None,
    search_text: str = "",
    occurrence_index: int = 0,
) -> dict[str, Any]:
    """Baut eine Buchungszeile auf dem Stand 0003 (Suchspalte noch leer)."""
    return {
        "account_id": 1,
        "status": "gebucht",
        "booking_date": TEST_BOOKING_DATE,
        "counterparty": counterparty,
        "purpose": purpose,
        "note": note,
        "counterparty_iban": counterparty_iban,
        "external_ref": external_ref,
        "search_text": search_text,
        "checked": False,
        "reconciled": False,
        "origin": "fints",
        "fingerprint": fingerprint,
        "occurrence_index": occurrence_index,
        "merged": False,
        "version": 1,
        "created_at": TEST_TIMESTAMP,
        "updated_at": TEST_TIMESTAMP,
        "amount_cents": -1234,
        "currency": "EUR",
    }


def test_migration_0004_backfills_search_text_from_0003(tmp_path: Path) -> None:
    """Bestandsbuchungen aus 0003 erhalten die gebaute Suchspalte (F-SUC-01).

    Der Vergleich mit :func:`build_search_text` belegt zugleich die Paritaet der
    eingefrorenen Migrationsnormalisierung mit der Laufzeitregel (F-SUC-06).
    """
    db_path = tmp_path / "migration_0004_backfill.db"
    upgrade_to_revision(db_path, PREVIOUS_REVISION)

    rows = [
        transaction_row(
            fingerprint="a" * 64,
            counterparty="Max Mustermann",
            purpose="Gehalt Juni",
            note="Wichtig",
            counterparty_iban="DE02 1203 0000 0000 2020 51",
            external_ref="TX-42",
        ),
        transaction_row(
            fingerprint="b" * 64,
            counterparty="Müller GmbH",
            purpose="Straße 1",
            counterparty_iban=None,
        ),
        transaction_row(
            fingerprint="c" * 64,
            counterparty=None,
            purpose="Zeile\n\nmit  Umbruch",
            note=None,
            counterparty_iban=None,
        ),
        transaction_row(fingerprint="d" * 64),
    ]
    insert_rows(db_path, "account", [ACCOUNT_ROW])
    insert_rows(db_path, "transaction", rows)

    upgrade_to_head(sqlite_url(db_path))

    after = {row["fingerprint"]: row for row in fetch_rows(db_path, "transaction")}
    assert set(after) == {"a" * 64, "b" * 64, "c" * 64, "d" * 64}

    for row in rows:
        stored = after[row["fingerprint"]]
        expected = build_search_text(
            counterparty=row["counterparty"],
            purpose=row["purpose"],
            note=row["note"],
            counterparty_iban=row["counterparty_iban"],
            external_ref=row["external_ref"],
        )
        assert stored["search_text"] == expected, f"Backfill weicht ab fuer {row['fingerprint'][:4]}"

    # Die kompakte IBAN ist ohne Leerzeichen durchsuchbar.
    assert "de02120300000000202051" in after["a" * 64]["search_text"]
    # Bestandsdaten bleiben erhalten.
    assert after["a" * 64]["counterparty"] == "Max Mustermann"
    assert after["a" * 64]["checked"] is False


def test_migration_0004_processes_multiple_batches(tmp_path: Path) -> None:
    """Mehr als ein Batch (BATCH_SIZE) wird vollstaendig befuellt (F-SUC-06)."""
    db_path = tmp_path / "migration_0004_batches.db"
    upgrade_to_revision(db_path, PREVIOUS_REVISION)

    total = 600
    rows = [
        transaction_row(
            fingerprint=f"{index:064x}",
            counterparty=f"Partei {index:03d}",
            purpose="Zweck",
        )
        for index in range(total)
    ]
    insert_rows(db_path, "account", [ACCOUNT_ROW])
    insert_rows(db_path, "transaction", rows)

    upgrade_to_head(sqlite_url(db_path))

    stored = fetch_rows(db_path, "transaction")
    assert len(stored) == total
    for row in stored:
        assert row["search_text"] == f"partei {int(row['fingerprint'], 16):03d} zweck"


def test_migration_0004_schema_matches_models(tmp_path: Path) -> None:
    """upgrade head laesst das Schema ohne Drift (F-SUC-06, N-DB-01)."""
    db_path = tmp_path / "migration_0004_schema.db"
    url = sqlite_url(db_path)

    upgrade_to_head(url)

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
        assert diff == [], f"Drift zwischen Modellen und migriertem Schema: {diff}"
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("transaction")}
        assert "search_text" in columns
    finally:
        engine.dispose()


def test_migration_0004_downgrade_is_not_supported(tmp_path: Path) -> None:
    """downgrade() bricht ohne Aenderung ab (N-DB-03: nur vorwaerts)."""
    db_path = tmp_path / "migration_0004_downgrade.db"
    upgrade_to_head(sqlite_url(db_path))
    insert_rows(db_path, "account", [ACCOUNT_ROW])
    insert_rows(
        db_path,
        "transaction",
        [transaction_row(fingerprint="e" * 64, counterparty="Max Mustermann", purpose="Gehalt")],
    )

    before = fetch_rows(db_path, "transaction")

    with pytest.raises(NotImplementedError):
        command.downgrade(build_alembic_config(db_path), "base")

    after = fetch_rows(db_path, "transaction")
    assert after == before, "Ein abgebrochener downgrade darf keine Daten veraendern"
