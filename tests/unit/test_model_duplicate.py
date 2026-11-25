"""Tests für das Dubletten-Entscheidungs-Modell.

Referenziert N-DB-01 (kein rohes SQL), F-DUP-03 (Verdachtsdubletten),
F-DUP-05 (Auflösung: Zusammenführen oder Ignorieren) und F-CON-01
(Versionsspalte für optimistische Sperre).
"""

import subprocess
import sys
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import Bank, BankAccess  # noqa: F401
from finue.models.duplicate import DuplicateDecision
from finue.models.transaction import Transaction


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


def make_test_transaction(
    account_id: int,
    amount_cents: int = 10000,
    booking_date: date | None = None,
    fingerprint: str = "a" * 64,
    occurrence_index: int = 1,
) -> Transaction:
    """Hilfsfunktion zum Erstellen von Test-Buchungen."""
    if booking_date is None:
        booking_date = date(2024, 1, 15)
    return Transaction(
        account_id=account_id,
        amount_cents=amount_cents,
        currency="EUR",
        booking_date=booking_date,
        origin="fints",
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
    )


def test_duplicate_decision_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur
    finue.models.duplicate importiert wird, ohne Zirkelimport-Fehler.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.duplicate
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


def test_create_duplicate_decision_with_partner(factory) -> None:  # type: ignore[no-untyped-def]
    """DuplicateDecision kann mit Partner erstellt werden (F-DUP-05)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        # Zwei Buchungen erstellen
        t1 = make_test_transaction(
            account_id=account_id,
            fingerprint="a" * 64,
            occurrence_index=1,
        )
        t2 = make_test_transaction(
            account_id=account_id,
            fingerprint="a" * 64,
            occurrence_index=2,
        )
        session.add(t1)
        session.add(t2)
        session.commit()
        t1_id = t1.id
        t2_id = t2.id

    with factory() as session:
        # DuplicateDecision mit Partner erstellen
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t2_id,
            decision="zusammengefuehrt",
            source="ui",
        )
        session.add(decision)
        session.commit()

        # Verifyication: Entscheidung ist gespeichert
        retrieved = session.query(DuplicateDecision).filter_by(id=decision.id).one()
        assert retrieved.transaction_id == t1_id
        assert retrieved.other_transaction_id == t2_id
        assert retrieved.decision == "zusammengefuehrt"
        assert retrieved.source == "ui"
        assert retrieved.version == 1


def test_create_duplicate_decision_without_partner(factory) -> None:  # type: ignore[no-untyped-def]
    """DuplicateDecision kann ohne Partner erstellt werden (other_transaction_id nullable)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(
            account_id=account_id,
            fingerprint="b" * 64,
        )
        session.add(t1)
        session.commit()
        t1_id = t1.id

    with factory() as session:
        # DuplicateDecision ohne Partner erstellen
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=None,
            decision="behalten",
            source="ui",
        )
        session.add(decision)
        session.commit()

        # Verification: Entscheidung ist gespeichert
        retrieved = session.query(DuplicateDecision).filter_by(id=decision.id).one()
        assert retrieved.transaction_id == t1_id
        assert retrieved.other_transaction_id is None
        assert retrieved.decision == "behalten"


def test_duplicate_decision_loads_transactions(factory) -> None:  # type: ignore[no-untyped-def]
    """Relationen laden die richtigen Buchungen (F-DUP-05)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(
            account_id=account_id,
            amount_cents=10000,
            fingerprint="c" * 64,
        )
        t2 = make_test_transaction(
            account_id=account_id,
            amount_cents=10000,
            fingerprint="c" * 64,
            occurrence_index=2,
        )
        session.add(t1)
        session.add(t2)
        session.commit()
        t1_id = t1.id
        t2_id = t2.id

    with factory() as session:
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t2_id,
            decision="verworfen",
            source="ui",
        )
        session.add(decision)
        session.commit()
        decision_id = decision.id

    with factory() as session:
        retrieved = session.query(DuplicateDecision).filter_by(id=decision_id).one()
        # Relationen sollten die richtigen Buchungen laden
        assert retrieved.transaction.id == t1_id
        assert retrieved.transaction.amount_cents == 10000
        assert retrieved.other_transaction is not None
        assert retrieved.other_transaction.id == t2_id
        assert retrieved.other_transaction.amount_cents == 10000


def test_duplicate_decision_rejects_invalid_decision(factory) -> None:  # type: ignore[no-untyped-def]
    """decision-Check lehnt ungültige Werte ab."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(account_id=account_id)
        session.add(t1)
        session.commit()
        t1_id = t1.id

    with factory() as session:
        # Ungültige Entscheidung erstellen
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=None,
            decision="xyz",  # Ungültig
            source="ui",
        )
        session.add(decision)
        with pytest.raises(IntegrityError):
            session.commit()


def test_duplicate_decision_rejects_self_reference(factory) -> None:  # type: ignore[no-untyped-def]
    """Selbstbezug (transaction_id == other_transaction_id) wird abgelehnt."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(account_id=account_id)
        session.add(t1)
        session.commit()
        t1_id = t1.id

    with factory() as session:
        # Selbstbezug erstellen
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t1_id,  # Gleich wie transaction_id
            decision="zusammengefuehrt",
            source="ui",
        )
        session.add(decision)
        with pytest.raises(IntegrityError):
            session.commit()


def test_duplicate_decision_rejects_duplicate_pair(factory) -> None:  # type: ignore[no-untyped-def]
    """Doppeltes Paar wird abgelehnt (UniqueConstraint)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(account_id=account_id, fingerprint="d" * 64)
        t2 = make_test_transaction(
            account_id=account_id,
            fingerprint="d" * 64,
            occurrence_index=2,
        )
        session.add(t1)
        session.add(t2)
        session.commit()
        t1_id = t1.id
        t2_id = t2.id

    with factory() as session:
        # Erste Entscheidung erstellen
        decision1 = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t2_id,
            decision="zusammengefuehrt",
            source="ui",
        )
        session.add(decision1)
        session.commit()

    with factory() as session:
        # Zweite Entscheidung mit gleichem Paar erstellen
        decision2 = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t2_id,
            decision="verworfen",
            source="ui",
        )
        session.add(decision2)
        with pytest.raises(IntegrityError):
            session.commit()


def test_duplicate_decision_rejects_fk_violation(factory) -> None:  # type: ignore[no-untyped-def]
    """FK-Verletzung (unbekannte transaction_id) wird abgelehnt."""
    with factory() as session:
        # Keine Buchung erstellen; transaction_id auf nicht existierende ID setzen
        decision = DuplicateDecision(
            transaction_id=9999,  # Nicht existierende ID
            other_transaction_id=None,
            decision="behalten",
            source="ui",
        )
        session.add(decision)
        with pytest.raises(IntegrityError):
            session.commit()


def test_duplicate_decision_cascade_delete(factory) -> None:  # type: ignore[no-untyped-def]
    """Buchung per Core-Delete löschen entfernt ihre Entscheidungen (CASCADE)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(account_id=account_id, fingerprint="e" * 64)
        t2 = make_test_transaction(
            account_id=account_id,
            fingerprint="e" * 64,
            occurrence_index=2,
        )
        session.add(t1)
        session.add(t2)
        session.commit()
        t1_id = t1.id
        t2_id = t2.id

    with factory() as session:
        # Entscheidung erstellen
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t2_id,
            decision="zusammengefuehrt",
            source="ui",
        )
        session.add(decision)
        session.commit()
        decision_id = decision.id

    with factory() as session:
        # Erste Buchung löschen
        session.execute(delete(Transaction).where(Transaction.id == t1_id))
        session.commit()

    with factory() as session:
        # Entscheidung sollte auch gelöscht sein
        retrieved = (
            session.query(DuplicateDecision)
            .filter_by(id=decision_id)
            .one_or_none()
        )
        assert retrieved is None


def test_duplicate_decision_decided_at_is_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """decided_at ist aware UTC."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(account_id=account_id)
        session.add(t1)
        session.commit()
        t1_id = t1.id

    with factory() as session:
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=None,
            decision="behalten",
            source="ui",
        )
        session.add(decision)
        session.commit()
        decision_id = decision.id

    with factory() as session:
        retrieved = session.query(DuplicateDecision).filter_by(id=decision_id).one()
        assert retrieved.decided_at.tzinfo is not None
        assert retrieved.decided_at.tzinfo == timezone.utc
        assert isinstance(retrieved.decided_at, datetime)


def test_duplicate_decision_version_increments(factory) -> None:  # type: ignore[no-untyped-def]
    """Version steigt bei Update (F-CON-01)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        t1 = make_test_transaction(account_id=account_id, fingerprint="f" * 64)
        t2 = make_test_transaction(
            account_id=account_id,
            fingerprint="f" * 64,
            occurrence_index=2,
        )
        session.add(t1)
        session.add(t2)
        session.commit()
        t1_id = t1.id
        t2_id = t2.id

    with factory() as session:
        decision = DuplicateDecision(
            transaction_id=t1_id,
            other_transaction_id=t2_id,
            decision="zusammengefuehrt",
            source="ui",
        )
        session.add(decision)
        session.commit()
        decision_id = decision.id

    with factory() as session:
        retrieved = session.query(DuplicateDecision).filter_by(id=decision_id).one()
        initial_version = retrieved.version
        assert initial_version == 1

    with factory() as session:
        # Update durchführen
        retrieved = session.query(DuplicateDecision).filter_by(id=decision_id).one()
        retrieved.source = "api"
        session.commit()

    with factory() as session:
        # Version sollte erhöht sein
        retrieved = session.query(DuplicateDecision).filter_by(id=decision_id).one()
        assert retrieved.version == initial_version + 1
