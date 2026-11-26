"""Tests fuer die Import-Durchfuehrung mit ImportRun-Protokoll (T-054).

Referenziert F-IMP-06 (Import als Import-Lauf mit Protokoll), F-SYN-07
(Zeitpunkt, Konto, Status, Zaehler, Fehlercode und Klartext), F-DUP-02
(Multimengen-Abgleich, Idempotenz) und F-DUP-03 (Verdachtsdubletten werden
importiert, aber getrennt gezaehlt).

Abgedeckt:

- erfolgreicher Import mit gemischten neu/vorhanden/Verdacht-Zeilen,
- eine fehlerhafte Zeile stoppt den Rest des Imports nicht (Status ``teilweise``),
- sind alle Zeilen fehlerhaft, lautet der Status ``fehler``,
- wiederholter Import derselben Datei erzeugt 0 neue Buchungen,
- eigenes JSON wird ohne Mapping importiert und behaelt die Notiz,
- Laufzeitfehler hinterlassen keinen halb geschriebenen Zustand.
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
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.transaction import Transaction
from finue.services import import_run as import_run_module
from finue.services.duplicate import compute_fingerprint
from finue.services.import_run import execute_import

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
        merged=False,
    )
    session.add(transaction)
    session.commit()
    return transaction


def csv_mapping() -> dict[str, str]:
    """Mapping fuer die CSV-Testzeilen."""
    return {
        "Datum": "booking_date",
        "Betrag": "amount_cents",
        "Gegenpartei": "counterparty",
        "IBAN": "counterparty_iban",
        "Zweck": "purpose",
        "Waehrung": "currency",
    }


def csv_row(
    *,
    datum: str,
    betrag: str,
    gegenpartei: str,
    iban: str,
    zweck: str,
) -> dict[str, str]:
    """Erzeugt eine CSV-Zeile im Testformat."""
    return {
        "Datum": datum,
        "Betrag": betrag,
        "Gegenpartei": gegenpartei,
        "IBAN": iban,
        "Zweck": zweck,
        "Waehrung": "EUR",
    }


def mixed_csv_rows() -> list[dict[str, str]]:
    """Liefert vorhanden, Verdachtsdublette und neu in einer Datei."""
    return [
        csv_row(
            datum="15.01.2026",
            betrag="-48,83",
            gegenpartei="Max Mustermann",
            iban=IBAN,
            zweck="SEPA Ueberweisung",
        ),
        csv_row(
            datum="16.01.2026",
            betrag="-48,83",
            gegenpartei="Max Mustermann",
            iban=IBAN,
            zweck="SEPA Ueberweisung geaendert",
        ),
        csv_row(
            datum="01.02.2026",
            betrag="-12,34",
            gegenpartei="Neuer Laden",
            iban="",
            zweck="Einkauf",
        ),
    ]


def own_json_payload(account_id: int, *, note: str | None = "aus Datei") -> dict[str, Any]:
    """Liefert ein gueltiges eigenes JSON-Dokument mit einer neuen Buchung."""
    booking_date = date(2026, 2, 10)
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
        "transactions": [
            {
                "id": 201,
                "account_id": account_id,
                "status": "gebucht",
                "booking_date": booking_date.isoformat(),
                "value_date": None,
                "counterparty": "Neuer Laden",
                "counterparty_iban": None,
                "purpose": "Einkauf",
                "note": note,
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
                    booking_date=booking_date,
                    amount_cents=-1234,
                    counterparty="Neuer Laden",
                    counterparty_iban=None,
                    purpose="Einkauf",
                ),
                "occurrence_index": 1,
                "merged": False,
                "amount": "-12.34",
                "currency": "EUR",
            }
        ],
    }


def count(session: Session, model: type[Any]) -> int:
    """Zaehlt die Zeilen eines Modells."""
    return session.scalar(select(func.count()).select_from(model)) or 0


class TestExecuteImportMixedRows:
    """F-IMP-06/F-SYN-07: Ein Lauf protokolliert alle drei Zeilenkategorien."""

    def test_gemischte_zeilen_werden_korrekt_gezaehlt_und_neue_angelegt(self, factory: sessionmaker[Session]) -> None:
        """Vorhandene Zeile wird uebersprungen, Verdacht und neu werden importiert."""
        with factory() as session:
            account = create_account(session)
            stored = add_stored(session, account.id)

            run = execute_import(account.id, mixed_csv_rows(), csv_mapping(), "csv", session)

            assert isinstance(run, ImportRun)
            assert run.id is not None
            assert run.account_id == account.id
            assert run.kind == "csv"
            assert run.status == "erfolgreich"
            assert run.started_at is not None
            assert run.finished_at is not None
            assert run.new_count == 1
            assert run.skipped_count == 1
            assert run.suspected_count == 1
            assert run.failed_count == 0
            assert run.message is not None
            assert "1 neu" in run.message
            assert run.errors == []

            assert count(session, ImportRun) == 1
            assert count(session, ImportRunError) == 0

            transactions = session.scalars(select(Transaction).order_by(Transaction.id)).all()
            assert len(transactions) == 3
            # Die vorhandene Buchung bleibt unveraendert und wird nicht dupliziert.
            assert transactions[0].id == stored.id
            assert sum(1 for row in transactions if row.amount_cents == -4883) == 2
            assert sum(1 for row in transactions if row.amount_cents == -1234) == 1
            assert {row.origin for row in transactions[1:]} == {"csv"}


class TestExecuteImportRowErrors:
    """F-IMP-06: Eine fehlerhafte Zeile stoppt den restlichen Import nicht."""

    def test_fehlerhafte_zeile_wird_protokolliert_und_rest_importiert(self, factory: sessionmaker[Session]) -> None:
        """Zeile 2 ist unlesbar, Zeilen 1 und 3 werden trotzdem angelegt."""
        with factory() as session:
            account = create_account(session)
            rows = [
                csv_row(
                    datum="01.03.2026",
                    betrag="-10,00",
                    gegenpartei="Erste Buchung",
                    iban="",
                    zweck="Test A",
                ),
                csv_row(
                    datum="02.03.2026",
                    betrag="keine-zahl",
                    gegenpartei="Defekte Buchung",
                    iban="",
                    zweck="Test B",
                ),
                csv_row(
                    datum="03.03.2026",
                    betrag="-20,00",
                    gegenpartei="Dritte Buchung",
                    iban="",
                    zweck="Test C",
                ),
            ]

            run = execute_import(account.id, rows, csv_mapping(), "csv", session)

            assert run.status == "teilweise"
            assert run.new_count == 2
            assert run.skipped_count == 0
            assert run.suspected_count == 0
            assert run.failed_count == 1
            assert run.finished_at is not None
            assert run.message is not None
            assert run.message.startswith("Import teilweise abgeschlossen")

            errors = session.scalars(select(ImportRunError)).all()
            assert len(errors) == 1
            assert errors[0].row_no == 2
            assert errors[0].code == "mapping_error"
            assert "Zeile 2" in errors[0].message

            transactions = session.scalars(select(Transaction).order_by(Transaction.id)).all()
            assert len(transactions) == 2
            assert {row.amount_cents for row in transactions} == {-1000, -2000}

    def test_alle_zeilen_fehlerhaft_ergibt_status_fehler(self, factory: sessionmaker[Session]) -> None:
        """Sind alle Zeilen fehlerhaft, lautet der Status ``fehler`` (F-SYN-07)."""
        with factory() as session:
            account = create_account(session)
            rows = [
                csv_row(
                    datum="kein-datum",
                    betrag="-10,00",
                    gegenpartei="Erste defekte Buchung",
                    iban="",
                    zweck="Test A",
                ),
                csv_row(
                    datum="02.03.2026",
                    betrag="keine-zahl",
                    gegenpartei="Zweite defekte Buchung",
                    iban="",
                    zweck="Test B",
                ),
            ]

            run = execute_import(account.id, rows, csv_mapping(), "csv", session)

            assert run.status == "fehler"
            assert run.new_count == 0
            assert run.skipped_count == 0
            assert run.suspected_count == 0
            assert run.failed_count == 2
            assert run.finished_at is not None
            assert run.message is not None
            assert run.message.startswith("Import fehlerhaft")
            assert count(session, Transaction) == 0
            assert count(session, ImportRunError) == 2
            assert {error.row_no for error in run.errors} == {1, 2}


class TestExecuteImportIdempotent:
    """F-DUP-02/F-IMP-06: Derselbe Import legt beim zweiten Mal nichts an."""

    def test_wiederholter_import_erzeugt_null_neue_buchungen(self, factory: sessionmaker[Session]) -> None:
        """Zweiter Lauf meldet new_count=0 und laesst den Bestand unveraendert."""
        with factory() as session:
            account = create_account(session)
            rows = [
                csv_row(
                    datum="01.04.2026",
                    betrag="-11,00",
                    gegenpartei="Erste Buchung",
                    iban="",
                    zweck="Test A",
                ),
                csv_row(
                    datum="02.04.2026",
                    betrag="-22,00",
                    gegenpartei="Zweite Buchung",
                    iban="",
                    zweck="Test B",
                ),
            ]

            first = execute_import(account.id, rows, csv_mapping(), "csv", session)
            assert first.new_count == 2
            assert count(session, Transaction) == 2

            second = execute_import(account.id, rows, csv_mapping(), "csv", session)

            assert second.status == "erfolgreich"
            assert second.new_count == 0
            assert second.skipped_count == 2
            assert second.suspected_count == 0
            assert second.failed_count == 0
            assert count(session, Transaction) == 2
            assert count(session, ImportRun) == 2

    def test_zweite_identische_zeile_wird_trotz_bestand_genau_einmal_angelegt(
        self, factory: sessionmaker[Session]
    ) -> None:
        """F-DUP-02: 1 vorhanden + 2 geliefert ergibt genau 1 neue Buchung."""
        with factory() as session:
            account = create_account(session)
            add_stored(session, account.id)
            row = csv_row(
                datum="15.01.2026",
                betrag="-48,83",
                gegenpartei="Max Mustermann",
                iban=IBAN,
                zweck="SEPA Ueberweisung",
            )

            run = execute_import(account.id, [row, row], csv_mapping(), "csv", session)

            assert run.new_count == 1
            assert run.skipped_count == 1
            assert run.suspected_count == 0
            assert count(session, Transaction) == 2
            assert sum(1 for item in session.scalars(select(Transaction)) if item.amount_cents == -4883) == 2


class TestExecuteImportOwnJson:
    """F-IMP-04/F-IMP-06: Eigenes JSON laeuft ohne Mapping durch denselben Lauf."""

    def test_eigenes_json_importiert_neue_buchung_mit_notiz(self, factory: sessionmaker[Session]) -> None:
        """Die Dateinotiz der neuen Buchung bleibt erhalten."""
        with factory() as session:
            account = create_account(session)

            run = execute_import(account.id, own_json_payload(account.id), None, "json", session)

            assert run.status == "erfolgreich"
            assert run.kind == "json"
            assert run.new_count == 1
            assert run.failed_count == 0

            transactions = session.scalars(select(Transaction)).all()
            assert len(transactions) == 1
            assert transactions[0].note == "aus Datei"
            assert transactions[0].origin == "csv"
            assert transactions[0].amount_cents == -1234


class TestExecuteImportAtomicity:
    """F-IMP-06: Ein Laufzeitfehler hinterlaesst keinen halb geschriebenen Zustand."""

    def test_laufzeitfehler_rollt_buchungen_zurueck_und_protokolliert_fehlschlag(
        self, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bereits geflushte Buchungen verschwinden, der Fehlerlauf bleibt."""
        with factory() as session:
            account = create_account(session)

            def failing_reconcile(
                account_id: int,
                incoming: list[Any],
                session: Session,
            ) -> None:
                session.add(
                    Transaction(
                        account_id=account_id,
                        booking_date=date(2026, 1, 1),
                        amount_cents=-1,
                        currency="EUR",
                        origin="csv",
                        fingerprint="0" * 64,
                        occurrence_index=0,
                        merged=False,
                    )
                )
                session.flush()
                raise RuntimeError("Simulierter Fehler")

            monkeypatch.setattr(import_run_module, "reconcile_multiset", failing_reconcile)

            run = execute_import(
                account.id,
                [csv_row(datum="01.05.2026", betrag="-5,00", gegenpartei="X", iban="", zweck="Y")],
                csv_mapping(),
                "csv",
                session,
            )

            assert run.status == "fehler"
            assert run.finished_at is not None
            assert run.failed_count == 1
            assert "Simulierter Fehler" in (run.message or "")
            assert count(session, Transaction) == 0
            assert count(session, ImportRun) == 1
            assert count(session, ImportRunError) == 1
            assert run.errors[0].code == "run_failed"
            assert run.errors[0].row_no is None
