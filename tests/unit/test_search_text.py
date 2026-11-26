"""Tests fuer die normalisierte Suchspalte (T-087).

Referenziert F-SUC-01 (Suchfeld ueber Gegenpartei, Verwendungszweck, Notiz, IBAN
und Betrag, Begriffe kleingeschrieben), F-SUC-06 (portable Suche ueber eine
normalisierte Spalte) und F-ABS-05 (optionale externe Referenz, durchsuchbar).

Abgedeckt:

* die Normalisierung inklusive Umlauten, scharfem s, Zeilenumbruechen und
  Mehrfachleerzeichen sowie IBAN mit und ohne Leerzeichen,
* die Paritaet zwischen der Laufzeitnormalisierung in
  :mod:`finue.services.search_text` und der eingefrorenen Kopie in Migration
  0004,
* die Befuellung von ``search_text`` ueber Import, Sync, Vormerkungen und den
  Multimengen-Abgleich (Dublettenpfad),
* die Neubildung der Suchspalte bei Notizaenderungen.

Alle Buchungen und Konten sind erfundene Testdaten (D8). Es finden keine
Bankzugriffe statt; PIN, TAN und Dialogdaten kommen nicht vor.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.transaction import Transaction
from finue.services.duplicate import TransactionData, compute_fingerprint, reconcile_multiset
from finue.services.fints_types import FetchedTransaction, SyncAccountData
from finue.services.import_run import execute_import
from finue.services.search_text import (
    build_search_text,
    normalize_query_term,
    normalize_search_text,
    refresh_search_text,
)
from finue.services.sync_persist import persist_account_sync

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MIGRATION_0004: Final[Path] = (
    PROJECT_ROOT / "backend" / "finue" / "migrations" / "versions" / "0004_search_text_backfill.py"
)

IBAN_SPACED: Final[str] = "DE02 1203 0000 0000 2020 51"
IBAN_COMPACT: Final[str] = "DE02120300000000202051"


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


def load_migration_0004() -> ModuleType:
    """Laedt das Migrationsmodul 0004 ueber den Dateipfad (kein Paketimport).

    Der Dateiname beginnt mit Ziffern und ist damit kein regulaerer
    Modulname; fuer den Paritaetstest wird das Modul daher explizit geladen.
    """
    assert MIGRATION_0004.is_file(), f"Migrationsdatei fehlt: {MIGRATION_0004}"
    spec = importlib.util.spec_from_file_location("migration_0004_search_text", MIGRATION_0004)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestNormalization:
    """F-SUC-01/F-SUC-06: Eine einheitliche Normalisierung fuer Spalte und Begriff."""

    def test_casefold_und_nfc_vereinheitlichen_schreibweisen(self) -> None:
        """Gross-/Kleinschreibung und Unicode-Zerlegung ergeben denselben Text."""
        assert normalize_search_text("MÜLLER") == "müller"
        # Zerlegt (u + Trema) vs. zusammengesetzt (ü) muessen gleich sein (NFC).
        assert normalize_search_text("Mu\u0308ller") == normalize_search_text("Müller")
        # Scharfes s wird nach casefold zu "ss" (Treffer fuer "Strasse").
        assert normalize_search_text("Straße") == "strasse"
        assert normalize_search_text("STRASSE") == normalize_search_text("straße")

    def test_zeilenumbrueche_und_mehrfachleerzeichen_werden_zusammengefasst(self) -> None:
        """Umbrueche und mehrfacher Leerraum werden zu einem Leerzeichen."""
        assert normalize_search_text("Max\n\nMuster\tmann") == "max muster mann"
        assert normalize_search_text("  a   b \r\n c  ") == "a b c"

    def test_leere_werte_ergeben_leeren_string(self) -> None:
        """None und der leere String ergeben einen leeren Text."""
        assert normalize_search_text(None) == ""
        assert normalize_search_text("") == ""

    def test_suchbegriff_nutzt_dieselbe_regel(self) -> None:
        """normalize_query_term normalisiert wie die Suchspalte (F-SUC-01)."""
        assert normalize_query_term("  MÜLLER  ") == normalize_search_text("MÜLLER")
        assert normalize_query_term("Straße") == "strasse"


class TestBuildSearchText:
    """F-SUC-01/F-ABS-05: Inhalt der Suchspalte."""

    def test_enthaelt_alle_felder_normalisiert(self) -> None:
        """Gegenpartei, Zweck, Notiz, IBAN und Referenz werden aufgenommen."""
        text = build_search_text(
            counterparty="Max Mustermann",
            purpose="Gehalt",
            note="Gehalt Juni",
            counterparty_iban=None,
            external_ref="ext-1",
        )
        assert text == "max mustermann gehalt gehalt juni ext-1"

    def test_iban_zusaetzlich_ohne_leerzeichen(self) -> None:
        """Die IBAN erscheint mit und ohne Leerzeichen in der Spalte."""
        text = build_search_text(counterparty="Vermieter", counterparty_iban=IBAN_SPACED)
        assert IBAN_SPACED.casefold() in text
        assert IBAN_COMPACT.casefold() in text

    def test_kompakte_iban_wird_nicht_doppelt_angehaengt(self) -> None:
        """Ohne Leerzeichen unterscheidet sich die Zusatzform nicht und entfaellt."""
        text = build_search_text(counterparty="Vermieter", counterparty_iban=IBAN_COMPACT)
        assert text.count(IBAN_COMPACT.casefold()) == 1

    def test_leere_felder_entfallen(self) -> None:
        """Alle Felder leer ergibt einen leeren Text."""
        assert build_search_text() == ""
        assert build_search_text(counterparty=None, purpose="", note=None) == ""

    def test_notiz_und_referenz_sind_durchsuchbar(self) -> None:
        """Notiz und externe Referenz sind Teil der Suchspalte (F-ABS-05)."""
        text = build_search_text(note="Wichtig", external_ref="TX-42")
        assert "wichtig" in text
        assert "tx-42" in text

    def test_refresh_baut_spalte_neu(self) -> None:
        """refresh_search_text uebernimmt geaenderte Felder."""
        transaction = Transaction(
            account_id=1,
            booking_date=date(2026, 1, 1),
            amount_cents=-100,
            currency="EUR",
            counterparty="Max Mustermann",
            purpose="Gehalt",
            origin="fints",
            fingerprint="a" * 64,
        )
        assert refresh_search_text(transaction) == "max mustermann gehalt"
        transaction.note = "Gehalt Juni"
        assert refresh_search_text(transaction) == "max mustermann gehalt gehalt juni"
        assert transaction.search_text == "max mustermann gehalt gehalt juni"


class TestMigrationParity:
    """F-SUC-01/F-SUC-06: Laufzeit- und eingefrorene Migrationsnormalisierung."""

    def test_eingefrorene_kopie_entspricht_laufzeitfunktion(self) -> None:
        """Migration 0004 darf von der Laufzeitregel nicht abweichen."""
        migration = load_migration_0004()
        samples: list[tuple[str | None, str | None, str | None, str | None, str | None]] = [
            ("Max Mustermann", "Gehalt", None, IBAN_SPACED, None),
            ("MÜLLER", "Straße", "Notiz\tText", IBAN_COMPACT, "ref-1"),
            (None, None, None, None, None),
            ("  a\n\nb  ", "", "  ", IBAN_SPACED, "  "),
            ("Vermieter", "Miete", "Wichtig", None, None),
        ]
        for sample in samples:
            expected = build_search_text(*sample)
            actual = migration._build_search_text(*sample)
            assert actual == expected, f"Abweichung bei {sample!r}: {actual!r} != {expected!r}"


class TestImportPopulatesSearchText:
    """F-SUC-01/F-SUC-06: Der Datei-Import befuellt die Suchspalte."""

    def test_csv_import_setzt_search_text(self, factory: sessionmaker[Session]) -> None:
        """Neu importierte Buchungen tragen eine gefuellte Suchspalte."""
        with factory() as session:
            account = create_account(session)
            rows = [
                {
                    "Datum": "15.01.2026",
                    "Betrag": "-48,83",
                    "Gegenpartei": "Max Mustermann",
                    "IBAN": IBAN_SPACED,
                    "Zweck": "SEPA Ueberweisung",
                    "Waehrung": "EUR",
                }
            ]
            mapping = {
                "Datum": "booking_date",
                "Betrag": "amount_cents",
                "Gegenpartei": "counterparty",
                "IBAN": "counterparty_iban",
                "Zweck": "purpose",
                "Waehrung": "currency",
            }

            run = execute_import(account.id, rows, mapping, "csv", session)

            assert run.new_count == 1
            stored = session.scalars(select(Transaction).where(Transaction.account_id == account.id)).one()
            expected = build_search_text(
                counterparty="Max Mustermann",
                purpose="SEPA Ueberweisung",
                counterparty_iban=IBAN_SPACED,
            )
            assert stored.search_text == expected
            assert IBAN_COMPACT.casefold() in stored.search_text

    def test_restore_import_refresht_search_text_bei_notizaenderung(self, factory: sessionmaker[Session]) -> None:
        """F-IMP-07/F-SUC-01: Eine wiederhergestellte Notiz aktualisiert die Spalte."""
        with factory() as session:
            account = create_account(session)
            stored = Transaction(
                account_id=account.id,
                status="gebucht",
                booking_date=date(2026, 1, 3),
                value_date=date(2026, 1, 3),
                amount_cents=-1234,
                currency="EUR",
                counterparty="Max Mustermann",
                counterparty_iban=None,
                purpose="SEPA Ueberweisung",
                search_text="max mustermann sepa ueberweisung",
                origin="fints",
                fingerprint=compute_fingerprint(
                    account_id=account.id,
                    booking_date=date(2026, 1, 3),
                    value_date=date(2026, 1, 3),
                    amount_cents=-1234,
                    currency="EUR",
                    counterparty="Max Mustermann",
                    counterparty_iban=None,
                    purpose="SEPA Ueberweisung",
                ),
                occurrence_index=1,
                merged=False,
            )
            session.add(stored)
            session.commit()

            payload = _own_json_document(
                account.id,
                [
                    {
                        "id": 1,
                        "account_id": account.id,
                        "status": "gebucht",
                        "booking_date": "2026-01-03",
                        "value_date": "2026-01-03",
                        "counterparty": "Max Mustermann",
                        "counterparty_iban": None,
                        "purpose": "SEPA Ueberweisung",
                        "note": "Gehalt Juni",
                        "checked": False,
                        "checked_at": None,
                        "checked_origin": None,
                        "reconciled": False,
                        "reconciled_at": None,
                        "reconciled_origin": None,
                        "origin": "fints",
                        "external_ref": "neu-ref",
                        "fingerprint": "f" * 64,
                        "occurrence_index": 1,
                        "merged": False,
                        "amount": "-12.34",
                        "currency": "EUR",
                    }
                ],
            )

            execute_import(account.id, payload, None, "json", session, restore_flags_for_existing=True)

            session.refresh(stored)
            expected = build_search_text(
                counterparty="Max Mustermann",
                purpose="SEPA Ueberweisung",
                note="Gehalt Juni",
                external_ref="neu-ref",
            )
            assert stored.note == "Gehalt Juni"
            assert stored.search_text == expected


class TestSyncPopulatesSearchText:
    """F-SUC-01/F-SUC-06: Sync und Vormerkungen befuellen die Suchspalte."""

    def test_gebuchte_und_vorgemerkte_buchung_erhalten_search_text(self, factory: sessionmaker[Session]) -> None:
        """Buchungen und Vormerkungen eines Syncs sind durchsuchbar."""
        with factory() as session:
            account = create_account(session)
            booked = FetchedTransaction(
                booking_date=date(2026, 1, 10),
                amount_cents=-1000,
                currency="EUR",
                status="gebucht",
                counterparty="Max Mustermann",
                counterparty_iban=IBAN_SPACED,
                purpose="Gehalt",
            )
            pending = FetchedTransaction(
                booking_date=date(2026, 1, 12),
                amount_cents=-2000,
                currency="EUR",
                status="vorgemerkt",
                counterparty="Vermieter",
                counterparty_iban=None,
                purpose="Miete",
            )
            data = SyncAccountData(account_ref=IBAN_COMPACT, balance=None, transactions=(booked, pending))

            persist_account_sync(session, account.id, data, "job-1")

            rows = session.scalars(select(Transaction).order_by(Transaction.id)).all()
            by_status = {row.status: row for row in rows}
            assert by_status["gebucht"].search_text == build_search_text(
                counterparty="Max Mustermann", purpose="Gehalt", counterparty_iban=IBAN_SPACED
            )
            assert by_status["vorgemerkt"].search_text == build_search_text(counterparty="Vermieter", purpose="Miete")

    def test_uebertragene_notiz_aktualisiert_search_text(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-04/F-SUC-01: Uebernimmt der Sync eine Vormerkungsnotiz, folgt die Spalte."""
        with factory() as session:
            account = create_account(session)
            existing_pending = Transaction(
                account_id=account.id,
                status="vorgemerkt",
                booking_date=date(2026, 1, 5),
                amount_cents=-5000,
                currency="EUR",
                counterparty="Vermieter",
                counterparty_iban=None,
                purpose="Miete",
                search_text="vermieter miete",
                note="Wichtig",
                origin="fints",
                fingerprint="b" * 64,
                occurrence_index=0,
                merged=False,
            )
            session.add(existing_pending)
            session.commit()

            incoming = FetchedTransaction(
                booking_date=date(2026, 1, 6),
                amount_cents=-5000,
                currency="EUR",
                status="gebucht",
                counterparty="Vermieter",
                counterparty_iban=None,
                purpose="Miete",
            )
            data = SyncAccountData(account_ref=IBAN_COMPACT, balance=None, transactions=(incoming,))

            persist_account_sync(session, account.id, data, "job-1")

            booked = session.scalars(
                select(Transaction).where(Transaction.account_id == account.id, Transaction.status == "gebucht")
            ).one()
            assert booked.note == "Wichtig"
            assert booked.search_text == build_search_text(counterparty="Vermieter", purpose="Miete", note="Wichtig")


class TestDuplicatePathPopulatesSearchText:
    """F-SUC-01/F-SUC-06: Der Multimengen-Abgleich befuellt die Spalte selbst."""

    def test_build_transaction_baut_search_text_ohne_vorgabe(self, factory: sessionmaker[Session]) -> None:
        """Ohne explizite Vorgabe wird die Spalte aus den Quellfeldern gebaut."""
        with factory() as session:
            account = create_account(session)
            incoming = [
                TransactionData(
                    booking_date=date(2026, 1, 9),
                    amount_cents=-4200,
                    currency="EUR",
                    counterparty="Max Mustermann",
                    counterparty_iban=IBAN_SPACED,
                    purpose="Stromabschlag",
                )
            ]

            result = reconcile_multiset(account.id, incoming, session)

            assert result.inserted_count == 1
            stored = result.to_insert[0]
            assert stored.search_text == build_search_text(
                counterparty="Max Mustermann", purpose="Stromabschlag", counterparty_iban=IBAN_SPACED
            )


def _own_json_document(account_id: int, transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Baut ein gueltiges eigenes JSON-Dokument um die Buchungen (F-IMP-04)."""
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
