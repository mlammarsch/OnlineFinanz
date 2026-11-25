"""Tests für das Modell der Termin- und Daueraufträge.

Referenziert N-DB-01 (kein rohes SQL), F-AUF-02 (Anzeigefelder), F-AUF-04
(Abruf beim Sync, Bestand je Konto) und F-CON-01 (Versionsspalte für
optimistische Sperre).
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
from finue.models.standing_order import StandingOrder


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


def make_dauerauftrag(account_id: int) -> StandingOrder:
    """Hilfsfunktion zum Erstellen eines Dauerauftrags."""
    return StandingOrder(
        account_id=account_id,
        kind="dauerauftrag",
        counterparty="Vermieter GmbH",
        counterparty_iban="DE02120300000000202051",
        purpose="Miete",
        amount_cents=85000,
        currency="EUR",
        interval="monatlich",
        next_execution=date(2024, 2, 1),
    )


def make_terminauftrag(account_id: int) -> StandingOrder:
    """Hilfsfunktion zum Erstellen eines Terminauftrags."""
    return StandingOrder(
        account_id=account_id,
        kind="terminauftrag",
        counterparty="Stadtwerke",
        counterparty_iban="DE02500105170137075030",
        purpose="Abschlag Strom",
        amount_cents=12000,
        currency="EUR",
        interval=None,
        next_execution=date(2024, 3, 15),
    )


def test_standing_order_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur
    finue.models.standing_order importiert wird, ohne Zirkelimport-Fehler.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.standing_order
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


def test_create_dauerauftrag(factory) -> None:  # type: ignore[no-untyped-def]
    """Dauerauftrag kann angelegt und wieder gelesen werden (F-AUF-02)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_dauerauftrag(account_id)
        session.add(order)
        session.commit()
        order_id = order.id

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one()
        assert retrieved.account_id == account_id
        assert retrieved.kind == "dauerauftrag"
        assert retrieved.counterparty == "Vermieter GmbH"
        assert retrieved.counterparty_iban == "DE02120300000000202051"
        assert retrieved.purpose == "Miete"
        assert retrieved.amount_cents == 85000
        assert retrieved.currency == "EUR"
        assert retrieved.interval == "monatlich"
        assert retrieved.next_execution == date(2024, 2, 1)
        assert retrieved.version == 1


def test_create_terminauftrag(factory) -> None:  # type: ignore[no-untyped-def]
    """Terminauftrag kann angelegt und wieder gelesen werden (F-AUF-02)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_terminauftrag(account_id)
        session.add(order)
        session.commit()
        order_id = order.id

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one()
        assert retrieved.account_id == account_id
        assert retrieved.kind == "terminauftrag"
        assert retrieved.counterparty == "Stadtwerke"
        assert retrieved.purpose == "Abschlag Strom"
        assert retrieved.amount_cents == 12000
        assert retrieved.currency == "EUR"
        assert retrieved.interval is None
        assert retrieved.next_execution == date(2024, 3, 15)


def test_standing_order_loads_account(factory) -> None:  # type: ignore[no-untyped-def]
    """Relation lädt das richtige Konto (nur lesende Abbildung)."""
    with factory() as session:
        account = make_test_account(name="Girokonto")
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_dauerauftrag(account_id)
        session.add(order)
        session.commit()
        order_id = order.id

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one()
        assert retrieved.account.id == account_id
        assert retrieved.account.name == "Girokonto"


def test_standing_order_rejects_invalid_kind(factory) -> None:  # type: ignore[no-untyped-def]
    """kind-Check lehnt ungültige Werte ab (F-AUF-02)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_dauerauftrag(account_id)
        order.kind = "ueberweisung"  # Nicht erlaubt
        session.add(order)
        with pytest.raises(IntegrityError):
            session.commit()


def test_standing_order_rejects_fk_violation(factory) -> None:  # type: ignore[no-untyped-def]
    """FK-Verletzung (unbekannte account_id) wird abgelehnt."""
    with factory() as session:
        order = make_dauerauftrag(9999)  # Nicht existierende Konto-ID
        session.add(order)
        with pytest.raises(IntegrityError):
            session.commit()


def test_standing_order_cascade_delete(factory) -> None:  # type: ignore[no-untyped-def]
    """Konto löschen entfernt seine Aufträge (CASCADE, F-AUF-04)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_dauerauftrag(account_id)
        session.add(order)
        session.commit()
        order_id = order.id

    with factory() as session:
        session.execute(delete(Account).where(Account.id == account_id))
        session.commit()

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one_or_none()
        assert retrieved is None


def test_standing_order_last_seen_at_is_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """last_seen_at ist aware UTC (N-DB-02, F-AUF-04)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_dauerauftrag(account_id)
        session.add(order)
        session.commit()
        order_id = order.id

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one()
        assert isinstance(retrieved.last_seen_at, datetime)
        assert retrieved.last_seen_at.tzinfo is not None
        assert retrieved.last_seen_at.tzinfo == timezone.utc


def test_standing_order_version_increments(factory) -> None:  # type: ignore[no-untyped-def]
    """Version steigt bei Update (F-CON-01)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        order = make_dauerauftrag(account_id)
        session.add(order)
        session.commit()
        order_id = order.id
        initial_version = order.version
        assert initial_version == 1

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one()
        retrieved.amount_cents = 86000
        session.commit()

    with factory() as session:
        retrieved = session.query(StandingOrder).filter_by(id=order_id).one()
        assert retrieved.version == initial_version + 1
