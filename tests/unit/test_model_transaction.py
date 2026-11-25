"""Tests für das Buchungs-Modell.

Referenziert N-DB-02 (Beträge in Cent, Zeitstempel in UTC), F-KTO-07 (Löschen
des Kontos entfernt Buchungen), F-SUC-06 (normalisierte Suchspalte), F-ABS-01
(abgestimmt-Flag), F-DUP-01 (Fingerprint für Dublettenkennung), F-DUP-02
(Vorkommen-Index) und F-CON-01 (Versionsspalte für optimistische Sperre).
"""

import subprocess
import sys
import time
from datetime import date, datetime, timezone, timedelta

import pytest
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import Bank, BankAccess  # noqa: F401
from finue.models.transaction import Transaction


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def make_test_transaction(
    account_id: int,
    amount_cents: int = 10000,
    booking_date: date | None = None,
    origin: str = "fints",
    fingerprint: str = "a" * 64,
    occurrence_index: int = 1,
) -> Transaction:
    """Hilfsfunktion zum Erstellen von Test-Buchungen mit Pflichtfeldern."""
    if booking_date is None:
        booking_date = date(2024, 1, 15)
    return Transaction(
        account_id=account_id,
        amount_cents=amount_cents,
        currency="EUR",
        booking_date=booking_date,
        origin=origin,
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
    )


@pytest.mark.parametrize(
    "module_name",
    ["finue.models.bank", "finue.models.account", "finue.models.transaction"],
)
def test_mapper_entry_points_configure(module_name: str) -> None:
    """N-DB-01: Jedes Modell-Modul kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur ein Modul
    importiert wird, ohne Zirkelimport-Fehler (NameError für 'BankAccess' etc.).
    """
    code = f"""
from sqlalchemy.orm import configure_mappers
import {module_name}
configure_mappers()
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Mapper konfiguration fehlgeschlagen für {module_name}: {result.stderr}"
    assert "OK" in result.stdout


def test_create_transaction_with_required_fields(factory) -> None:  # type: ignore[no-untyped-def]
    """Buchung kann mit Pflichtfeldern erstellt werden (N-DB-01, F-DUP-01)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            amount_cents=10050,
            origin="fints",
            fingerprint="b" * 64,
        )
        session.add(transaction)
        session.commit()
        assert transaction.id is not None
        assert transaction.account_id == account_id
        assert transaction.amount_cents == 10050
        assert transaction.currency == "EUR"
        assert transaction.booking_date == date(2024, 1, 15)
        assert transaction.origin == "fints"
        assert transaction.fingerprint == "b" * 64


def test_transaction_defaults(factory) -> None:  # type: ignore[no-untyped-def]
    """Standardwerte sind korrekt gesetzt (N-DB-02, F-SUC-06, F-ABS-01, F-DUP-02, F-DUP-04)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            amount_cents=5000,
            booking_date=date(2024, 1, 20),
        )
        session.add(transaction)
        session.commit()
        assert transaction.status == "gebucht"
        assert transaction.currency == "EUR"
        assert transaction.search_text == ""
        assert transaction.value_date is None
        assert transaction.counterparty is None
        assert transaction.counterparty_iban is None
        assert transaction.purpose is None
        assert transaction.note is None
        assert transaction.checked is False
        assert transaction.checked_at is None
        assert transaction.reconciled is False
        assert transaction.reconciled_at is None
        assert transaction.occurrence_index == 1
        assert transaction.merged is False


def test_transaction_status_check_valid_values(factory) -> None:  # type: ignore[no-untyped-def]
    """Status kann 'gebucht' oder 'vorgemerkt' sein."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction_booked = make_test_transaction(
            account_id=account_id,
            amount_cents=1000,
            origin="fints",
            fingerprint="c" * 64,
        )
        transaction_booked.status = "gebucht"
        transaction_pending = make_test_transaction(
            account_id=account_id,
            amount_cents=2000,
            origin="csv",
            fingerprint="d" * 64,
        )
        transaction_pending.status = "vorgemerkt"
        session.add(transaction_booked)
        session.add(transaction_pending)
        session.commit()
        assert transaction_booked.status == "gebucht"
        assert transaction_pending.status == "vorgemerkt"


def test_transaction_status_check_invalid_value(factory) -> None:  # type: ignore[no-untyped-def]
    """Ungültige status-Werte werden abgelehnt (N-DB-02)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            amount_cents=1000,
        )
        transaction.status = "xyz"
        session.add(transaction)
        with pytest.raises(IntegrityError):
            session.commit()


def test_transaction_without_account_id_fails(factory) -> None:  # type: ignore[no-untyped-def]
    """Buchung ohne account_id wird abgelehnt (N-DB-01)."""
    with factory() as session:
        transaction = Transaction(
            amount_cents=1000,
            booking_date=date(2024, 1, 15),
            origin="fints",
            fingerprint="e" * 64,
            # account_id ist nicht gesetzt
        )
        session.add(transaction)
        with pytest.raises(IntegrityError):
            session.commit()


def test_delete_account_cascades_to_transactions(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-07: Konto-Löschen per ORM entfernt seine Buchungen (Cascade)."""
    with factory() as session:
        account = Account(
            name="Konto zum Löschen",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction1 = make_test_transaction(
            account_id=account_id,
            amount_cents=1000,
            fingerprint="f" * 64,
        )
        transaction2 = make_test_transaction(
            account_id=account_id,
            amount_cents=2000,
            fingerprint="0" * 64,
        )
        session.add(transaction1)
        session.add(transaction2)
        session.commit()
        transaction_id_1 = transaction1.id
        transaction_id_2 = transaction2.id

    with factory() as session:
        # Konto per ORM löschen
        account = session.get(Account, account_id)
        assert account is not None
        session.delete(account)
        session.commit()

    with factory() as session:
        # Buchungen sollten gelöscht sein
        transaction1 = session.get(Transaction, transaction_id_1)
        transaction2 = session.get(Transaction, transaction_id_2)
        assert transaction1 is None
        assert transaction2 is None


def test_delete_account_cascades_via_core(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-07: Konto-Löschen per Core-Delete entfernt seine Buchungen (Cascade)."""
    with factory() as session:
        account = Account(
            name="Konto zum Core-Löschen",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction1 = make_test_transaction(
            account_id=account_id,
            amount_cents=3000,
            booking_date=date(2024, 2, 1),
            fingerprint="1" * 64,
        )
        transaction2 = make_test_transaction(
            account_id=account_id,
            amount_cents=4000,
            booking_date=date(2024, 2, 2),
            fingerprint="2" * 64,
        )
        session.add(transaction1)
        session.add(transaction2)
        session.commit()
        transaction_id_1 = transaction1.id
        transaction_id_2 = transaction2.id

    with factory() as session:
        # Konto per Core-Delete löschen
        session.execute(delete(Account).where(Account.id == account_id))
        session.commit()
        session.expire_all()

    with factory() as session:
        # Buchungen sollten gelöscht sein
        transaction1 = session.get(Transaction, transaction_id_1)
        transaction2 = session.get(Transaction, transaction_id_2)
        assert transaction1 is None
        assert transaction2 is None


def test_account_transactions_relation(factory) -> None:  # type: ignore[no-untyped-def]
    """Bidirektionale Relation zwischen Account und Transaction."""
    with factory() as session:
        account = Account(
            name="Konto mit Buchungen",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction1 = make_test_transaction(
            account_id=account_id,
            amount_cents=5000,
            booking_date=date(2024, 3, 1),
            fingerprint="3" * 64,
        )
        transaction2 = make_test_transaction(
            account_id=account_id,
            amount_cents=6000,
            booking_date=date(2024, 3, 2),
            fingerprint="4" * 64,
        )
        session.add(transaction1)
        session.add(transaction2)
        session.commit()

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert len(account.transactions) == 2
        # Nicht auf Reihenfolge verlassen: nach ID sortieren
        transactions_sorted = sorted(account.transactions, key=lambda t: t.id)
        assert transactions_sorted[0].amount_cents == 5000
        assert transactions_sorted[1].amount_cents == 6000


def test_transaction_version_increments_on_update(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: Transaction.version startet bei 1 und steigt bei Update."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            amount_cents=7000,
            booking_date=date(2024, 4, 1),
            fingerprint="5" * 64,
        )
        session.add(transaction)
        session.commit()
        assert transaction.version == 1
        transaction_id = transaction.id

    with factory() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        assert transaction.version == 1
        transaction.purpose = "Miete"
        session.commit()
        assert transaction.version == 2


def test_transaction_timestamps(factory) -> None:  # type: ignore[no-untyped-def]
    """Transaction hat created_at und updated_at Zeitstempel (N-DB-02)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            amount_cents=8000,
            booking_date=date(2024, 5, 1),
            fingerprint="6" * 64,
        )
        session.add(transaction)
        session.commit()
        assert transaction.created_at is not None
        assert transaction.updated_at is not None
        assert transaction.created_at == transaction.updated_at
        transaction_id = transaction.id

    with factory() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        created = transaction.created_at
        time.sleep(0.01)
        transaction.note = "Test Note"
        session.commit()
        assert transaction.updated_at > created


def test_transaction_with_all_fields(factory) -> None:  # type: ignore[no-untyped-def]
    """Buchung kann mit allen Feldern erstellt werden (N-DB-01, F-ABS-01, F-DUP-01)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            amount_cents=15000,
            booking_date=date(2024, 6, 1),
            origin="csv",
            fingerprint="7" * 64,
        )
        transaction.status = "vorgemerkt"
        transaction.value_date = date(2024, 6, 3)
        transaction.counterparty = "Max Mustermann"
        transaction.counterparty_iban = "DE89370400440532013000"
        transaction.purpose = "Gehalt"
        transaction.search_text = "Max Mustermann Gehalt"
        transaction.note = "Gehalt Juni"
        transaction.checked = True
        transaction.reconciled = False
        transaction.external_ref = "ext-123"
        transaction.occurrence_index = 2
        session.add(transaction)
        session.commit()
        transaction_id = transaction.id

    with factory() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        assert transaction.amount_cents == 15000
        assert transaction.currency == "EUR"
        assert transaction.status == "vorgemerkt"
        assert transaction.booking_date == date(2024, 6, 1)
        assert transaction.value_date == date(2024, 6, 3)
        assert transaction.counterparty == "Max Mustermann"
        assert transaction.counterparty_iban == "DE89370400440532013000"
        assert transaction.purpose == "Gehalt"
        assert transaction.search_text == "Max Mustermann Gehalt"
        assert transaction.note == "Gehalt Juni"
        assert transaction.origin == "csv"
        assert transaction.fingerprint == "7" * 64
        assert transaction.checked is True
        assert transaction.reconciled is False
        assert transaction.external_ref == "ext-123"
        assert transaction.occurrence_index == 2


def test_transaction_origin_check_valid_values(factory) -> None:  # type: ignore[no-untyped-def]
    """Origin kann 'fints', 'csv' oder 'json' sein (F-DUP-01)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        for idx, origin in enumerate(["fints", "csv", "json"]):
            transaction = make_test_transaction(
                account_id=account_id,
                origin=origin,
                fingerprint=chr(97 + idx) * 64,
            )
            session.add(transaction)
        session.commit()

    with factory() as session:
        transactions = session.query(Transaction).filter_by(account_id=account_id).all()
        assert len(transactions) == 3
        origins = {t.origin for t in transactions}
        assert origins == {"fints", "csv", "json"}


def test_transaction_origin_check_invalid_value(factory) -> None:  # type: ignore[no-untyped-def]
    """Ungültige origin-Werte werden abgelehnt (F-DUP-01)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(account_id=account_id)
        transaction.origin = "xyz"
        session.add(transaction)
        with pytest.raises(IntegrityError):
            session.commit()


def test_transaction_unique_constraint_fingerprint_index(factory) -> None:  # type: ignore[no-untyped-def]
    """UniqueConstraint (account_id, fingerprint, occurrence_index) wird durchgesetzt (F-DUP-02)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    # Erste Buchung mit Fingerprint 'a'*64, occurrence_index=1
    with factory() as session:
        transaction1 = make_test_transaction(
            account_id=account_id,
            fingerprint="a" * 64,
            origin="fints",
        )
        session.add(transaction1)
        session.commit()

    # Zweite Buchung mit gleichem Fingerprint und Index sollte fehlschlagen
    with factory() as session:
        transaction2 = make_test_transaction(
            account_id=account_id,
            fingerprint="a" * 64,
            occurrence_index=1,
            origin="fints",
        )
        session.add(transaction2)
        with pytest.raises(IntegrityError):
            session.commit()

    # Zweite Buchung mit gleichem Fingerprint aber Index=2 sollte ok sein
    with factory() as session:
        transaction3 = make_test_transaction(
            account_id=account_id,
            fingerprint="a" * 64,
            occurrence_index=2,
            origin="csv",
        )
        session.add(transaction3)
        session.commit()
        assert transaction3.id is not None


def test_transaction_unique_constraint_different_account(factory) -> None:  # type: ignore[no-untyped-def]
    """Gleicher Fingerprint in anderem Konto ist erlaubt (F-DUP-01)."""
    with factory() as session:
        account1 = Account(
            name="Konto 1",
            currency="EUR",
            source="fints",
        )
        account2 = Account(
            name="Konto 2",
            currency="EUR",
            source="fints",
        )
        session.add(account1)
        session.add(account2)
        session.commit()
        account1_id = account1.id
        account2_id = account2.id

    with factory() as session:
        transaction1 = make_test_transaction(
            account_id=account1_id,
            fingerprint="same_fp" + "a" * 57,
            origin="fints",
        )
        transaction2 = make_test_transaction(
            account_id=account2_id,
            fingerprint="same_fp" + "a" * 57,
            origin="csv",
        )
        session.add(transaction1)
        session.add(transaction2)
        session.commit()
        assert transaction1.id is not None
        assert transaction2.id is not None


def test_transaction_indexes() -> None:  # type: ignore[no-untyped-def]
    """Indizes auf (account_id, booking_date) und fingerprint sind vorhanden."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    indexes = inspector.get_indexes("transaction")
    index_columns = {tuple(idx["column_names"]) for idx in indexes}

    # Prüfe auf Index auf (account_id, booking_date)
    assert ("account_id", "booking_date") in index_columns, (
        f"Index auf (account_id, booking_date) nicht gefunden. Vorhanden: {index_columns}"
    )

    # Prüfe auf Index auf fingerprint
    assert ("fingerprint",) in index_columns, f"Index auf fingerprint nicht gefunden. Vorhanden: {index_columns}"


def test_transaction_checked_reconciled_timestamps(factory) -> None:  # type: ignore[no-untyped-def]
    """checked_at und reconciled_at Zeitstempel sind nullable und optional (F-ABS-01)."""
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            fingerprint="8" * 64,
        )
        session.add(transaction)
        session.commit()
        transaction_id = transaction.id
        assert transaction.checked_at is None
        assert transaction.reconciled_at is None

    with factory() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        assert transaction.checked is False
        assert transaction.checked_at is None
        assert transaction.reconciled is False
        assert transaction.reconciled_at is None


def test_transaction_checked_reconciled_timestamps_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """checked_at und reconciled_at werden als aware UTC gespeichert (F-ABS-01, N-DB-02).

    Schreibt einen aware Wert in UTC+2, lädt neu und prüft, dass er aware UTC zurückkommt
    mit gleichem Zeitpunkt.
    """
    with factory() as session:
        account = Account(
            name="Testkonto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    # Schreibe aware Wert in UTC+2
    utc_plus_2 = timezone(timedelta(hours=2))
    aware_time_plus_2 = datetime(2024, 7, 15, 10, 30, 0, tzinfo=utc_plus_2)

    with factory() as session:
        transaction = make_test_transaction(
            account_id=account_id,
            fingerprint="9" * 64,
        )
        transaction.checked = True
        transaction.checked_at = aware_time_plus_2
        transaction.reconciled = True
        transaction.reconciled_at = aware_time_plus_2
        session.add(transaction)
        session.commit()
        transaction_id = transaction.id

    # Neu laden und prüfen
    with factory() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        assert transaction.checked_at is not None
        assert transaction.reconciled_at is not None

        # Prüfe, dass es aware ist und UTC
        assert transaction.checked_at.tzinfo is not None
        assert transaction.checked_at.utcoffset() == timedelta(0)
        assert transaction.reconciled_at.tzinfo is not None
        assert transaction.reconciled_at.utcoffset() == timedelta(0)

        # Prüfe gleicher Zeitpunkt (aware_time_plus_2 in UTC ist 08:30)
        expected_utc = datetime(2024, 7, 15, 8, 30, 0, tzinfo=timezone.utc)
        assert transaction.checked_at == expected_utc
        assert transaction.reconciled_at == expected_utc
