"""Tests fuer die Import-Vorschau (F-IMP-06).

Deckt die Statusklassifikation ``neu`` / ``vorhanden`` / ``verdachtsdublette``
fuer CSV und das eigene JSON-Format ab (F-DUP-02, F-DUP-03, F-IMP-03,
F-IMP-04) und belegt explizit, dass die Vorschau die Datenbank nicht veraendert
(reine Lesefunktion, F-IMP-06).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.duplicate import DuplicateDecision
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.import_json import TransactionData as OwnJsonTransactionData
from finue.services.import_preview import ImportPreview, build_preview
from finue.services.mapping import TransactionData as MappedTransactionData

IBAN = "DE89370400440532013000"


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


def fingerprint(
    account_id: int,
    *,
    booking_date: date,
    amount_cents: int,
    currency: str = "EUR",
    counterparty: str | None = None,
    counterparty_iban: str | None = IBAN,
    purpose: str | None = None,
    value_date: date | None = None,
) -> str:
    """Berechnet den F-DUP-01-Fingerprint fuer Testdaten."""
    return compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
    )


def add_stored(
    session: Session,
    account_id: int,
    *,
    booking_date: date = date(2026, 1, 15),
    amount_cents: int = -4883,
    currency: str = "EUR",
    counterparty: str | None = "Max Mustermann",
    counterparty_iban: str | None = IBAN,
    purpose: str | None = "SEPA Ueberweisung",
    origin: str = "fints",
    occurrence_index: int = 1,
    merged: bool = False,
) -> Transaction:
    """Legt eine gespeicherte Buchung mit passendem Fingerprint an."""
    transaction = Transaction(
        account_id=account_id,
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        origin=origin,
        fingerprint=fingerprint(
            account_id,
            booking_date=booking_date,
            amount_cents=amount_cents,
            currency=currency,
            counterparty=counterparty,
            counterparty_iban=counterparty_iban,
            purpose=purpose,
        ),
        occurrence_index=occurrence_index,
        merged=merged,
    )
    session.add(transaction)
    session.commit()
    return transaction


def csv_rows() -> list[dict[str, str]]:
    """Liefert drei CSV-Zeilen fuer vorhanden, Verdacht und neu."""
    return [
        {
            "Datum": "15.01.2026",
            "Betrag": "-48,83",
            "Gegenpartei": "Max Mustermann",
            "IBAN": IBAN,
            "Zweck": "SEPA Ueberweisung",
            "Waehrung": "EUR",
        },
        {
            "Datum": "16.01.2026",
            "Betrag": "-48,83",
            "Gegenpartei": "Max Mustermann",
            "IBAN": IBAN,
            "Zweck": "SEPA Ueberweisung geaendert",
            "Waehrung": "EUR",
        },
        {
            "Datum": "01.02.2026",
            "Betrag": "-12,34",
            "Gegenpartei": "Neuer Laden",
            "IBAN": "",
            "Zweck": "Einkauf",
            "Waehrung": "EUR",
        },
    ]


def csv_mapping() -> dict[str, str]:
    """Mapping fuer :func:`csv_rows`."""
    return {
        "Datum": "booking_date",
        "Betrag": "amount_cents",
        "Gegenpartei": "counterparty",
        "IBAN": "counterparty_iban",
        "Zweck": "purpose",
        "Waehrung": "currency",
    }


def own_json_payload(account_id: int) -> dict[str, Any]:
    """Liefert ein v1-Dokument mit vorhanden-, Verdacht- und neu-Buchung."""
    return {
        "format": "finanzuebersicht",
        "version": 1,
        "created_at": "2026-11-26T10:41:13Z",
        "filter": {
            "scope": "selection",
            "account_ids": [account_id],
            "group_ids": None,
            "transaction_ids": [101, 102, 103],
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
        "transactions": [
            {
                "id": 101,
                "account_id": account_id,
                "status": "gebucht",
                "booking_date": "2026-01-15",
                "value_date": None,
                "counterparty": "Max Mustermann",
                "counterparty_iban": IBAN,
                "purpose": "SEPA Ueberweisung",
                "note": "aus Export",
                "checked": False,
                "checked_at": None,
                "checked_origin": None,
                "reconciled": False,
                "reconciled_at": None,
                "reconciled_origin": None,
                "origin": "fints",
                "external_ref": None,
                "fingerprint": fingerprint(
                    account_id,
                    booking_date=date(2026, 1, 15),
                    amount_cents=-4883,
                    counterparty="Max Mustermann",
                    purpose="SEPA Ueberweisung",
                ),
                "occurrence_index": 1,
                "merged": False,
                "amount": "-48.83",
                "currency": "EUR",
            },
            {
                "id": 102,
                "account_id": account_id,
                "status": "gebucht",
                "booking_date": "2026-01-16",
                "value_date": None,
                "counterparty": "Max Mustermann",
                "counterparty_iban": IBAN,
                "purpose": "SEPA Ueberweisung geaendert",
                "note": None,
                "checked": False,
                "checked_at": None,
                "checked_origin": None,
                "reconciled": False,
                "reconciled_at": None,
                "reconciled_origin": None,
                "origin": "csv",
                "external_ref": None,
                "fingerprint": fingerprint(
                    account_id,
                    booking_date=date(2026, 1, 16),
                    amount_cents=-4883,
                    counterparty="Max Mustermann",
                    purpose="SEPA Ueberweisung geaendert",
                ),
                "occurrence_index": 1,
                "merged": False,
                "amount": "-48.83",
                "currency": "EUR",
            },
            {
                "id": 103,
                "account_id": account_id,
                "status": "gebucht",
                "booking_date": "2026-02-01",
                "value_date": None,
                "counterparty": "Neuer Laden",
                "counterparty_iban": None,
                "purpose": "Einkauf",
                "note": None,
                "checked": False,
                "checked_at": None,
                "checked_origin": None,
                "reconciled": False,
                "reconciled_at": None,
                "reconciled_origin": None,
                "origin": "csv",
                "external_ref": None,
                "fingerprint": fingerprint(
                    account_id,
                    booking_date=date(2026, 2, 1),
                    amount_cents=-1234,
                    counterparty="Neuer Laden",
                    counterparty_iban=None,
                    purpose="Einkauf",
                ),
                "occurrence_index": 1,
                "merged": False,
                "amount": "-12.34",
                "currency": "EUR",
            },
        ],
    }


def count(session: Session, model: type[Any]) -> int:
    """Zaehlt die Zeilen eines Modells."""
    return session.scalar(select(func.count()).select_from(model)) or 0


def transaction_snapshot(session: Session) -> list[tuple[Any, ...]]:
    """Liest alle Buchungsfelder als vergleichbaren Vorher/Nachher-Stand."""
    rows = session.scalars(select(Transaction).order_by(Transaction.id)).all()
    return [
        (
            row.id,
            row.account_id,
            row.status,
            row.booking_date,
            row.value_date,
            row.amount_cents,
            row.currency,
            row.counterparty,
            row.counterparty_iban,
            row.purpose,
            row.search_text,
            row.note,
            row.checked,
            row.reconciled,
            row.origin,
            row.external_ref,
            row.fingerprint,
            row.occurrence_index,
            row.merged,
            row.version,
            row.created_at,
            row.updated_at,
        )
        for row in rows
    ]


class TestCsvPreview:
    """F-IMP-03/F-IMP-06: CSV-Zeilen werden korrekt klassifiziert."""

    def test_csv_zeilen_werden_als_neu_vorhanden_und_verdacht_klassifiziert(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Je ein Fall fuer vorhanden, Verdachtsdublette und neu."""
        with factory() as session:
            account = create_account(session)
            stored = add_stored(session, account.id)

            preview = build_preview(account.id, csv_rows(), csv_mapping(), "csv", session)

            assert isinstance(preview, ImportPreview)
            assert [row.status for row in preview.rows] == ["vorhanden", "verdachtsdublette", "neu"]
            assert [row.row_no for row in preview.rows] == [1, 2, 3]
            assert preview.rows[0].fingerprint == stored.fingerprint
            assert all(isinstance(row.parsed_data, MappedTransactionData) for row in preview.rows)

    def test_csv_zusammenfassung_zaehlt_jeden_status(self, factory: sessionmaker[Session]) -> None:
        """F-IMP-06: Die Zusammenfassung enthaelt die Anzahl je Status."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)

            preview = build_preview(account.id, csv_rows(), csv_mapping(), "csv", session)

            assert preview.summary.neu == 1
            assert preview.summary.vorhanden == 1
            assert preview.summary.verdachtsdublette == 1
            assert preview.new_count == 1
            assert preview.existing_count == 1
            assert preview.suspected_count == 1
            assert len(preview.rows) == sum(
                (
                    preview.summary.neu,
                    preview.summary.vorhanden,
                    preview.summary.verdachtsdublette,
                )
            )

    def test_row_offset_aus_dem_mapping_wird_beruecksichtigt(self, factory: sessionmaker[Session]) -> None:
        """F-IMP-02: Vorspann-Zeilen verschieben die angezeigte Zeilennummer."""
        with factory() as session:
            account = create_account(session)
            mapping = {**csv_mapping(), "row_offset": 3}

            preview = build_preview(account.id, csv_rows(), mapping, "csv", session)

            assert [row.row_no for row in preview.rows] == [4, 5, 6]

    def test_mehrfach_vorkommen_ersten_sind_vorhanden_rest_neu(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-02: Bei n_db=1 und zwei gleichen Zeilen ist die zweite neu."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            rows = [csv_rows()[0], csv_rows()[0]]

            preview = build_preview(account.id, rows, csv_mapping(), "csv", session)

            assert [row.status for row in preview.rows] == ["vorhanden", "neu"]

    def test_fehlendes_mapping_wird_abgelehnt(self, factory: sessionmaker[Session]) -> None:
        """F-IMP-03: CSV ohne Mapping ist kein gueltiger Vorschau-Aufruf."""
        with factory() as session:
            account = create_account(session)

            with pytest.raises(ValueError, match="Mapping"):
                build_preview(account.id, csv_rows(), None, "csv", session)

    def test_leere_zeilenliste_ergibt_leere_vorschau(self, factory: sessionmaker[Session]) -> None:
        """Eine leere Lieferung ist eine leere Vorschau ohne Schreibzugriff."""
        with factory() as session:
            account = create_account(session)

            preview = build_preview(account.id, [], csv_mapping(), "csv", session)

            assert preview.rows == []
            assert preview.summary.neu == preview.summary.vorhanden == 0
            assert preview.summary.verdachtsdublette == 0


class TestOwnJsonPreview:
    """F-IMP-04/F-IMP-06: Eigenes JSON wird ohne Mapping klassifiziert."""

    def test_eigenes_json_wird_als_neu_vorhanden_und_verdacht_klassifiziert(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Eigenes JSON nutzt parse_own_json und dieselbe Statuslogik."""
        with factory() as session:
            account = create_account(session)
            stored = add_stored(session, account.id)

            preview = build_preview(account.id, own_json_payload(account.id), None, "json", session)

            assert [row.status for row in preview.rows] == ["vorhanden", "verdachtsdublette", "neu"]
            assert [row.row_no for row in preview.rows] == [1, 2, 3]
            assert preview.rows[0].fingerprint == stored.fingerprint
            assert all(isinstance(row.parsed_data, OwnJsonTransactionData) for row in preview.rows)

    def test_eigenes_json_behaelt_notiz_und_herkunft_im_parsed_data(self, factory: sessionmaker[Session]) -> None:
        """F-IMP-07-Vorbereitung: Quellfelder bleiben fuer den Import erhalten."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)

            preview = build_preview(account.id, own_json_payload(account.id), None, "json", session)

            first = preview.rows[0].parsed_data
            assert isinstance(first, OwnJsonTransactionData)
            assert first.note == "aus Export"
            assert first.origin == "fints"

    def test_eigenes_json_mit_mapping_wird_als_fremdes_json_behandelt(self, factory: sessionmaker[Session]) -> None:
        """F-IMP-04: Ohne eigenen Kopf greift das Mapping fuer fremdes JSON."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            document = own_json_payload(account.id)
            document["format"] = "fremd"
            rows = document["transactions"]

            preview = build_preview(
                account.id,
                rows,
                {
                    "booking_date": "booking_date",
                    "amount_cents": "amount",
                    "counterparty": "counterparty",
                    "purpose": "purpose",
                    "currency": "currency",
                },
                "json",
                session,
            )

            assert [row.status for row in preview.rows] == ["neu", "verdachtsdublette", "neu"]
            assert all(isinstance(row.parsed_data, MappedTransactionData) for row in preview.rows)


class TestPreviewIsReadOnly:
    """F-IMP-06: Die Vorschau veraendert keine Datenbankzeile."""

    def test_vorschau_aendert_keine_buchung(self, factory: sessionmaker[Session]) -> None:
        """Vorher/Nachher-Stand aller Buchungen ist identisch."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            before = transaction_snapshot(session)

            build_preview(account.id, csv_rows(), csv_mapping(), "csv", session)
            build_preview(account.id, own_json_payload(account.id), None, "json", session)

            assert transaction_snapshot(session) == before
            assert count(session, Transaction) == len(before)

    def test_vorschau_erzeugt_weder_import_lauf_noch_entscheidung(self, factory: sessionmaker[Session]) -> None:
        """Es entstehen keine ImportRun-, ImportRunError- oder DuplicateDecision-Zeilen."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)

            preview = build_preview(account.id, csv_rows(), csv_mapping(), "csv", session)

            assert len(preview.rows) == 3
            assert count(session, ImportRun) == 0
            assert count(session, ImportRunError) == 0
            assert count(session, DuplicateDecision) == 0
            assert len(session.new) == 0
            assert len(session.dirty) == 0

    def test_verdachtsdublettenpruefung_veraendert_bestand_nicht(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-03: Verdacht wird nur gemeldet, nicht gespeichert oder zusammengefuehrt."""
        with factory() as session:
            account = create_account(session)
            stored = add_stored(session, account.id)
            before = transaction_snapshot(session)

            preview = build_preview(account.id, [csv_rows()[1]], csv_mapping(), "csv", session)

            assert preview.rows[0].status == "verdachtsdublette"
            assert transaction_snapshot(session) == before
            assert stored.merged is False
            assert session.get(Transaction, stored.id) is stored
