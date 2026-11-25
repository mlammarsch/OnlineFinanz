"""Tests für den Demo-Daten-Generator.

Referenziert N-OPS-01 (Demo-Daten per Befehl für die lokale Entwicklung) und
F-DUP-01/F-DUP-02 (vorläufiger Fingerprint und Vorkommens-Index). Alle Tests
laufen gegen eine In-Memory-SQLite (:memory:).
"""

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.demo import (
    DEMO_ACCOUNT_NAMES,
    DEMO_BANK_BLZ,
    DEMO_COUNTERPARTIES,
    DEMO_IBANS,
    DEMO_TRANSACTION_COUNT,
    clear_demo,
    seed_demo,
)
from finue.models.account import Account, AccountGroup
from finue.models.bank import Bank, BankAccess
from finue.models.standing_order import StandingOrder
from finue.models.transaction import Transaction


def _new_factory() -> "sessionmaker[Session]":
    """Erstellt eine In-Memory-SQLite mit aktivierten Foreign Keys."""
    engine = create_db_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_seed_demo_creates_expected_counts() -> None:
    """seed_demo erzeugt 1 Bank, 3 Konten, 1 Gruppe, 200 Buchungen, 2 Aufträge (N-OPS-01)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)

    with factory() as session:
        assert session.query(Bank).count() == 1
        assert session.query(BankAccess).count() == 1
        assert session.query(AccountGroup).count() == 1
        assert session.query(Account).count() == 3
        assert session.query(Transaction).count() == DEMO_TRANSACTION_COUNT
        assert session.query(StandingOrder).count() == 2


def test_seed_demo_has_file_account_without_bank_access() -> None:
    """Genau ein Konto ist ein Datei-Konto (source 'datei') ohne Bankzugang (N-OPS-01)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)

    with factory() as session:
        accounts = session.query(Account).all()
        datei_accounts = [a for a in accounts if a.source == "datei"]
        assert len(datei_accounts) == 1
        assert datei_accounts[0].bank_access_id is None

        fints_accounts = [a for a in accounts if a.source == "fints"]
        assert len(fints_accounts) == 2
        assert all(a.bank_access_id is not None for a in fints_accounts)


def test_seed_demo_has_pending_checked_and_reconciled() -> None:
    """Es gibt vorgemerkte sowie geprüfte und abgestimmte Buchungen (F-ABS-01)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)

    with factory() as session:
        pending = session.query(Transaction).filter_by(status="vorgemerkt").all()
        checked = session.query(Transaction).filter_by(checked=True).all()
        reconciled = session.query(Transaction).filter_by(reconciled=True).all()

        assert len(pending) >= 1
        assert len(checked) >= 1
        assert len(reconciled) >= 1
        assert all(t.checked_at is not None for t in checked)
        assert all(t.reconciled_at is not None for t in reconciled)


def test_seed_demo_has_identical_duplicate_pair() -> None:
    """Ein Paar identischer Buchungen am selben Tag mit occurrence_index 0 und 1 (F-DUP-02)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)

    with factory() as session:
        first = session.query(Transaction).filter_by(occurrence_index=0).one()
        assert first.status == "gebucht"
        sibling = (
            session.query(Transaction)
            .filter_by(
                account_id=first.account_id,
                fingerprint=first.fingerprint,
                occurrence_index=1,
            )
            .one()
        )
        assert sibling.booking_date == first.booking_date
        assert sibling.amount_cents == first.amount_cents
        assert sibling.counterparty == first.counterparty
        assert sibling.purpose == first.purpose


def test_seed_demo_has_both_standing_order_kinds() -> None:
    """Beide Auftragsarten (dauerauftrag, terminauftrag) sind vertreten (F-AUF-02)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)

    with factory() as session:
        kinds = {order.kind for order in session.query(StandingOrder).all()}
        assert kinds == {"dauerauftrag", "terminauftrag"}


def _signature(factory: "sessionmaker[Session]") -> list[tuple[int, object, object, object, str, int]]:
    """Bildet den Buchungsbestand auf eine vergleichbare Signatur ab."""
    with factory() as session:
        rows = session.query(Transaction).order_by(Transaction.id).all()
        return [
            (t.amount_cents, t.booking_date, t.purpose, t.counterparty, t.origin, t.occurrence_index)
            for t in rows
        ]


def test_seed_demo_is_deterministic_for_same_seed() -> None:
    """Gleicher Seed erzeugt denselben Buchungsbestand (Reproduzierbarkeit, N-OPS-01)."""
    factory_first = _new_factory()
    with factory_first() as session:
        seed_demo(session, seed=42)

    factory_second = _new_factory()
    with factory_second() as session:
        seed_demo(session, seed=42)

    assert _signature(factory_first) == _signature(factory_second)


def test_clear_demo_removes_all_demo_data() -> None:
    """clear_demo entfernt Bank, Zugang, Gruppe, Konten, Buchungen und Aufträge (N-OPS-01)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)
        clear_demo(session)

    with factory() as session:
        assert session.query(Bank).count() == 0
        assert session.query(BankAccess).count() == 0
        assert session.query(AccountGroup).count() == 0
        assert session.query(Account).count() == 0
        assert session.query(Transaction).count() == 0
        assert session.query(StandingOrder).count() == 0


def test_clear_demo_makes_seed_repeatable() -> None:
    """seed_demo -> clear_demo -> seed_demo läuft ohne Eindeutigkeitsfehler (N-OPS-01)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)
        clear_demo(session)
        seed_demo(session)

    with factory() as session:
        assert session.query(Account).count() == 3
        assert session.query(Transaction).count() == DEMO_TRANSACTION_COUNT
        assert session.query(StandingOrder).count() == 2
        assert session.query(Bank).count() == 1


def test_seed_demo_uses_only_fictitious_data() -> None:
    """Nur fiktive Gegenparteien, Test-IBANs und Test-Bankleitzahl (keine echten Daten)."""
    factory = _new_factory()
    with factory() as session:
        seed_demo(session)

    with factory() as session:
        bank = session.query(Bank).one()
        assert bank.blz == DEMO_BANK_BLZ

        for account in session.query(Account).all():
            assert account.name in DEMO_ACCOUNT_NAMES
            if account.iban is not None:
                assert account.iban in DEMO_IBANS

        for transaction in session.query(Transaction).all():
            if transaction.counterparty is not None:
                assert transaction.counterparty in DEMO_COUNTERPARTIES
            if transaction.counterparty_iban is not None:
                assert transaction.counterparty_iban in DEMO_IBANS
