"""Tests fuer die CSV-Formaterkennung (F-IMP-02)."""

from __future__ import annotations

import pytest

from finue.services.csv_detect import CsvFormatGuess, detect_csv_format

_UTF8_BOM = b"\xef\xbb\xbf"


def _encode(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


def test_utf8_without_bom_semicolon_german_decimal_and_date() -> None:
    raw = _encode("Datum;Verwendungszweck;Betrag\n01.02.2026;Miete;-1.234,56\n02.02.2026;Gehalt;2.500,00\n")

    guess = detect_csv_format(raw)

    assert guess == CsvFormatGuess(
        encoding="utf-8",
        delimiter=";",
        decimal_separator="comma",
        date_format="TT.MM.JJJJ",
        has_header=True,
        skip_rows=0,
    )


def test_utf8_with_bom_is_detected_as_utf8_sig() -> None:
    text = "Datum;Betrag\n01.02.2026;1.234,56\n02.02.2026;2.500,00\n"

    guess = detect_csv_format(_UTF8_BOM + _encode(text))

    assert guess.encoding == "utf-8-sig"
    assert guess.delimiter == ";"
    assert guess.decimal_separator == "comma"
    assert guess.date_format == "TT.MM.JJJJ"
    assert guess.has_header is True
    assert guess.skip_rows == 0


def test_cp1252_with_umlauts() -> None:
    raw = _encode(
        "Datum;Beschreibung;Betrag\n03.02.2026;Bäckerei Müller;-12,50\n04.02.2026;Getränke;18,90\n",
        encoding="cp1252",
    )

    guess = detect_csv_format(raw)

    assert guess.encoding == "cp1252"
    assert guess.delimiter == ";"
    assert guess.decimal_separator == "comma"
    assert guess.date_format == "TT.MM.JJJJ"
    assert guess.has_header is True


def test_comma_delimiter_with_english_decimal_and_iso_date() -> None:
    raw = _encode('Date,Description,Amount\n2026-02-01,Coffee,"-1,234.56"\n2026-02-02,Salary,"2,500.00"\n')

    guess = detect_csv_format(raw)

    assert guess.delimiter == ","
    assert guess.decimal_separator == "point"
    assert guess.date_format == "JJJJ-MM-TT"
    assert guess.has_header is True


def test_tab_delimiter_with_mm_dd_yyyy_date() -> None:
    raw = _encode("Date\tDescription\tAmount\n01/31/2026\tCoffee\t-12.50\n02/01/2026\tSalary\t2500.00\n")

    guess = detect_csv_format(raw)

    assert guess.delimiter == "\t"
    assert guess.decimal_separator == "point"
    assert guess.date_format == "MM/TT/JJJJ"
    assert guess.has_header is True


def test_two_preamble_rows_before_header_are_skipped() -> None:
    raw = _encode(
        "Kontoauszug Girokonto\n"
        "Zeitraum: 01.01.2026 - 31.01.2026\n"
        "Datum;Verwendungszweck;Betrag\n"
        "01.02.2026;Miete;-1.234,56\n"
        "02.02.2026;Gehalt;2.500,00\n"
    )

    guess = detect_csv_format(raw)

    assert guess.has_header is True
    assert guess.skip_rows == 2
    assert guess.delimiter == ";"
    assert guess.decimal_separator == "comma"
    assert guess.date_format == "TT.MM.JJJJ"


def test_file_without_header() -> None:
    raw = _encode("01.02.2026;Miete;-1.234,56\n02.02.2026;Gehalt;2.500,00\n")

    guess = detect_csv_format(raw)

    assert guess.has_header is False
    assert guess.skip_rows == 0
    assert guess.decimal_separator == "comma"
    assert guess.date_format == "TT.MM.JJJJ"


@pytest.mark.parametrize(
    ("date_value", "expected"),
    [
        ("01.02.2026", "TT.MM.JJJJ"),
        ("2026-02-01", "JJJJ-MM-TT"),
        ("01/31/2026", "MM/TT/JJJJ"),
    ],
)
def test_all_supported_date_formats(date_value: str, expected: str) -> None:
    raw = _encode(f"Datum;Beschreibung;Betrag\n{date_value};Test;-12,50\n")

    guess = detect_csv_format(raw)

    assert guess.date_format == expected


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("-1.234,56", "comma"),
        ("-1,234.56", "point"),
        ("-12,50", "comma"),
        ("-12.50", "point"),
    ],
)
def test_german_and_english_decimal_formats(amount: str, expected: str) -> None:
    raw = _encode(f"Datum;Beschreibung;Betrag\n01.02.2026;Test;{amount}\n02.02.2026;Test2;{amount}\n")

    guess = detect_csv_format(raw)

    assert guess.decimal_separator == expected


def test_manual_overrides_win_over_detection() -> None:
    raw = _encode("Datum;Betrag\n01.02.2026;1.234,56\n")

    guess = detect_csv_format(
        raw,
        encoding="utf-8-sig",
        delimiter="\t",
        decimal_separator="point",
        date_format="JJJJ-MM-TT",
        has_header=False,
        skip_rows=1,
    )

    assert guess == CsvFormatGuess(
        encoding="utf-8-sig",
        delimiter="\t",
        decimal_separator="point",
        date_format="JJJJ-MM-TT",
        has_header=False,
        skip_rows=1,
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"encoding": "latin-1"}, "Kodierung"),
        ({"delimiter": "|"}, "Trennzeichen"),
        ({"decimal_separator": "dot"}, "Dezimalformat"),
        ({"date_format": "YYYY-MM-DD"}, "Datumsformat"),
        ({"skip_rows": -1}, "skip_rows"),
    ],
)
def test_unsupported_overrides_are_rejected(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        detect_csv_format(_encode("Datum;Betrag\n01.02.2026;1.234,56\n"), **kwargs)
