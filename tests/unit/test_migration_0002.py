"""Test fuer Migration 0002 (T-063).

Referenziert N-DB-01 (Migrationen mit Alembic, kein rohes SQL fuer Daten),
N-DB-03 (Schema-Versionierung, Migrationen nur vorwaerts, Bestandsdaten bleiben
erhalten), F-SYN-05 (Bankcode -> Klartext), F-SYN-07 (Lauf-Gruppierung),
F-SYN-08 (PIN verschluesselt speichern oder je Sync abfragen) und F-SYN-09
(pflegbares Fehler-Mapping mit optionaler BLZ).

Der Test faehrt echte ``upgrade``-Laeufe gegen eine Datei-basierte
SQLite-Datenbank in ``tmp_path``:

* ``upgrade head`` von leer und ``compare_metadata`` ohne Abweichung,
* Bestandsdaten in ``bank_access`` und ``import_run`` ueberstehen das Upgrade
  von Revision 0001; ``store_pin`` wird ``true``, ``job_id`` bleibt NULL,
* Unique ``(code, blz)`` und der Index auf ``code`` wirken,
* ``downgrade`` bricht ohne Schema- oder Datenaenderung ab (N-DB-03).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect
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

PREVIOUS_REVISION: Final[str] = "0001"
TEST_TIMESTAMP: Final[datetime] = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

BANK_ROW: Final[dict[str, Any]] = {
    "blz": "10010010",
    "name": "Testbank Eins",
    "website": None,
    "fints_url": None,
}

BANK_ACCESS_ROW: Final[dict[str, Any]] = {
    "bank_id": 1,
    "login_name": "test-login",
    "pin_encrypted": None,
    "tan_method": "946",
    "version": 1,
    "created_at": TEST_TIMESTAMP,
    "updated_at": TEST_TIMESTAMP,
}

ACCOUNT_ROW: Final[dict[str, Any]] = {
    "name": "Testkonto",
    "currency": "EUR",
    "source": "fints",
    "include_in_total": True,
    "bank_access_id": 1,
    "opening_balance_cents": 0,
    "active": True,
    "version": 1,
    "created_at": TEST_TIMESTAMP,
    "updated_at": TEST_TIMESTAMP,
}

IMPORT_RUN_ROW: Final[dict[str, Any]] = {
    "account_id": 1,
    "kind": "sync",
    "status": "erfolgreich",
    "started_at": TEST_TIMESTAMP,
    "finished_at": TEST_TIMESTAMP,
    "new_count": 3,
    "skipped_count": 1,
    "suspected_count": 0,
    "failed_count": 0,
    "message": "Testlauf ohne echte Bankdaten",
    "version": 1,
    "created_at": TEST_TIMESTAMP,
    "updated_at": TEST_TIMESTAMP,
}


def test_migration_0002_schema_matches_models(tmp_path: Path) -> None:
    """upgrade head erzeugt das Modellschema ohne Drift (N-DB-01, N-DB-03).

    ``compare_metadata`` vergleicht das migrierte Schema inklusive Typen und
    Indizes mit ``Base.metadata``. Eine leere Diff-Liste bedeutet, dass ein
    weiterer Autogenerate-Lauf keine Aenderung erzeugen wuerde.
    """
    db_path = tmp_path / "migration_0002_schema.db"
    url = sqlite_url(db_path)

    upgrade_to_head(url)

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
        assert diff == [], f"Drift zwischen Modellen und migriertem Schema: {diff}"

        inspector = inspect(engine)
        assert "bank_error_mapping" in inspector.get_table_names()

        bank_access_columns = {column["name"]: column for column in inspector.get_columns("bank_access")}
        assert "store_pin" in bank_access_columns, "F-SYN-08: bank_access.store_pin fehlt"
        assert bank_access_columns["store_pin"]["nullable"] is False

        import_run_columns = {column["name"]: column for column in inspector.get_columns("import_run")}
        assert "job_id" in import_run_columns, "F-SYN-07: import_run.job_id fehlt"
        assert import_run_columns["job_id"]["nullable"] is True

        import_run_indexes = {index["name"] for index in inspector.get_indexes("import_run")}
        assert "ix_import_run_job_id" in import_run_indexes, f"Index fehlt: {import_run_indexes}"

        mapping_indexes = {index["name"] for index in inspector.get_indexes("bank_error_mapping")}
        assert "ix_bank_error_mapping_code" in mapping_indexes, f"Index fehlt: {mapping_indexes}"
    finally:
        engine.dispose()


def test_migration_0002_preserves_existing_data(tmp_path: Path) -> None:
    """Bestandsdaten aus 0001 bleiben erhalten (N-DB-03, F-SYN-07, F-SYN-08).

    Legt Bank, Bankzugang, Konto und Import-Lauf auf Revision 0001 an (ueber
    reflektierte Tabellen, kein rohes SQL) und hebt dann auf head an. Der
    Zugang behaelt seine Felder und erhaelt ``store_pin = true``; der Lauf
    behaelt seine Felder und erhaelt ``job_id = NULL``.
    """
    db_path = tmp_path / "migration_0002_data.db"
    upgrade_to_revision(db_path, PREVIOUS_REVISION)

    insert_rows(db_path, "bank", [BANK_ROW])
    insert_rows(db_path, "bank_access", [BANK_ACCESS_ROW])
    insert_rows(db_path, "account", [ACCOUNT_ROW])
    insert_rows(db_path, "import_run", [IMPORT_RUN_ROW])

    access_before = fetch_rows(db_path, "bank_access")
    run_before = fetch_rows(db_path, "import_run")
    assert len(access_before) == 1
    assert len(run_before) == 1

    upgrade_to_head(sqlite_url(db_path))

    access_after = fetch_rows(db_path, "bank_access")
    run_after = fetch_rows(db_path, "import_run")
    assert len(access_after) == 1, "Bankzugang ist beim Upgrade verloren gegangen"
    assert len(run_after) == 1, "Import-Lauf ist beim Upgrade verloren gegangen"

    access = access_after[0]
    assert access["login_name"] == "test-login"
    assert access["tan_method"] == "946"
    assert access["store_pin"] is True, "Bestandszeilen muessen store_pin=true erhalten (F-SYN-08)"

    run = run_after[0]
    assert run["kind"] == "sync"
    assert run["status"] == "erfolgreich"
    assert run["new_count"] == 3
    assert run["job_id"] is None, "Einzelner Bestandslauf darf keine Batch-Gruppe erhalten"


def test_migration_0002_unique_code_blz_enforced(tmp_path: Path) -> None:
    """Unique (code, blz) verhindert doppelte Eintraege je Geltungsbereich (F-SYN-05/09).

    Ein zweiter Eintrag mit gleichem Code und gleicher BLZ wird abgewiesen.
    Derselbe Code mit anderer BLZ bleibt erlaubt, damit Standard- und
    bankspezifische Eintraege nebeneinander bestehen koennen.
    """
    db_path = tmp_path / "migration_0002_unique.db"
    upgrade_to_head(sqlite_url(db_path))

    base_row: dict[str, Any] = {
        "message": "Testmeldung",
        "recommendation": "Testempfehlung",
        "retryable": False,
        "is_default": False,
        "version": 1,
    }
    insert_rows(
        db_path,
        "bank_error_mapping",
        [
            {"code": "9800", "blz": "10010010", **base_row},
            {"code": "9800", "blz": "20020020", **base_row},
        ],
    )

    with pytest.raises(IntegrityError):
        insert_rows(db_path, "bank_error_mapping", [{"code": "9800", "blz": "10010010", **base_row}])

    rows = fetch_rows(db_path, "bank_error_mapping")
    assert len(rows) == 2, f"Unique-Verletzung darf keine Zeile hinterlassen: {rows}"


def test_migration_0002_downgrade_is_not_supported(tmp_path: Path) -> None:
    """downgrade() bricht ohne Aenderung ab (N-DB-03: nur vorwaerts).

    Der Test ruft ``downgrade`` echt auf und prueft, dass die
    ``NotImplementedError``-Grenze greift und weder Schema noch Daten
    veraendert werden.
    """
    db_path = tmp_path / "migration_0002_downgrade.db"
    url = sqlite_url(db_path)
    upgrade_to_head(url)

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            mapping = Base.metadata.tables["bank_error_mapping"]
            connection.execute(
                mapping.insert().values(
                    code="9010",
                    blz=None,
                    message="Testmeldung",
                    recommendation="Testempfehlung",
                    retryable=True,
                    is_default=True,
                    version=1,
                )
            )
    finally:
        engine.dispose()

    before = fetch_rows(db_path, "bank_error_mapping")

    with pytest.raises(NotImplementedError):
        command.downgrade(build_alembic_config(db_path), "base")

    after = fetch_rows(db_path, "bank_error_mapping")
    assert after == before, "Ein abgebrochener downgrade darf keine Daten veraendern"
