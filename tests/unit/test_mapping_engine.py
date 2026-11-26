"""Tests fuer die Mapping-Engine (F-IMP-03, N-DB-02)."""

from __future__ import annotations

from datetime import date

import pytest

from finue.services.csv_detect import CsvFormatGuess
from finue.services.mapping import MappingError, TransactionData, apply_mapping

_CSV_OPTS = {"decimal_separator": "comma", "date_format": "TT.MM.JJJJ"}


def test_simple_1_to_1_mapping_german_csv() -> None:
    rows = [
        {
            "Datum": "01.02.2026",
            "Wertstellung": "02.02.2026",
            "Betrag": "-1.234,56",
            "Waehrung": "EUR",
            "Empfaenger": "Max Mustermann",
            "IBAN": "DE89370400440532013000",
            "Verwendungszweck": "Miete Februar",
        }
    ]
    mapping = {
        "Datum": "booking_date",
        "Wertstellung": "value_date",
        "Betrag": "amount_cents",
        "Waehrung": "currency",
        "Empfaenger": "counterparty",
        "IBAN": "counterparty_iban",
        "Verwendungszweck": "purpose",
    }

    result = apply_mapping(rows, mapping, _CSV_OPTS)

    assert result == [
        TransactionData(
            booking_date=date(2026, 2, 1),
            value_date=date(2026, 2, 2),
            amount_cents=-123456,
            currency="EUR",
            counterparty="Max Mustermann",
            counterparty_iban="DE89370400440532013000",
            purpose="Miete Februar",
        )
    ]
    assert isinstance(result[0].amount_cents, int)


def test_target_to_source_mapping_with_csv_format_guess() -> None:
    guess = CsvFormatGuess(
        encoding="utf-8",
        delimiter=";",
        decimal_separator="comma",
        date_format="TT.MM.JJJJ",
        has_header=True,
        skip_rows=0,
    )
    rows = [{"Buchungstag": "05.03.2026", "Umsatz": "12,50"}]
    mapping = {"booking_date": "Buchungstag", "amount_cents": "Umsatz"}

    result = apply_mapping(rows, mapping, guess)

    assert result[0].booking_date == date(2026, 3, 5)
    assert result[0].amount_cents == 1250
    assert result[0].currency == "EUR"
    assert result[0].value_date is None
    assert result[0].counterparty is None


def test_soll_haben_columns_set_sign() -> None:
    rows = [
        {"Datum": "01.02.2026", "Soll": "12,50", "Haben": ""},
        {"Datum": "02.02.2026", "Soll": None, "Haben": "1.000,00"},
    ]
    mapping = {
        "Datum": "booking_date",
        "amount_cents": {"soll": "Soll", "haben": "Haben"},
    }

    result = apply_mapping(rows, mapping, _CSV_OPTS)

    assert [entry.amount_cents for entry in result] == [-1250, 100000]


def test_soll_and_haben_filled_in_same_row_is_rejected_with_row_number() -> None:
    mapping = {
        "Datum": "booking_date",
        "amount_cents": {"soll": "Soll", "haben": "Haben"},
    }

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([{"Datum": "01.02.2026", "Soll": "1,00", "Haben": "2,00"}], mapping, _CSV_OPTS)

    assert excinfo.value.row_no == 1
    assert "Zeile 1" in str(excinfo.value)
    assert "Soll" in str(excinfo.value)
    assert "Haben" in str(excinfo.value)


def test_empty_soll_and_haben_cells_are_a_missing_required_field() -> None:
    mapping = {
        "Datum": "booking_date",
        "amount_cents": {"soll": "Soll", "haben": "Haben"},
    }

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([{"Datum": "01.02.2026", "Soll": "", "Haben": " "}], mapping, _CSV_OPTS)

    assert "Zeile 1" in str(excinfo.value)
    assert "amount_cents" in str(excinfo.value)


def test_invert_sign_for_credit_card_single_amount() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents", "invert_sign": True}

    result = apply_mapping([{"Datum": "03.02.2026", "Betrag": "48,83"}], mapping, _CSV_OPTS)

    assert result[0].amount_cents == -4883


def test_invert_sign_reverses_soll_and_haben_signs() -> None:
    mapping = {
        "Datum": "booking_date",
        "amount_cents": {"soll": "Soll", "haben": "Haben"},
        "invert_sign": True,
    }
    rows = [
        {"Datum": "01.02.2026", "Soll": "10,00", "Haben": ""},
        {"Datum": "02.02.2026", "Soll": "", "Haben": "10,00"},
    ]

    result = apply_mapping(rows, mapping, _CSV_OPTS)

    assert [entry.amount_cents for entry in result] == [1000, -1000]


def test_json_nested_dot_paths() -> None:
    rows = [
        {
            "booking": {"date": "2026-02-01"},
            "amount": {"value": "-48.83", "currency": "EUR"},
            "counterparty": {"name": "Online Shop", "iban": "DE02120300000000202051"},
            "details": {"reference": "Rechnung 42"},
        }
    ]
    mapping = {
        "booking_date": "booking.date",
        "amount_cents": "amount.value",
        "currency": "amount.currency",
        "counterparty": "counterparty.name",
        "counterparty_iban": "counterparty.iban",
        "purpose": "details.reference",
    }

    result = apply_mapping(
        rows,
        mapping,
        {"kind": "json", "decimal_separator": "point", "date_format": "JJJJ-MM-TT"},
    )

    assert result == [
        TransactionData(
            booking_date=date(2026, 2, 1),
            value_date=None,
            amount_cents=-4883,
            currency="EUR",
            counterparty="Online Shop",
            counterparty_iban="DE02120300000000202051",
            purpose="Rechnung 42",
        )
    ]


def test_literal_column_with_dot_wins_over_nested_path() -> None:
    rows = [{"Datum": "01.02.2026", "amount.value": "1,00", "amount": {"value": "9,99"}}]
    mapping = {"Datum": "booking_date", "amount.value": "amount_cents"}

    result = apply_mapping(rows, mapping, _CSV_OPTS)

    assert result[0].amount_cents == 100


def test_structured_mapping_with_fields_and_amount_option() -> None:
    mapping = {
        "fields": {"Datum": "booking_date", "Waehrung": "currency"},
        "amount": {"soll": "Soll", "haben": "Haben"},
        "currency_default": "CHF",
    }
    rows = [{"Datum": "01.02.2026", "Soll": "", "Haben": "5,00", "Waehrung": ""}]

    result = apply_mapping(rows, mapping, _CSV_OPTS)

    assert result[0].amount_cents == 500
    assert result[0].currency == "CHF"


def test_missing_required_field_reports_row_number() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([{"Betrag": "10,00"}], mapping, _CSV_OPTS)

    assert excinfo.value.row_no == 1
    assert "Zeile 1" in str(excinfo.value)
    assert "booking_date" in str(excinfo.value)


def test_missing_amount_in_second_row_reports_row_two() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}
    rows = [
        {"Datum": "01.02.2026", "Betrag": "10,00"},
        {"Datum": "02.02.2026", "Betrag": ""},
    ]

    with pytest.raises(MappingError) as excinfo:
        apply_mapping(rows, mapping, _CSV_OPTS)

    assert excinfo.value.row_no == 2
    assert "Zeile 2" in str(excinfo.value)
    assert "amount_cents" in str(excinfo.value)


def test_invalid_date_reports_row_and_expected_format() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([{"Datum": "32.13.2026", "Betrag": "1,00"}], mapping, _CSV_OPTS)

    assert "Zeile 1" in str(excinfo.value)
    assert "TT.MM.JJJJ" in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw", "separator", "expected"),
    [
        ("1.234,56", "comma", 123456),
        ("-1.234,56", "comma", -123456),
        ("1,234.56", "point", 123456),
        ("-0.07", "point", -7),
        ("0,07", "comma", 7),
        ("12,50-", "comma", -1250),
        ("(12,50)", "comma", -1250),
        ("€ 12,50", "comma", 1250),
        ("1234", "comma", 123400),
        ("+5,00", "comma", 500),
    ],
)
def test_amount_parsing_variants(raw: str, separator: str, expected: int) -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}
    options = {"decimal_separator": separator, "date_format": "TT.MM.JJJJ"}

    result = apply_mapping([{"Datum": "01.02.2026", "Betrag": raw}], mapping, options)

    assert result[0].amount_cents == expected
    assert isinstance(result[0].amount_cents, int)


def test_amount_rounds_half_up_to_cents() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}

    result = apply_mapping(
        [{"Datum": "01.02.2026", "Betrag": "1,005"}],
        mapping,
        {"decimal_separator": "comma", "date_format": "TT.MM.JJJJ"},
    )

    assert result[0].amount_cents == 101


@pytest.mark.parametrize(
    ("raw", "date_format", "expected"),
    [
        ("01.02.2026", "TT.MM.JJJJ", date(2026, 2, 1)),
        ("2026-02-01", "JJJJ-MM-TT", date(2026, 2, 1)),
        ("02/01/2026", "MM/TT/JJJJ", date(2026, 2, 1)),
        ("01/02/2026", "TT/MM/JJJJ", date(2026, 2, 1)),
    ],
)
def test_date_parsing_per_format(raw: str, date_format: str, expected: date) -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}
    options = {"decimal_separator": "comma", "date_format": date_format}

    result = apply_mapping([{"Datum": raw, "Betrag": "1,00"}], mapping, options)

    assert result[0].booking_date == expected


def test_date_format_none_tries_supported_formats() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}

    result = apply_mapping(
        [{"Datum": "2026-02-01", "Betrag": "1.00"}],
        mapping,
        {"decimal_separator": "point"},
    )

    assert result[0].booking_date == date(2026, 2, 1)


def test_value_date_is_optional_but_invalid_value_is_reported() -> None:
    mapping = {"Datum": "booking_date", "Wert": "value_date", "Betrag": "amount_cents"}

    empty = apply_mapping([{"Datum": "01.02.2026", "Wert": "", "Betrag": "1,00"}], mapping, _CSV_OPTS)
    assert empty[0].value_date is None

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([{"Datum": "01.02.2026", "Wert": "31.02.2026", "Betrag": "1,00"}], mapping, _CSV_OPTS)
    assert "Zeile 1" in str(excinfo.value)
    assert "value_date" in str(excinfo.value)


def test_currency_default_and_validation() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}

    default = apply_mapping([{"Datum": "01.02.2026", "Betrag": "1,00"}], mapping, _CSV_OPTS)
    assert default[0].currency == "EUR"

    mapped = apply_mapping(
        [{"Datum": "01.02.2026", "Betrag": "1,00", "Whg": "usd"}],
        {**mapping, "Whg": "currency"},
        _CSV_OPTS,
    )
    assert mapped[0].currency == "USD"

    with pytest.raises(MappingError) as excinfo:
        apply_mapping(
            [{"Datum": "01.02.2026", "Betrag": "1,00", "Whg": "Euro"}],
            {**mapping, "Whg": "currency"},
            _CSV_OPTS,
        )
    assert "Zeile 1" in str(excinfo.value)
    assert excinfo.value.field == "currency"
    assert "Waehrung" in str(excinfo.value)


def test_unknown_target_is_rejected() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents", "Kategorie": "category"}

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([], mapping, _CSV_OPTS)

    assert "category" in str(excinfo.value)
    assert "Erlaubte Zielfelder" in str(excinfo.value)


def test_duplicate_target_is_rejected() -> None:
    mapping = {"Datum": "booking_date", "Buchungstag": "booking_date", "Betrag": "amount_cents"}

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([], mapping, _CSV_OPTS)

    assert "booking_date" in str(excinfo.value)
    assert "mehrfach" in str(excinfo.value)


def test_missing_amount_mapping_is_rejected() -> None:
    with pytest.raises(MappingError) as excinfo:
        apply_mapping([], {"Datum": "booking_date"}, _CSV_OPTS)

    assert "Kein Betrag" in str(excinfo.value)


def test_blank_rows_are_skipped_and_row_offset_shifts_errors() -> None:
    mapping = {"Datum": "booking_date", "Betrag": "amount_cents"}
    options = {**_CSV_OPTS, "row_offset": 10}
    rows = [{}, {"Datum": "", "Betrag": ""}, {"Datum": "01.02.2026", "Betrag": "1,00"}]

    result = apply_mapping(rows, mapping, options)

    assert len(result) == 1
    assert result[0].booking_date == date(2026, 2, 1)

    with pytest.raises(MappingError) as excinfo:
        apply_mapping([{"Datum": "01.02.2026", "Betrag": "keine Zahl"}], mapping, options)
    assert excinfo.value.row_no == 11
    assert "Zeile 11" in str(excinfo.value)
