"""Tests für die Modelle Import-Lauf und Lauf-Fehler.

Referenziert N-DB-01 (kein rohes SQL), F-SYN-05 (Bankcode -> Klartext),
F-SYN-07 (Sync-Protokoll je Import-Lauf mit Status und Zählern), F-IMP-06
(Datei-Import als Import-Lauf mit Protokoll), N-SEC-02 (keine Geheimnisse in
Meldungen) und F-CON-01 (Versionsspalte für optimistische Sperre).
"""

import subprocess
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import Bank, BankAccess  # noqa: F401
from finue.models.import_run import ImportRun, ImportRunError


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests mit aktivierten Foreign Keys."""
    engine = create_db_engine("sqlite://")

    # Foreign Keys aktivieren für SQLite
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def make_test_account(name: str = "Testkonto") -> Account:
    """Hilfsfunktion zum Erstellen eines Test-Kontos."""
    return Account(
        name=name,
        currency="EUR",
        source="fints",
    )


def make_test_run(account_id: int, kind: str = "sync") -> ImportRun:
    """Hilfsfunktion zum Erstellen eines Import-Laufs."""
    return ImportRun(
        account_id=account_id,
        kind=kind,
    )


def test_import_run_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur
    finue.models.import_run importiert wird, ohne Zirkelimport-Fehler.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.import_run
configure_mappers()
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Mapper configuration fehlgeschlagen: {result.stderr}"
    assert "OK" in result.stdout


def test_import_run_with_two_errors(factory) -> None:  # type: ignore[no-untyped-def]
    """Ein Lauf mit zwei Fehlern wird angelegt und gelesen (F-SYN-05, F-IMP-06)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="csv")
        run.status = "fehler"
        run.new_count = 3
        run.skipped_count = 1
        run.suspected_count = 2
        run.failed_count = 2
        run.message = "Zwei Zeilen konnten nicht gelesen werden."
        run.errors.append(
            ImportRunError(
                row_no=4,
                code="FORMAT",
                message="Betragsspalte fehlt.",
            )
        )
        run.errors.append(
            ImportRunError(
                row_no=7,
                code="FORMAT",
                message="Buchungsdatum ungueltig.",
            )
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert retrieved.account_id == account_id
        assert retrieved.kind == "csv"
        assert retrieved.status == "fehler"
        assert retrieved.new_count == 3
        assert retrieved.skipped_count == 1
        assert retrieved.suspected_count == 2
        assert retrieved.failed_count == 2
        assert retrieved.message == "Zwei Zeilen konnten nicht gelesen werden."
        assert len(retrieved.errors) == 2
        first, second = retrieved.errors
        assert first.row_no == 4
        assert first.code == "FORMAT"
        assert first.message == "Betragsspalte fehlt."
        assert second.row_no == 7
        assert second.code == "FORMAT"
        assert second.message == "Buchungsdatum ungueltig."


def test_import_run_status_defaults_to_running(factory) -> None:  # type: ignore[no-untyped-def]
    """Ein neuer Lauf startet mit Status 'laeuft' und Zähler 0 (F-SYN-07)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="sync")
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert retrieved.status == "laeuft"
        assert retrieved.new_count == 0
        assert retrieved.skipped_count == 0
        assert retrieved.suspected_count == 0
        assert retrieved.failed_count == 0
        assert retrieved.finished_at is None
        assert retrieved.version == 1


def test_import_run_status_wartet_auf_tan(factory) -> None:  # type: ignore[no-untyped-def]
    """Status 'wartet_auf_tan' bleibt als technischer Zwischenzustand erlaubt.

    F-SYN-07 fuehrt ihn nicht als abschliessenden Status; er beschreibt den
    angehaltenen FinTS-Dialog (N-SEC-03) und wird daher im Constraint belassen.
    """
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="sync")
        run.status = "wartet_auf_tan"
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert retrieved.status == "wartet_auf_tan"


@pytest.mark.parametrize(
    "status",
    ["erfolgreich", "teilweise", "fehler", "abgebrochen", "zeitueberschreitung"],
)
def test_import_run_allows_spec_final_status(factory, status) -> None:  # type: ignore[no-untyped-def]
    """Die abschliessenden F-SYN-07-Statuswerte sind im Constraint erlaubt.

    Geprueft werden insbesondere 'teilweise', 'fehler' und
    'zeitueberschreitung'; 'laeuft' und 'wartet_auf_tan' bleiben als
    technische Zwischenzustaende separat erlaubt.
    """
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="sync")
        run.status = status
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert retrieved.status == status


def test_import_run_rejects_invalid_kind(factory) -> None:  # type: ignore[no-untyped-def]
    """kind-Check lehnt ungültige Werte ab."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id)
        run.kind = "excel"  # Nicht erlaubt
        session.add(run)
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_run_rejects_invalid_status(factory) -> None:  # type: ignore[no-untyped-def]
    """status-Check lehnt ungültige Werte ab (F-SYN-07)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id)
        run.status = "unbekannt"  # Nicht erlaubt
        session.add(run)
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_run_rejects_fk_violation(factory) -> None:  # type: ignore[no-untyped-def]
    """FK-Verletzung (unbekannte account_id) wird abgelehnt."""
    with factory() as session:
        run = make_test_run(9999)  # Nicht existierende Konto-ID
        session.add(run)
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_run_error_rejects_fk_violation(factory) -> None:  # type: ignore[no-untyped-def]
    """FK-Verletzung (unbekannte run_id) wird abgelehnt."""
    with factory() as session:
        error = ImportRunError(
            run_id=9999,  # Nicht existierende Lauf-ID
            row_no=1,
            code="FORMAT",
            message="Unbekannter Lauf.",
        )
        session.add(error)
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_run_error_row_no_nullable(factory) -> None:  # type: ignore[no-untyped-def]
    """Ein Lauf-Fehler ohne Zeilenbezug (row_no NULL) ist erlaubt (F-SYN-05)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="sync")
        run.errors.append(
            ImportRunError(
                row_no=None,
                code="9800",
                message="Bank meldet: Auftrag nicht ausgefuehrt, bitte erneut versuchen.",
            )
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert len(retrieved.errors) == 1
        assert retrieved.errors[0].row_no is None
        assert retrieved.errors[0].code == "9800"


def test_import_run_cascade_delete_removes_errors(factory) -> None:  # type: ignore[no-untyped-def]
    """Lauf löschen entfernt seine Fehler (CASCADE)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="csv")
        run.errors.append(ImportRunError(row_no=1, code="FORMAT", message="Fehler 1."))
        run.errors.append(ImportRunError(row_no=2, code="FORMAT", message="Fehler 2."))
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        session.execute(delete(ImportRun).where(ImportRun.id == run_id))
        session.commit()

    with factory() as session:
        remaining = session.query(ImportRunError).filter_by(run_id=run_id).all()
        assert remaining == []


def test_account_cascade_delete_removes_runs(factory) -> None:  # type: ignore[no-untyped-def]
    """Konto löschen entfernt seine Läufe und deren Fehler (CASCADE)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="json")
        run.errors.append(ImportRunError(row_no=1, code="FORMAT", message="Fehler."))
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        session.execute(delete(Account).where(Account.id == account_id))
        session.commit()

    with factory() as session:
        assert session.query(ImportRun).filter_by(id=run_id).one_or_none() is None
        assert session.query(ImportRunError).filter_by(run_id=run_id).all() == []


def test_import_run_timestamps_are_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """started_at und finished_at sind aware UTC (N-DB-02)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="csv")
        run.finished_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert isinstance(retrieved.started_at, datetime)
        assert retrieved.started_at.tzinfo is not None
        assert retrieved.started_at.tzinfo == timezone.utc
        assert isinstance(retrieved.finished_at, datetime)
        assert retrieved.finished_at.tzinfo is not None
        assert retrieved.finished_at.tzinfo == timezone.utc


def test_import_run_version_increments(factory) -> None:  # type: ignore[no-untyped-def]
    """Version steigt bei Update des Laufs (F-CON-01)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        run = make_test_run(account_id, kind="csv")
        session.add(run)
        session.commit()
        run_id = run.id
        initial_version = run.version
        assert initial_version == 1

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        retrieved.status = "erfolgreich"
        session.commit()

    with factory() as session:
        retrieved = session.query(ImportRun).filter_by(id=run_id).one()
        assert retrieved.version == initial_version + 1
