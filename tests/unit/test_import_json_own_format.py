"""Tests fuer den JSON-Import des eigenen Formats (F-IMP-04, F-EXP-07).

Prueft die Erkennung ohne Mapping, die Cent-Umrechnung aus Dezimal-Strings
(N-DB-02), klare Fehlermeldungen bei fehlerhaften Instanzen und die
Versionsregel aus F-EXP-07 (neuere Version wird abgelehnt).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from finue.services.import_json import (
    ImportFormatError,
    TransactionData,
    amount_string_to_cents,
    detect_own_format,
    parse_own_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = REPO_ROOT / "docs" / "schemas" / "export-v1.example.json"


def _payload() -> dict[str, Any]:
    """Liefert ein minimales, schema-valides Dokument der eigenen Version 1."""
    return {
        "format": "finanzuebersicht",
        "version": 1,
        "created_at": "2026-11-26T10:41:13Z",
        "filter": {
            "scope": "selection",
            "account_ids": [1],
            "group_ids": None,
            "transaction_ids": [101, 102],
            "search": None,
            "checked": None,
            "reconciled": None,
            "with_note": None,
        },
        "period": {"start": "2026-01-01", "end": "2026-11-25"},
        "accounts": [
            {
                "id": 1,
                "name": "Girokonto",
                "iban": "DE89370400440532013000",
                "currency": "EUR",
                "account_type": "giro",
            }
        ],
        "transactions": [
            {
                "id": 101,
                "account_id": 1,
                "status": "gebucht",
                "booking_date": "2026-01-15",
                "value_date": "2026-01-16",
                "counterparty": "Beispiel Markt GmbH",
                "counterparty_iban": "DE02120300000000202051",
                "purpose": "Einkauf",
                "note": "Wocheneinkauf",
                "checked": True,
                "checked_at": "2026-01-17T08:00:00Z",
                "checked_origin": "ui",
                "reconciled": True,
                "reconciled_at": "2026-01-18T09:30:00Z",
                "reconciled_origin": "import",
                "origin": "fints",
                "external_ref": "ACTUAL-2026-0001",
                "fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "occurrence_index": 1,
                "merged": False,
                "amount": "-48.83",
                "currency": "EUR",
            },
            {
                "id": 102,
                "account_id": 1,
                "status": "vorgemerkt",
                "booking_date": "2026-11-25",
                "value_date": None,
                "counterparty": "Arbeitgeber AG",
                "counterparty_iban": None,
                "purpose": "Gehalt",
                "note": None,
                "checked": False,
                "checked_at": None,
                "checked_origin": None,
                "reconciled": False,
                "reconciled_at": None,
                "reconciled_origin": None,
                "origin": "csv",
                "external_ref": None,
                "fingerprint": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
                "occurrence_index": 1,
                "merged": False,
                "amount": "2500.00",
                "currency": "EUR",
            },
        ],
    }


def test_eigenes_format_wird_erkannt() -> None:
    """F-IMP-04: Der Kopf format='finanzuebersicht' kennzeichnet das eigene Format."""
    assert detect_own_format(_payload()) is True
    assert detect_own_format(json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))) is True


@pytest.mark.parametrize(
    "foreign",
    [
        {"transactions": []},
        {"format": "anderes-produkt", "transactions": []},
        {"format": None},
        [],
        "kein objekt",
    ],
)
def test_fremdes_json_wird_nicht_als_eigenes_format_erkannt(foreign: object) -> None:
    """F-IMP-04: Fremdes JSON ohne passenden Kopf wird nicht als eigenes Format erkannt."""
    assert detect_own_format(foreign) is False


def test_eigenes_json_v1_wird_ohne_mapping_geparst() -> None:
    """F-IMP-04: Buchungen werden direkt gelesen, Betraege exakt in Cent umgerechnet."""
    transactions = parse_own_json(_payload())

    assert len(transactions) == 2
    assert all(isinstance(transaction, TransactionData) for transaction in transactions)

    first = transactions[0]
    assert first.account_id == 1
    assert first.status == "gebucht"
    assert first.booking_date == date(2026, 1, 15)
    assert first.value_date == date(2026, 1, 16)
    assert first.amount_cents == -4883
    assert first.currency == "EUR"
    assert first.counterparty == "Beispiel Markt GmbH"
    assert first.purpose == "Einkauf"
    assert first.note == "Wocheneinkauf"
    assert first.checked is True
    assert first.checked_at == datetime.fromisoformat("2026-01-17T08:00:00+00:00")
    assert first.checked_origin == "ui"
    assert first.reconciled is True
    assert first.reconciled_origin == "import"
    assert first.origin == "fints"
    assert first.external_ref == "ACTUAL-2026-0001"
    assert first.occurrence_index == 1
    assert first.merged is False

    second = transactions[1]
    assert second.amount_cents == 250000
    assert second.status == "vorgemerkt"
    assert second.value_date is None
    assert second.checked is False
    assert second.checked_at is None
    assert second.reconciled is False
    assert second.reconciled_at is None


def test_beispieldatei_wird_geparst() -> None:
    """F-EXP-07: Die mitgelieferte v1-Testdatei ist ohne Mapping importierbar."""
    payload = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    transactions = parse_own_json(payload)

    assert [transaction.amount_cents for transaction in transactions] == [-4883, 250000]
    assert [transaction.account_id for transaction in transactions] == [1, 2]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("-48.83", -4883),
        ("2500.00", 250000),
        ("0.00", 0),
        ("-0.01", -1),
        ("999999999.99", 99999999999),
    ],
)
def test_betrag_dezimal_string_zu_cent(value: str, expected: int) -> None:
    """N-DB-02: Dezimal-Strings werden ohne Gleitkomma-Rundung in Cent umgerechnet."""
    assert amount_string_to_cents(value) == expected


def test_betrag_mit_drei_nachkommastellen_wird_abgelehnt() -> None:
    """N-DB-02: Mehr als zwei Nachkommastellen sind kein gueltiger Centbetrag."""
    with pytest.raises(ImportFormatError, match="Nachkommastellen"):
        amount_string_to_cents("12.345")


def test_fehlerhafte_instanz_erzeugt_klare_meldung() -> None:
    """F-IMP-04: Ein Schemafehler nennt Feldpfad und Ursache im Klartext."""
    payload = _payload()
    payload["transactions"][0]["amount"] = "12,34"

    with pytest.raises(ImportFormatError) as excinfo:
        parse_own_json(payload)

    message = str(excinfo.value)
    assert "transactions.0.amount" in message
    assert "Ungueltige JSON-Importdaten" in message


def test_fehlendes_pflichtfeld_erzeugt_klare_meldung() -> None:
    """F-IMP-04: Ein fehlendes Pflichtfeld wird mit Feldpfad gemeldet."""
    payload = _payload()
    del payload["transactions"][0]["amount"]

    with pytest.raises(ImportFormatError) as excinfo:
        parse_own_json(payload)

    message = str(excinfo.value)
    assert "transactions.0.amount" in message
    assert "Field required" in message


def test_fremdes_json_wird_beim_parsen_abgelehnt() -> None:
    """F-IMP-04: Ohne eigenen Kopf wird nicht importiert."""
    with pytest.raises(ImportFormatError, match="Kein eigenes JSON-Format"):
        parse_own_json({"transactions": []})


def test_nicht_objekt_wurzel_wird_abgelehnt() -> None:
    """F-IMP-04: Die Wurzel muss ein JSON-Objekt sein."""
    with pytest.raises(ImportFormatError, match="JSON-Objekt"):
        parse_own_json([])  # type: ignore[arg-type]


def test_fehlende_version_wird_abgelehnt() -> None:
    """F-EXP-07: Ein Dokument ohne version ist nicht importierbar."""
    payload = _payload()
    del payload["version"]

    with pytest.raises(ImportFormatError, match="version"):
        parse_own_json(payload)


@pytest.mark.parametrize("version", ["1", 1.0, True])
def test_version_muss_ganzzahl_sein(version: object) -> None:
    """F-EXP-07: Eine String-, Float- oder Boolean-Version ist ungueltig."""
    payload = _payload()
    payload["version"] = version

    with pytest.raises(ImportFormatError, match="ganze Zahl"):
        parse_own_json(payload)


def test_neuere_version_wird_abgelehnt() -> None:
    """F-EXP-07: Eine neuere Version als die App wird mit klarer Meldung abgelehnt."""
    payload = _payload()
    payload["version"] = 99

    with pytest.raises(ValueError) as excinfo:
        parse_own_json(payload)

    message = str(excinfo.value)
    assert "99" in message
    assert "neuer" in message or "Version" in message


def test_versionskonvertierung_wird_delegiert(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-EXP-07: Abweichende Versionen werden an T-052/export_version delegiert, sofern vorhanden."""
    upgraded = _payload()
    calls: list[dict[str, Any]] = []

    module = ModuleType("finue.services.export_version")

    def upgrade_to_current(data: dict[str, Any]) -> dict[str, Any]:
        calls.append(data)
        return upgraded

    module.upgrade_to_current = upgrade_to_current  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "finue.services.export_version", module)

    payload = _payload()
    payload["version"] = 2

    transactions = parse_own_json(payload)

    assert calls and calls[0]["version"] == 2
    assert [transaction.amount_cents for transaction in transactions] == [-4883, 250000]
