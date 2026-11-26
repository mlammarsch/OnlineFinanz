"""Tests fuer die Erkennung von Verdachtsdubletten.

Referenziert F-DUP-03 (gleiches Konto, gleicher Betrag, Buchungstag +/- 2 Tage,
abweichender normalisierter Text; nur Kandidaten, kein Loeschen, keine
automatische Zusammenfuehrung) und F-DUP-05 (ein als ``verworfen``
entschiedenes Paar wird nicht erneut vorgeschlagen, Entscheidungen legt erst
die UI an). F-DUP-02 grenzt den Fall mit identischem Text ab: Das ist eine
echte Dublette und keine Verdachtsdublette.
"""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.duplicate import DuplicateDecision
from finue.models.transaction import Transaction
from finue.services.duplicate import (
    SuspectedPair,
    TransactionData,
    compute_fingerprint,
    find_suspected_duplicates,
)


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def create_account(session: Session, name: str = "Testkonto") -> Account:
    """Legt ein Testkonto an und gibt es mit gesetzter ID zurueck."""
    account = Account(name=name, currency="EUR", source="fints")
    session.add(account)
    session.commit()
    return account


def make_incoming(
    *,
    booking_date: date = date(2026, 1, 15),
    value_date: date | None = None,
    amount_cents: int = -4883,
    currency: str = "EUR",
    counterparty: str | None = "Max Mustermann",
    counterparty_iban: str | None = "DE89370400440532013000",
    purpose: str | None = "SEPA Ueberweisung",
    origin: str = "csv",
) -> TransactionData:
    """Erzeugt eine eingehende Lieferzeile mit F-DUP-01-Standardfeldern."""
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


def add_stored(
    session: Session,
    account_id: int,
    *,
    booking_date: date = date(2026, 1, 15),
    value_date: date | None = None,
    amount_cents: int = -4883,
    currency: str = "EUR",
    counterparty: str | None = "Max Mustermann",
    counterparty_iban: str | None = "DE89370400440532013000",
    purpose: str | None = "SEPA Ueberweisung",
    origin: str = "fints",
    occurrence_index: int = 0,
    merged: bool = False,
) -> Transaction:
    """Legt eine gespeicherte Buchung mit passendem Fingerprint an."""
    fingerprint = compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
    )
    transaction = Transaction(
        account_id=account_id,
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        origin=origin,
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
        merged=merged,
    )
    session.add(transaction)
    session.commit()
    return transaction


def count_rows(session: Session, model: type[Transaction] | type[DuplicateDecision]) -> int:
    """Zaehlt Zeilen eines Modells fuer die Read-only-Nachweise."""
    return session.scalar(select(func.count()).select_from(model)) or 0


class TestFindSuspectedDuplicates:
    """Kriterien aus F-DUP-03."""

    def test_gleicher_betrag_tag_plus_eins_und_abweichender_text_ist_treffer(
        self, factory: sessionmaker[Session]
    ) -> None:
        """F-DUP-03: Tag +1, gleicher Betrag und geaenderter Text sind verdaechtig."""
        with factory() as session:
            account = create_account(session)
            existing = add_stored(session, account.id)
            incoming = make_incoming(booking_date=date(2026, 1, 16), purpose="SEPA Ueberweisung geaendert")

            pairs = find_suspected_duplicates(account.id, [incoming], session)

            assert len(pairs) == 1
            assert isinstance(pairs[0], SuspectedPair)
            assert pairs[0].new_transaction is incoming
            assert pairs[0].existing_transaction.id == existing.id

    def test_gleicher_betrag_tag_plus_drei_ist_kein_treffer(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Das Tagesfenster endet nach zwei Tagen."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            incoming = make_incoming(booking_date=date(2026, 1, 18), purpose="SEPA Ueberweisung geaendert")

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_gleicher_betrag_tag_plus_zwei_ist_noch_treffer(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: +/- 2 Tage sind einschliesslich gemeint."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            incoming = make_incoming(booking_date=date(2026, 1, 17), purpose="SEPA Ueberweisung geaendert")

            assert len(find_suspected_duplicates(account.id, [incoming], session)) == 1

    def test_identischer_normalisierter_text_ist_kein_verdachtsfall(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Identischer Text ist eine echte Dublette, kein Verdacht."""
        with factory() as session:
            account = create_account(session)
            add_stored(
                session,
                account.id,
                counterparty="Max Mustermann",
                purpose="SEPA Ueberweisung",
            )
            incoming = make_incoming(
                booking_date=date(2026, 1, 16),
                counterparty="  max   mustermann ",
                purpose="sepa ueberweisung",
            )

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_abweichende_gegenpartei_ist_treffer(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Bereits eine abweichende Gegenpartei reicht aus."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            incoming = make_incoming(booking_date=date(2026, 1, 15), counterparty="Erika Musterfrau")

            pairs = find_suspected_duplicates(account.id, [incoming], session)
            assert len(pairs) == 1

    def test_abweichender_verwendungszweck_ist_treffer(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Bereits ein abweichender Verwendungszweck reicht aus."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            incoming = make_incoming(booking_date=date(2026, 1, 15), purpose="Anderer Zweck")

            pairs = find_suspected_duplicates(account.id, [incoming], session)
            assert len(pairs) == 1

    def test_abweichender_betrag_ist_kein_treffer(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Der Betrag muss exakt gleich sein."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            incoming = make_incoming(
                booking_date=date(2026, 1, 16),
                amount_cents=-4884,
                purpose="SEPA Ueberweisung geaendert",
            )

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_anderes_konto_ist_kein_treffer(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Es wird nur gegen dasselbe Konto geprueft."""
        with factory() as session:
            account = create_account(session, name="Konto A")
            other = create_account(session, name="Konto B")
            add_stored(session, other.id)
            incoming = make_incoming(booking_date=date(2026, 1, 16), purpose="SEPA Ueberweisung geaendert")

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_merged_buchung_wird_ignoriert(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-05: Zusammengefuehrte Buchungen sind unsichtbar und kein Kandidat."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id, merged=True)
            incoming = make_incoming(booking_date=date(2026, 1, 16), purpose="SEPA Ueberweisung geaendert")

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_verworfenes_paar_wird_nicht_erneut_vorgeschlagen(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-05: ``verworfen`` unterdrueckt das Paar auch fuer eine neue Zeile."""
        with factory() as session:
            account = create_account(session)
            first = add_stored(session, account.id, purpose="SEPA Ueberweisung")
            second = add_stored(
                session,
                account.id,
                occurrence_index=1,
                purpose="SEPA Ueberweisung geaendert",
            )
            incoming = make_incoming(booking_date=date(2026, 1, 15), purpose="SEPA Ueberweisung geaendert")

            before = find_suspected_duplicates(account.id, [incoming], session)
            assert len(before) == 1
            assert before[0].existing_transaction.id == first.id

            session.add(
                DuplicateDecision(
                    transaction_id=first.id,
                    other_transaction_id=second.id,
                    decision="verworfen",
                    source="ui",
                )
            )
            session.commit()

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_verworfenes_paar_wirkt_in_beiden_richtungen(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-05: Die Paar-Richtung der Entscheidung ist fuer den Verdacht egal."""
        with factory() as session:
            account = create_account(session)
            first = add_stored(session, account.id, purpose="SEPA Ueberweisung")
            second = add_stored(
                session,
                account.id,
                occurrence_index=1,
                purpose="SEPA Ueberweisung geaendert",
            )
            session.add(
                DuplicateDecision(
                    transaction_id=second.id,
                    other_transaction_id=first.id,
                    decision="verworfen",
                    source="ui",
                )
            )
            session.commit()

            incoming = make_incoming(booking_date=date(2026, 1, 15), purpose="SEPA Ueberweisung")

            assert find_suspected_duplicates(account.id, [incoming], session) == []

    def test_nur_verworfene_nicht_behaltene_entscheidungen_unterdruecken(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-05: ``behalten`` ist keine Verwerfung und unterdrueckt nicht."""
        with factory() as session:
            account = create_account(session)
            first = add_stored(session, account.id, purpose="SEPA Ueberweisung")
            second = add_stored(
                session,
                account.id,
                occurrence_index=1,
                purpose="SEPA Ueberweisung geaendert",
            )
            session.add(
                DuplicateDecision(
                    transaction_id=first.id,
                    other_transaction_id=second.id,
                    decision="behalten",
                    source="ui",
                )
            )
            session.commit()

            incoming = make_incoming(booking_date=date(2026, 1, 16), purpose="SEPA Ueberweisung geaendert")

            assert len(find_suspected_duplicates(account.id, [incoming], session)) == 1

    def test_funktion_schreibt_nichts_und_erzeugt_keine_entscheidung(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Nur Kandidatenliste, kein Loeschen, kein Zusammenfuehren, keine Entscheidung."""
        with factory() as session:
            account = create_account(session)
            existing = add_stored(session, account.id)
            incoming = make_incoming(booking_date=date(2026, 1, 16), purpose="SEPA Ueberweisung geaendert")

            transactions_before = count_rows(session, Transaction)
            decisions_before = count_rows(session, DuplicateDecision)

            pairs = find_suspected_duplicates(account.id, [incoming], session)

            assert len(pairs) == 1
            assert count_rows(session, Transaction) == transactions_before
            assert count_rows(session, DuplicateDecision) == decisions_before == 0
            assert session.get(Transaction, existing.id) is existing
            assert existing.merged is False

    def test_leere_lieferung_ergibt_leere_liste(self, factory: sessionmaker[Session]) -> None:
        """Eine leere Lieferung ist ein No-op."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)

            assert find_suspected_duplicates(account.id, [], session) == []

    def test_mehrere_lieferzeilen_liefern_je_treffer_ein_paar(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Jede verdaechtige Lieferzeile erscheint einmal je Bestandstreffer."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id, purpose="SEPA Ueberweisung")
            first = make_incoming(booking_date=date(2026, 1, 15), purpose="Zweck A")
            second = make_incoming(booking_date=date(2026, 1, 16), purpose="Zweck B")

            pairs = find_suspected_duplicates(account.id, [first, second], session)

            assert len(pairs) == 2
            assert pairs[0].new_transaction is first
            assert pairs[1].new_transaction is second
