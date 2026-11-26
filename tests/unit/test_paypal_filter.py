"""Tests fuer den PayPal-Hilfszeilen-Filter (F-IMP-08).

Die Muster sind unbelegt, nach bestem Wissen (echte PayPal-Beispieldatei vom
Owner offen). Die Tests sichern das vereinbarte Verhalten: abgeschlossene und
unbekannte Status bleiben, bekannte nicht abgeschlossene Status und die
Hilfszeilen Waehrungsumrechnung/Einbehalt/Bankeinzug zur Deckung entfallen.
"""

from __future__ import annotations

from typing import Any

import pytest

from finue.services.paypal_filter import (
    PAYPAL_PRE_FILTER,
    PRE_FILTERS,
    filter_paypal_rows,
)


def _row(status: Any = "Completed", type_: Any = "Zahlung", description: Any = "", **extra: Any) -> dict[str, Any]:
    """Baut eine PayPal-Zeile im Format des deutschen Exports."""
    row: dict[str, Any] = {
        "Datum": "01.02.2026",
        "Name": "Beispiel",
        "Typ": type_,
        "Status": status,
        "Währung": "EUR",
        "Brutto": "-12,34",
        "Transaktionsnummer": "TX-1",
    }
    if description != "":
        row["Beschreibung"] = description
    row.update(extra)
    return row


def test_completed_payment_is_kept_in_both_languages() -> None:
    rows = [
        _row(status="Completed"),
        _row(status="Abgeschlossen"),
        _row(status="  abgeschlossen  "),
    ]

    assert filter_paypal_rows(rows, "Status") == rows


def test_currency_conversion_helper_row_is_removed() -> None:
    rows = [
        _row(status="Completed", type_="Zahlung"),
        _row(status="Abgeschlossen", type_="Währungsumrechnung"),
        _row(status="Completed", type_="Currency Conversion"),
    ]

    result = filter_paypal_rows(rows, "Status")

    assert result == [rows[0]]
    assert len(rows) == 3  # Eingabe bleibt unveraendert


def test_hold_helper_row_is_removed() -> None:
    rows = [
        _row(status="Completed", type_="Zahlung"),
        _row(status="Completed", type_="Vorübergehender Einbehalt"),
        _row(status="Completed", type_="Temporary Hold"),
        _row(status="Completed", type_="Payment Hold"),
    ]

    assert filter_paypal_rows(rows, "Status") == [rows[0]]


def test_bank_debit_to_cover_helper_row_is_removed() -> None:
    rows = [
        _row(status="Completed", type_="Zahlung"),
        _row(status="Completed", type_="Bankeinzug zur Deckung"),
        _row(status="Completed", type_="Bank Debit to Cover"),
    ]

    assert filter_paypal_rows(rows, "Status") == [rows[0]]


def test_bank_debit_to_cover_is_recognised_in_description_field() -> None:
    helper = _row(status="Completed", type_="Zahlung", description="Bankeinzug zur Deckung")
    payment = _row(status="Completed", type_="Zahlung", description="Zahlung an Online-Shop")

    assert filter_paypal_rows([helper, payment], "Status") == [payment]


def test_description_mentioning_helper_word_does_not_remove_real_payment() -> None:
    payment = _row(status="Completed", type_="Zahlung", description="Zahlung: Währungsumrechnung erklärt")

    assert filter_paypal_rows([payment], "Status") == [payment]


@pytest.mark.parametrize(
    "status",
    ["Pending", "Ausstehend", "Refunded", "Erstattet", "Denied", "Abgelehnt", "Cancelled", "Storniert"],
)
def test_known_incomplete_status_is_removed(status: str) -> None:
    rows = [_row(status=status), _row(status="Completed")]

    assert filter_paypal_rows(rows, "Status") == [rows[1]]


def test_unknown_status_is_kept_conservatively() -> None:
    rows = [
        _row(status="Irgendein neuer Status"),
        _row(status=""),
        _row(status=None),
        _row(status=42),
    ]

    assert filter_paypal_rows(rows, "Status") == rows


def test_missing_status_field_is_treated_as_unknown() -> None:
    row = {"Datum": "01.02.2026", "Typ": "Zahlung", "Brutto": "-12,34"}

    assert filter_paypal_rows([row], "Status") == [row]


def test_status_field_name_is_matched_case_insensitively() -> None:
    rows = [_row(status="Pending"), _row(status="Completed")]

    assert filter_paypal_rows(rows, "status") == [rows[1]]


def test_helper_row_is_removed_even_before_status_check() -> None:
    helper = _row(status="Unbekannt", type_="Währungsumrechnung")
    payment = _row(status="Unbekannt", type_="Zahlung")

    assert filter_paypal_rows([helper, payment], "Status") == [payment]


def test_pre_filter_registry_exposes_paypal_filter() -> None:
    rows = [_row(status="Completed"), _row(status="Completed", type_="Währungsumrechnung")]

    assert PRE_FILTERS[PAYPAL_PRE_FILTER] is filter_paypal_rows
    assert PRE_FILTERS[PAYPAL_PRE_FILTER](rows, "Status") == [rows[0]]


def test_empty_status_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        filter_paypal_rows([], " ")


def test_non_mapping_row_is_rejected() -> None:
    bad_rows: Any = [["keine", "Zeile"]]

    with pytest.raises(TypeError):
        filter_paypal_rows(bad_rows, "Status")
