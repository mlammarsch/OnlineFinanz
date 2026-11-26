"""Tests fuer den Multimengen-Abgleich der Dublettenkennung.

Referenziert F-DUP-02 (Multimenge: ``max(0, n_neu - n_db)`` neue Buchungen mit
laufendem Vorkommens-Index, Eindeutigkeit ueber Konto/Fingerprint/Index) und
F-DUP-05 (zusammengefuehrte Buchungen behalten Fingerprint und Index). Die
Verdachtsdubletten-Erkennung aus F-DUP-03 ist bewusst nicht Teil dieser Datei.
"""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.transaction import Transaction
from finue.services.duplicate import (
    ReconcileResult,
    TransactionData,
    compute_fingerprint,
    reconcile_multiset,
)


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def make_incoming(
    *,
    booking_date: date = date(2026, 1, 15),
    value_date: date | None = date(2026, 1, 16),
    amount_cents: int = -4883,
    currency: str = "EUR",
    counterparty: str | None = "Max Mustermann",
    counterparty_iban: str | None = "DE89370400440532013000",
    purpose: str | None = "SEPA Ueberweisung",
    origin: str = "fints",
) -> TransactionData:
    """Erzeugt eine eingehende Buchung mit F-DUP-01-Standardfeldern."""
    return TransactionData(
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        origin=origin,
    )


def expected_fingerprint(account_id: int, item: TransactionData) -> str:
    """Berechnet den erwarteten Fingerprint einer Testbuchung."""
    return compute_fingerprint(
        account_id=account_id,
        booking_date=item.booking_date,
        value_date=item.value_date,
        amount_cents=item.amount_cents,
        currency=item.currency,
        counterparty=item.counterparty,
        counterparty_iban=item.counterparty_iban,
        purpose=item.purpose,
    )


def create_account(session: Session) -> Account:
    """Legt ein Testkonto an und gibt es mit gesetzter ID zurueck."""
    account = Account(name="Testkonto", currency="EUR", source="fints")
    session.add(account)
    session.commit()
    return account


def stored_transactions(session: Session, account_id: int) -> list[Transaction]:
    """Liest alle Buchungen eines Kontos, sortiert nach Index und ID."""
    statement = (
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.occurrence_index, Transaction.id)
    )
    return list(session.scalars(statement).all())


class TestReconcileMultiset:
    """Szenarien aus F-DUP-02."""

    def test_zwei_echte_gleiche_buchungen_werden_beide_uebernommen(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Zwei echte gleiche Buchungen am selben Tag bleiben beide."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            result = reconcile_multiset(account.id, [incoming, incoming], session)

            assert isinstance(result, ReconcileResult)
            assert result.inserted_count == 2
            assert result.skipped_count == 0
            assert result.warnings == []
            assert sorted(item.occurrence_index for item in result.to_insert) == [0, 1]

            session.commit()
            stored = stored_transactions(session, account.id)
            assert len(stored) == 2
            assert sorted(item.occurrence_index for item in stored) == [0, 1]
            assert {item.fingerprint for item in stored} == {expected_fingerprint(account.id, incoming)}

    def test_identischer_reimport_erzeugt_null_neue_buchungen(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Wiederholter Import derselben Daten legt nichts neu an."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            first = reconcile_multiset(account.id, [incoming, incoming], session)
            session.commit()
            assert first.inserted_count == 2

            second = reconcile_multiset(account.id, [incoming, incoming], session)
            assert second.to_insert == []
            assert second.inserted_count == 0
            assert second.skipped_count == 2
            assert second.warnings == []

            session.commit()
            assert len(stored_transactions(session, account.id)) == 2

    def test_spaeter_hinzukommende_gleiche_buchung_wird_genau_einmal_ergaenzt(
        self, factory: sessionmaker[Session]
    ) -> None:
        """F-DUP-02: Eine spaeter zusaetzlich gelieferte gleiche Buchung wird ergaenzt."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            reconcile_multiset(account.id, [incoming], session)
            session.commit()

            result = reconcile_multiset(account.id, [incoming, incoming], session)
            assert result.inserted_count == 1
            assert result.skipped_count == 1
            assert result.warnings == []
            assert result.to_insert[0].occurrence_index == 1

            session.commit()
            stored = stored_transactions(session, account.id)
            assert len(stored) == 2
            assert sorted(item.occurrence_index for item in stored) == [0, 1]

    def test_bank_liefert_weniger_nichts_geloescht_mit_warnung(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Weniger gelieferte Buchungen loeschen nichts und warnen."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            reconcile_multiset(account.id, [incoming, incoming], session)
            session.commit()
            before_ids = {item.id for item in stored_transactions(session, account.id)}

            result = reconcile_multiset(account.id, [incoming], session)
            assert result.to_insert == []
            assert result.skipped_count == 1
            assert len(result.warnings) == 1
            assert "nichts geloescht" in result.warnings[0].lower()
            assert "2" in result.warnings[0]
            assert "1" in result.warnings[0]

            session.commit()
            after = stored_transactions(session, account.id)
            assert {item.id for item in after} == before_ids
            assert len(after) == 2

    def test_merged_buchung_zaehlt_weiter_mit(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-05: Eine zusammengefuehrte Buchung belegt ihren Fingerprint weiter."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            reconcile_multiset(account.id, [incoming], session)
            session.commit()

            existing = stored_transactions(session, account.id)[0]
            existing.merged = True
            session.commit()

            result = reconcile_multiset(account.id, [incoming], session)
            assert result.inserted_count == 0
            assert result.skipped_count == 1

            session.commit()
            assert len(stored_transactions(session, account.id)) == 1

    def test_merged_buchung_erzwingt_bei_gleicher_lieferung_genau_eine_neue(
        self, factory: sessionmaker[Session]
    ) -> None:
        """F-DUP-05: Der belegte Index fuehrt zum naechsten freien Vorkommen."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            reconcile_multiset(account.id, [incoming], session)
            session.commit()

            existing = stored_transactions(session, account.id)[0]
            existing.merged = True
            session.commit()

            result = reconcile_multiset(account.id, [incoming, incoming], session)
            assert result.inserted_count == 1
            assert result.skipped_count == 1
            assert result.to_insert[0].occurrence_index == 1

            session.commit()
            stored = stored_transactions(session, account.id)
            assert len(stored) == 2
            assert sum(1 for item in stored if item.merged) == 1

    def test_verschiedene_fingerprints_werden_getrennt_gezaehlt(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Der Abgleich laeuft je Fingerprint, nicht global."""
        first = make_incoming()
        second = make_incoming(purpose="Anderer Zweck")
        with factory() as session:
            account = create_account(session)
            result = reconcile_multiset(account.id, [first, second], session)

            assert result.inserted_count == 2
            assert result.skipped_count == 0
            assert [item.occurrence_index for item in result.to_insert] == [0, 0]

            session.commit()
            stored = stored_transactions(session, account.id)
            assert len(stored) == 2
            assert len({item.fingerprint for item in stored}) == 2

    def test_leere_lieferung_aendert_nichts(self, factory: sessionmaker[Session]) -> None:
        """Eine leere Lieferung ist ein No-op."""
        with factory() as session:
            account = create_account(session)
            result = reconcile_multiset(account.id, [], session)

            assert result.inserted_count == 0
            assert result.skipped_count == 0
            assert result.warnings == []
            assert stored_transactions(session, account.id) == []

    def test_index_setzt_hinter_dem_hoechsten_vorhandenen_index_fort(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Fortlaufender Index kollidiert nicht mit vorhandenen Werten."""
        incoming = make_incoming()
        with factory() as session:
            account = create_account(session)
            fingerprint = expected_fingerprint(account.id, incoming)
            session.add(
                Transaction(
                    account_id=account.id,
                    booking_date=incoming.booking_date,
                    amount_cents=incoming.amount_cents,
                    currency=incoming.currency,
                    origin="fints",
                    fingerprint=fingerprint,
                    occurrence_index=4,
                )
            )
            session.commit()

            result = reconcile_multiset(account.id, [incoming, incoming], session)
            assert result.inserted_count == 1
            assert result.to_insert[0].occurrence_index == 5

            session.commit()
            assert sorted(item.occurrence_index for item in stored_transactions(session, account.id)) == [4, 5]

    def test_lieferdaten_werden_uebernommen(self, factory: sessionmaker[Session]) -> None:
        """Die Felder der Lieferzeile landen unveraendert in der neuen Buchung."""
        incoming = make_incoming(origin="csv")
        with factory() as session:
            account = create_account(session)
            result = reconcile_multiset(account.id, [incoming], session)

            assert result.inserted_count == 1
            inserted = result.to_insert[0]
            assert inserted.account_id == account.id
            assert inserted.booking_date == incoming.booking_date
            assert inserted.value_date == incoming.value_date
            assert inserted.amount_cents == incoming.amount_cents
            assert inserted.currency == incoming.currency
            assert inserted.counterparty == incoming.counterparty
            assert inserted.counterparty_iban == incoming.counterparty_iban
            assert inserted.purpose == incoming.purpose
            assert inserted.origin == "csv"
            assert inserted.merged is False
