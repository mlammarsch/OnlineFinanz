"""Test fuer Migration 0003 (T-085).

Referenziert N-DB-01 (Migrationen mit Alembic, kein rohes SQL fuer Daten),
N-DB-03 (Schema-Versionierung, Migrationen nur vorwaerts, Bestandsdaten
bleiben erhalten), F-ABS-01 (Herkunft der Kennzeichen), F-KTO-10 (Banken-
Stammdaten mit Icon), F-EXP-03 (speicherbare Exportprofile) und F-CON-02
(Versionsspalte fuer optimistisches Sperren).

Der Test faehrt echte ``upgrade``-Laeufe gegen eine Datei-basierte
SQLite-Datenbank in ``tmp_path``:

* ``upgrade head`` von leer und ``compare_metadata`` ohne Abweichung, genau ein
  Head generisch geprueft (kein hart codierter Head),
* Bestandsdaten aus Revision 0002 ueberstehen das Upgrade; neue Spalten sind
  NULL bzw. tragen ``version = 1``,
* CHECK-Bedingungen der Herkunftsspalten und die Eindeutigkeit des Profilnamens
  wirken,
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
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

import finue.models  # noqa: F401  registriert alle Tabellen in Base.metadata
from finue.db.base import Base
from finue.db.engine import create_db_engine
from finue.db.migrate import upgrade_to_head
from helpers_migration import (
    build_alembic_config,
    fetch_rows,
    insert_rows,
    sqlite_url,
    upgrade_to_revision,
)

PREVIOUS_REVISION: Final[str] = "0002"
TEST_TIMESTAMP: Final[datetime] = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TEST_BOOKING_DATE: Final[date] = date(2026, 1, 3)

#: Spalten der neuen Tabelle export_profile (F-EXP-03).
EXPORT_PROFILE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"id", "name", "is_default", "options", "columns", "version", "created_at", "updated_at"}
)

#: Neue Indizes auf transaction fuer Liste/Suche (F-SUC-01, F-BUC-01).
NEW_TRANSACTION_INDEXES: Final[frozenset[str]] = frozenset(
    {
        "ix_transaction_account_status_booking_id",
        "ix_transaction_booking_id",
        "ix_transaction_amount_cents",
    }
)

BANK_ROW: Final[dict[str, Any]] = {
    "blz": "10010010",
    "name": "Testbank Eins",
    "website": None,
    "fints_url": None,
}

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

TRANSACTION_ROW: Final[dict[str, Any]] = {
    "account_id": 1,
    "status": "gebucht",
    "booking_date": TEST_BOOKING_DATE,
    "search_text": "test zweck",
    "checked": False,
    "reconciled": False,
    "origin": "fints",
    "fingerprint": "a" * 64,
    "occurrence_index": 0,
    "merged": False,
    "version": 1,
    "created_at": TEST_TIMESTAMP,
    "updated_at": TEST_TIMESTAMP,
    "amount_cents": -1234,
    "currency": "EUR",
}

DUPLICATE_DECISION_ROW: Final[dict[str, Any]] = {
    "transaction_id": 1,
    "other_transaction_id": None,
    "decision": "behalten",
    "decided_at": TEST_TIMESTAMP,
    "source": "ui",
    "version": 1,
    "created_at": TEST_TIMESTAMP,
    "updated_at": TEST_TIMESTAMP,
}


def _insert_transaction(db_path: Path, **overrides: Any) -> None:
    """Fuegt eine Buchung ueber die reflektierte Tabelle ein (kein rohes SQL)."""
    insert_rows(db_path, "transaction", [{**TRANSACTION_ROW, **overrides}])


def test_migration_0003_schema_matches_models(tmp_path: Path) -> None:
    """upgrade head erzeugt das Modellschema ohne Drift (N-DB-01, N-DB-03).

    ``compare_metadata`` vergleicht das migrierte Schema inklusive Typen und
    Indizes mit ``Base.metadata``. Eine leere Diff-Liste bedeutet, dass ein
    weiterer Autogenerate-Lauf keine Aenderung erzeugen wuerde.
    """
    db_path = tmp_path / "migration_0003_schema.db"
    url = sqlite_url(db_path)

    upgrade_to_head(url)

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
            # Genau ein Head, generisch geprueft (kein hart codierter Head-Wert).
            heads = list(ScriptDirectory.from_config(build_alembic_config(db_path)).get_heads())
            assert len(heads) == 1, f"Erwartet genau ein Head, gefunden: {heads}"
            assert context.get_current_revision() == heads[0]
        assert diff == [], f"Drift zwischen Modellen und migriertem Schema: {diff}"

        inspector = inspect(engine)

        transaction_columns = {column["name"]: column for column in inspector.get_columns("transaction")}
        for name in ("checked_origin", "reconciled_origin"):
            assert name in transaction_columns, f"F-ABS-01: transaction.{name} fehlt"
            assert transaction_columns[name]["nullable"] is True

        transaction_indexes = {index["name"] for index in inspector.get_indexes("transaction")}
        assert NEW_TRANSACTION_INDEXES <= transaction_indexes, (
            f"Indizes fehlen: erwartet {NEW_TRANSACTION_INDEXES}, vorhanden {transaction_indexes}"
        )
        # Bestehende Indizes bleiben erhalten.
        assert {"ix_transaction_account_booking", "ix_transaction_fingerprint"} <= transaction_indexes

        bank_columns = {column["name"]: column for column in inspector.get_columns("bank")}
        assert "version" in bank_columns, "F-CON-02: bank.version fehlt"
        assert bank_columns["version"]["nullable"] is False
        assert "icon" in bank_columns, "F-KTO-10: bank.icon fehlt"
        assert "icon_mime" in bank_columns, "F-KTO-10: bank.icon_mime fehlt"
        assert bank_columns["icon"]["nullable"] is True
        assert bank_columns["icon_mime"]["nullable"] is True

        assert "export_profile" in inspector.get_table_names()
        profile_columns = {column["name"] for column in inspector.get_columns("export_profile")}
        assert profile_columns == set(EXPORT_PROFILE_COLUMNS)
    finally:
        engine.dispose()


def test_migration_0003_preserves_existing_data_from_0002(tmp_path: Path) -> None:
    """Bestandsdaten aus 0002 bleiben erhalten (N-DB-03, F-ABS-01, F-CON-02).

    Legt Bank, Konto, Buchung und Dublettenentscheidung auf Revision 0002 an
    (ueber reflektierte Tabellen, kein rohes SQL) und hebt dann auf head an. Die
    Zeilen bleiben erhalten; die Bank erhaelt ``version = 1``, die Buchung
    erhaelt NULL-Herkunftsspalten. Die Kindzeile in ``duplicate_decision`` darf
    der Tabellenumbau der CHECK-Bedingungen nicht loeschen.
    """
    db_path = tmp_path / "migration_0003_data.db"
    upgrade_to_revision(db_path, PREVIOUS_REVISION)

    insert_rows(db_path, "bank", [BANK_ROW])
    insert_rows(db_path, "account", [ACCOUNT_ROW])
    insert_rows(db_path, "transaction", [TRANSACTION_ROW])
    insert_rows(db_path, "duplicate_decision", [DUPLICATE_DECISION_ROW])

    assert len(fetch_rows(db_path, "duplicate_decision")) == 1

    upgrade_to_head(sqlite_url(db_path))

    bank_after = fetch_rows(db_path, "bank")
    transaction_after = fetch_rows(db_path, "transaction")
    decisions_after = fetch_rows(db_path, "duplicate_decision")

    assert len(bank_after) == 1, "Bank ist beim Upgrade verloren gegangen"
    assert len(transaction_after) == 1, "Buchung ist beim Upgrade verloren gegangen"
    assert len(decisions_after) == 1, "Dublettenentscheidung wurde beim Tabellenumbau geloescht"

    bank = bank_after[0]
    assert bank["blz"] == "10010010"
    assert bank["version"] == 1, "Bestandszeilen muessen version=1 erhalten (F-CON-02)"
    assert bank["icon"] is None, "Neue Icon-Spalte ist fuer Bestandszeilen NULL"
    assert bank["icon_mime"] is None

    transaction = transaction_after[0]
    assert transaction["fingerprint"] == "a" * 64
    assert transaction["checked"] is False
    assert transaction["checked_origin"] is None, "Neue Spalte ist fuer Bestandszeilen NULL"
    assert transaction["reconciled_origin"] is None


def test_migration_0003_origin_check_constraints_enforced(tmp_path: Path) -> None:
    """Die Herkunftsspalten lassen nur NULL und ui/mcp/import zu (F-ABS-01)."""
    db_path = tmp_path / "migration_0003_origin_check.db"
    upgrade_to_head(sqlite_url(db_path))
    insert_rows(db_path, "account", [ACCOUNT_ROW])

    # Gueltige Werte werden angenommen.
    _insert_transaction(db_path, fingerprint="b" * 64, checked_origin="ui", reconciled_origin="import")
    _insert_transaction(db_path, fingerprint="c" * 64, checked_origin=None, reconciled_origin=None)
    assert len(fetch_rows(db_path, "transaction")) == 2

    # Ungueltige Werte werden von der CHECK-Bedingung abgewiesen.
    with pytest.raises(IntegrityError):
        _insert_transaction(db_path, fingerprint="d" * 64, checked_origin="sms")
    with pytest.raises(IntegrityError):
        _insert_transaction(db_path, fingerprint="e" * 64, reconciled_origin="bank")

    assert len(fetch_rows(db_path, "transaction")) == 2, "Abgewiesene Zeilen duerfen nicht gespeichert werden"


def test_migration_0003_export_profile_name_is_unique(tmp_path: Path) -> None:
    """export_profile.name ist eindeutig (F-EXP-03)."""
    db_path = tmp_path / "migration_0003_profile_unique.db"
    upgrade_to_head(sqlite_url(db_path))

    row: dict[str, Any] = {
        "name": "Finanzblick-angelehnt",
        "is_default": True,
        "options": {"delimiter": ";"},
        "columns": ["booking_date", "amount"],
        "version": 1,
        "created_at": TEST_TIMESTAMP,
        "updated_at": TEST_TIMESTAMP,
    }
    insert_rows(db_path, "export_profile", [row])

    with pytest.raises(IntegrityError):
        insert_rows(db_path, "export_profile", [{**row, "is_default": False}])

    rows = fetch_rows(db_path, "export_profile")
    assert len(rows) == 1, f"Unique-Verletzung darf keine Zeile hinterlassen: {rows}"


def test_migration_0003_downgrade_is_not_supported(tmp_path: Path) -> None:
    """downgrade() bricht ohne Aenderung ab (N-DB-03: nur vorwaerts)."""
    db_path = tmp_path / "migration_0003_downgrade.db"
    upgrade_to_head(sqlite_url(db_path))
    insert_rows(db_path, "account", [ACCOUNT_ROW])
    _insert_transaction(db_path, fingerprint="f" * 64)

    before = fetch_rows(db_path, "transaction")

    with pytest.raises(NotImplementedError):
        command.downgrade(build_alembic_config(db_path), "base")

    after = fetch_rows(db_path, "transaction")
    assert after == before, "Ein abgebrochener downgrade darf keine Daten veraendern"
