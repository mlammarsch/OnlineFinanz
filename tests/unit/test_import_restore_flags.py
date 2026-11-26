"""Tests fuer die Wiederherstellung von Notiz und Kennzeichen beim Re-Import.

Referenziert F-IMP-07 (Standard: nur neue Buchungen uebernehmen; auf Wunsch
Werte auf vorhandene Buchungen uebertragen) und F-ABS-06 (Kennzeichen bleiben
bei jedem Sync und Re-Import erhalten und nehmen nicht am Fingerprint teil).

Abgedeckt:

- Re-Import ohne Wiederherstellung laesst vorhandene Notiz/Kennzeichen/
  externe Referenz und die Version unveraendert,
- Re-Import mit Wiederherstellung uebernimmt die Dateiwerte samt Zeitpunkten,
- Wiederherstellung kann Kennzeichen, Notiz und externe Referenz auch leeren,
- neue Buchungen erhalten die Dateiwerte in beiden Modi,
- mehrere Vorkommen desselben Fingerprints werden in Dateireihenfolge
  zugeordnet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.import_run import execute_import

IBAN = "DE89370400440532013000"

CHECKED_AT = datetime(2026, 2, 10, 9, 30, tzinfo=timezone.utc)
RECONCILED_AT = datetime(2026, 2, 11, 8, 15, tzinfo=timezone.utc)

BOOKING_DATE = date(2026, 2, 10)
VALUE_DATE = date(2026, 2, 11)
AMOUNT_CENTS = -1234
COUNTERPARTY = "Neuer Laden"
PURPOSE = "Einkauf"


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


def fingerprint(account_id: int, *, counterparty: str | None = COUNTERPARTY) -> str:
    """Berechnet den F-DUP-01-Fingerprint der Testbuchung."""
    return compute_fingerprint(
        account_id=account_id,
        booking_date=BOOKING_DATE,
        value_date=VALUE_DATE,
        amount_cents=AMOUNT_CENTS,
        currency="EUR",
        counterparty=counterparty,
        counterparty_iban=None,
        purpose=PURPOSE,
    )


def stored_transaction(
    session: Session,
    account_id: int,
    *,
    note: str | None = None,
    checked: bool = False,
    checked_at: datetime | None = None,
    reconciled: bool = False,
    reconciled_at: datetime | None = None,
    external_ref: str | None = None,
    occurrence_index: int = 0,
) -> Transaction:
    """Legt eine vorhandene Buchung mit passendem Fingerprint an."""
    transaction = Transaction(
        account_id=account_id,
        status="gebucht",
        booking_date=BOOKING_DATE,
        value_date=VALUE_DATE,
        amount_cents=AMOUNT_CENTS,
        currency="EUR",
        counterparty=COUNTERPARTY,
        counterparty_iban=None,
        purpose=PURPOSE,
        note=note,
        checked=checked,
        checked_at=checked_at,
        reconciled=reconciled,
        reconciled_at=reconciled_at,
        origin="fints",
        external_ref=external_ref,
        fingerprint=fingerprint(account_id),
        occurrence_index=occurrence_index,
        merged=False,
    )
    session.add(transaction)
    session.commit()
    return transaction


def iso(value: datetime) -> str:
    """Formatiert einen UTC-Zeitstempel als ISO-String mit ``Z``."""
    return value.isoformat().replace("+00:00", "Z")


def payload_transaction(
    account_id: int,
    *,
    transaction_id: int = 201,
    note: str | None = None,
    checked: bool = False,
    checked_at: datetime | None = None,
    reconciled: bool = False,
    reconciled_at: datetime | None = None,
    external_ref: str | None = None,
) -> dict[str, Any]:
    """Erzeugt eine Buchung im eigenen JSON-Format (Version 1)."""
    return {
        "id": transaction_id,
        "account_id": account_id,
        "status": "gebucht",
        "booking_date": BOOKING_DATE.isoformat(),
        "value_date": VALUE_DATE.isoformat(),
        "counterparty": COUNTERPARTY,
        "counterparty_iban": None,
        "purpose": PURPOSE,
        "note": note,
        "checked": checked,
        "checked_at": iso(checked_at) if checked_at is not None else None,
        "checked_origin": "import" if checked else None,
        "reconciled": reconciled,
        "reconciled_at": iso(reconciled_at) if reconciled_at is not None else None,
        "reconciled_origin": "import" if reconciled else None,
        "origin": "csv",
        "external_ref": external_ref,
        "fingerprint": fingerprint(account_id),
        "occurrence_index": 1,
        "merged": False,
        "amount": "-12.34",
        "currency": "EUR",
    }


def own_json_document(
    account_id: int,
    transactions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Baut ein gueltiges eigenes JSON-Dokument um die Buchungen."""
    return {
        "format": "finanzuebersicht",
        "version": 1,
        "created_at": "2026-11-26T10:41:13Z",
        "filter": {
            "scope": "account",
            "account_ids": [account_id],
            "group_ids": None,
            "transaction_ids": None,
            "search": None,
            "checked": None,
            "reconciled": None,
            "with_note": None,
        },
        "period": {"start": "2026-01-01", "end": "2026-11-25"},
        "accounts": [
            {
                "id": account_id,
                "name": "Testkonto",
                "iban": None,
                "currency": "EUR",
                "account_type": "giro",
            }
        ],
        "transactions": transactions,
    }


def count_transactions(session: Session) -> int:
    """Zaehlt alle Buchungen."""
    return session.scalar(select(func.count()).select_from(Transaction)) or 0


class TestReimportWithoutRestore:
    """F-IMP-07/F-ABS-06: Der Standard laesst vorhandene Werte unveraendert."""

    def test_standard_reimport_laesst_vorhandene_werte_unveraendert(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Ohne Wiederherstellung bleiben Notiz, Kennzeichen und Version gleich."""
        with factory() as session:
            account = create_account(session)
            stored = stored_transaction(
                session,
                account.id,
                note="alte Notiz",
                checked=True,
                checked_at=CHECKED_AT,
                reconciled=False,
                external_ref="alt-ref",
            )
            old_version = stored.version

            payload = own_json_document(
                account.id,
                [
                    payload_transaction(
                        account.id,
                        note="neue Notiz",
                        checked=False,
                        reconciled=True,
                        reconciled_at=RECONCILED_AT,
                        external_ref="neu-ref",
                    )
                ],
            )

            run = execute_import(account.id, payload, None, "json", session)

            assert run.new_count == 0
            assert run.skipped_count == 1
            session.refresh(stored)
            assert stored.note == "alte Notiz"
            assert stored.checked is True
            assert stored.checked_at == CHECKED_AT
            assert stored.reconciled is False
            assert stored.reconciled_at is None
            assert stored.external_ref == "alt-ref"
            assert stored.version == old_version
            assert count_transactions(session) == 1


class TestReimportWithRestore:
    """F-IMP-07: Die Wiederherstellung uebernimmt die Dateiwerte."""

    def test_wiederherstellung_ueberschreibt_vorhandene_werte(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Notiz, Kennzeichen, Zeitpunkte und externe Referenz kommen aus der Datei."""
        with factory() as session:
            account = create_account(session)
            stored = stored_transaction(
                session,
                account.id,
                note="alte Notiz",
                checked=False,
                reconciled=False,
                external_ref="alt-ref",
            )
            old_version = stored.version

            payload = own_json_document(
                account.id,
                [
                    payload_transaction(
                        account.id,
                        note="neue Notiz",
                        checked=True,
                        checked_at=CHECKED_AT,
                        reconciled=True,
                        reconciled_at=RECONCILED_AT,
                        external_ref="neu-ref",
                    )
                ],
            )

            run = execute_import(
                account.id,
                payload,
                None,
                "json",
                session,
                restore_flags_for_existing=True,
            )

            assert run.new_count == 0
            assert run.skipped_count == 1
            session.refresh(stored)
            assert stored.note == "neue Notiz"
            assert stored.checked is True
            assert stored.checked_at == CHECKED_AT
            assert stored.reconciled is True
            assert stored.reconciled_at == RECONCILED_AT
            assert stored.external_ref == "neu-ref"
            assert stored.version > old_version
            assert count_transactions(session) == 1

    def test_wiederherstellung_kann_kennzeichen_und_notiz_leeren(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Dateiwerte False/None setzen vorhandene Kennzeichen und Texte zurueck."""
        with factory() as session:
            account = create_account(session)
            stored = stored_transaction(
                session,
                account.id,
                note="alte Notiz",
                checked=True,
                checked_at=CHECKED_AT,
                reconciled=True,
                reconciled_at=RECONCILED_AT,
                external_ref="alt-ref",
            )

            payload = own_json_document(
                account.id,
                [
                    payload_transaction(
                        account.id,
                        note=None,
                        checked=False,
                        reconciled=False,
                        external_ref=None,
                    )
                ],
            )

            run = execute_import(
                account.id,
                payload,
                None,
                "json",
                session,
                restore_flags_for_existing=True,
            )

            assert run.new_count == 0
            session.refresh(stored)
            assert stored.note is None
            assert stored.checked is False
            assert stored.checked_at is None
            assert stored.reconciled is False
            assert stored.reconciled_at is None
            assert stored.external_ref is None
            assert count_transactions(session) == 1

    def test_wiederherstellung_ordnet_mehrfach_vorkommen_in_dateireihenfolge_zu(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Gleicher Fingerprint: Vorkommen 0/1 erhalten Notiz 0/1 aus der Datei."""
        with factory() as session:
            account = create_account(session)
            first = stored_transaction(
                session,
                account.id,
                note="alt-0",
                occurrence_index=0,
            )
            second = stored_transaction(
                session,
                account.id,
                note="alt-1",
                occurrence_index=1,
            )

            payload = own_json_document(
                account.id,
                [
                    payload_transaction(account.id, transaction_id=201, note="neu-0"),
                    payload_transaction(account.id, transaction_id=202, note="neu-1"),
                ],
            )

            run = execute_import(
                account.id,
                payload,
                None,
                "json",
                session,
                restore_flags_for_existing=True,
            )

            assert run.new_count == 0
            assert run.skipped_count == 2
            session.refresh(first)
            session.refresh(second)
            assert first.note == "neu-0"
            assert second.note == "neu-1"
            assert count_transactions(session) == 2


class TestNewTransactionFlags:
    """F-IMP-07: Neue Buchungen erhalten die Dateiwerte in beiden Modi."""

    @pytest.mark.parametrize("restore_flags", [False, True])
    def test_neue_buchung_erhaelt_notiz_kennzeichen_und_referenz(
        self,
        factory: sessionmaker[Session],
        restore_flags: bool,
    ) -> None:
        """Neue Buchung traegt Dateiwerte unabhaengig vom Wiederherstellungsmodus."""
        with factory() as session:
            account = create_account(session)
            payload = own_json_document(
                account.id,
                [
                    payload_transaction(
                        account.id,
                        note="Notiz aus Datei",
                        checked=True,
                        checked_at=CHECKED_AT,
                        reconciled=True,
                        reconciled_at=RECONCILED_AT,
                        external_ref="ref-aus-datei",
                    )
                ],
            )

            run = execute_import(
                account.id,
                payload,
                None,
                "json",
                session,
                restore_flags_for_existing=restore_flags,
            )

            assert run.new_count == 1
            assert run.skipped_count == 0
            transaction = session.scalars(select(Transaction)).one()
            assert transaction.note == "Notiz aus Datei"
            assert transaction.checked is True
            assert transaction.checked_at == CHECKED_AT
            assert transaction.reconciled is True
            assert transaction.reconciled_at == RECONCILED_AT
            assert transaction.external_ref == "ref-aus-datei"
            assert count_transactions(session) == 1
