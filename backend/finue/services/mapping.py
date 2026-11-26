"""Mapping-Engine fuer den Datei-Import (F-IMP-03).

Bildet Zeilen aus einer CSV-Datei oder aus fremdem JSON auf Buchungsfelder ab.
Quellfelder sind bei CSV die Spaltennamen, bei JSON die Schluessel der Objekte
in einer Liste. Verschachtelte JSON-Schluessel werden per Punkt-Pfad
aufgeloest (z. B. ``amount.value``); ein exakt vorhandener Spaltenname mit Punkt
gewinnt vor der Pfadaufloesung.

Unterstuetzt werden:

- ein einfaches 1:1-Mapping Quellfeld -> Buchungsfeld,
- getrennte Soll/Haben-Spalten (je Zeile ist genau eine gefuellt; Soll ergibt
  einen negativen, Haben einen positiven Betrag),
- Vorzeichenumkehr als Mapping-Option (Kreditkartenabrechnung),
- Betragsparsing nach erkanntem Dezimalformat, Datumsparsing nach erkanntem
  Format,
- Betraege als ganze Centzahl (N-DB-02).

Nicht erlaubte Zielfelder (z. B. ``category``) werden abgelehnt. Damit bleiben
die Nicht-Ziele aus SPEC Abschnitt 1 auch im Import geschlossen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from finue.services.csv_detect import SUPPORTED_DATE_FORMATS, CsvFormatGuess

__all__ = ["MappingError", "TARGET_FIELDS", "TransactionData", "apply_mapping"]

#: Erlaubte Buchungsfelder als Mapping-Ziel (F-IMP-03).
TARGET_FIELDS: tuple[str, ...] = (
    "booking_date",
    "value_date",
    "amount_cents",
    "currency",
    "counterparty",
    "counterparty_iban",
    "purpose",
)

_TARGETS = frozenset(TARGET_FIELDS)
_AMOUNT_TARGET = "amount_cents"
_FIELD_OPTIONS = frozenset(
    {
        "amount",
        "invert_sign",
        "currency_default",
        "kind",
        "row_offset",
        "decimal_separator",
        "date_format",
    }
)
_DECIMAL_SEPARATORS = ("comma", "point")
_KINDS = ("csv", "json")
_CENT = Decimal("0.01")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_CURRENCY_WORDS = re.compile(r"(?i)(?:eur|usd|gbp|chf)")
_AMOUNT_TEXT_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")

_DATE_PATTERNS: dict[str, re.Pattern[str]] = {
    "TT.MM.JJJJ": re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$"),
    "JJJJ-MM-TT": re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    "MM/TT/JJJJ": re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"),
    "TT/MM/JJJJ": re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"),
}

_MISSING = object()


class MappingError(ValueError):
    """Fehler beim Mapping einer Import-Zeile.

    Die Meldung beginnt mit ``Zeile <n>:``, sobald eine Zeilennummer bekannt
    ist. ``field`` nennt das betroffene Buchungsfeld, ``row_no`` die
    Zeilennummer (1-basiert, inklusive ``row_offset``).
    """

    def __init__(self, message: str, *, row_no: int | None = None, field: str | None = None) -> None:
        self.row_no = row_no
        self.field = field
        prefix = f"Zeile {row_no}: " if row_no is not None else ""
        super().__init__(prefix + message)


@dataclass(frozen=True, slots=True)
class TransactionData:
    """Eine gemappte Buchung als Zwischenergebnis vor der Dublettenpruefung.

    Betraege sind ganzzahlige Cent (N-DB-02); ``booking_date`` ist ein
    Kalenderdatum. Optionale Felder sind ``None``, wenn die Quelle sie nicht
    liefert oder die Zelle leer ist.
    """

    booking_date: date
    value_date: date | None
    amount_cents: int
    currency: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None


@dataclass(frozen=True, slots=True)
class _MappingSpec:
    """Normalisiertes Mapping: Buchungsfeld -> Quellpfad plus Optionen."""

    fields: Mapping[str, str]
    amount_path: str | None
    soll_path: str | None
    haben_path: str | None
    decimal_separator: str | None
    date_format: str | None
    invert_sign: bool
    currency_default: str
    kind: str | None
    row_offset: int


def apply_mapping(
    rows: Iterable[Mapping[str, Any]],
    mapping: Mapping[str, Any],
    format_opts: object = None,
) -> list[TransactionData]:
    """Wendet ein Mapping auf Zeilen an und liefert Buchungsdaten.

    Args:
        rows: Zeilen als Woerterbuch. Bei CSV sind die Schluessel die
            Spaltennamen, bei JSON die Objektschluessel. Vollstaendig leere
            Zeilen werden uebersprungen.
        mapping: Zuordnung Quellfeld -> Buchungsfeld. Zulaessig sind die flache
            Form ``{"Datum": "booking_date"}`` und die invertierte Form
            ``{"booking_date": "Datum"}``. Getrennte Soll/Haben-Spalten werden
            ueber ``{"amount_cents": {"soll": "Soll", "haben": "Haben"}}`` oder
            ueber ``{"fields": {...}, "amount": {...}}`` abgebildet.
            Optionen: ``invert_sign`` (bool), ``currency_default``
            (ISO-Code, Standard ``EUR``), ``kind`` (``csv``/``json``),
            ``row_offset`` (nicht negative Ganzzahl), ``decimal_separator``
            (``comma``/``point``) und ``date_format``.
        format_opts: Optional ein :class:`finue.services.csv_detect.CsvFormatGuess`
            aus der Formaterkennung (F-IMP-02) oder ein Mapping mit denselben
            Optionsnamen. Explizite Mapping-Optionen haben Vorrang.

    Returns:
        Liste von :class:`TransactionData` in Eingabereihenfolge.

    Raises:
        MappingError: Bei ungueltigem Mapping, fehlenden Pflichtfeldern oder
            unlesbaren Datums-/Betragswerten; die Meldung enthaelt die
            Zeilennummer.
    """
    spec = _normalise_mapping(mapping, format_opts)
    result: list[TransactionData] = []

    for index, row in enumerate(rows, start=1):
        row_no = index + spec.row_offset
        if not isinstance(row, Mapping):
            raise MappingError(f"Zeile {row_no}: Zeile ist kein Objekt.", row_no=row_no)
        if _is_blank_row(row):
            continue
        result.append(_map_row(row, spec, row_no))

    return result


def _normalise_mapping(mapping: Mapping[str, Any], format_opts: object) -> _MappingSpec:
    """Prueft und normalisiert das Mapping zu einer :class:`_MappingSpec`."""
    if not isinstance(mapping, Mapping):
        raise MappingError("Mapping muss ein Objekt sein.")

    data = dict(mapping)
    nested_fields = data.pop("fields", _MISSING)
    if nested_fields is not _MISSING:
        if not isinstance(nested_fields, Mapping):
            raise MappingError("'fields' muss ein Objekt mit Mapping-Eintraegen sein.")
        fields_raw = dict(nested_fields)
        options: dict[str, Any] = data
    else:
        fields_raw = data
        options = {}
        for key in list(fields_raw):
            if key in _FIELD_OPTIONS and not _is_field_entry(fields_raw[key]):
                options[key] = fields_raw.pop(key)

    fields: dict[str, str] = {}
    amount_path: str | None = None
    amount_spec: Mapping[str, Any] | None = None

    for key, value in fields_raw.items():
        if key == _AMOUNT_TARGET and isinstance(value, Mapping):
            if amount_spec is not None:
                raise MappingError("Betrag ist mehrfach gemappt.")
            amount_spec = value
            continue

        entry = _entry_target(key, value)
        if entry is None:
            raise MappingError(
                f"Unbekannter Mapping-Eintrag {key!r} -> {value!r}. Erlaubte Zielfelder: {', '.join(TARGET_FIELDS)}."
            )

        target, source = entry
        if target == _AMOUNT_TARGET:
            if amount_path is not None or amount_spec is not None:
                raise MappingError("Betrag ist mehrfach gemappt.")
            amount_path = _checked_path(source, target)
            continue
        if target in fields:
            raise MappingError(f"Zielfeld {target!r} ist mehrfach gemappt.")
        fields[target] = _checked_path(source, target)

    option_amount = options.get("amount", _MISSING)
    if option_amount is not _MISSING:
        if not isinstance(option_amount, Mapping):
            raise MappingError("Option 'amount' muss ein Objekt mit 'soll' und 'haben' sein.")
        if amount_spec is not None or amount_path is not None:
            raise MappingError("Betrag ist mehrfach gemappt.")
        amount_spec = option_amount

    soll_path: str | None = None
    haben_path: str | None = None

    if amount_spec is not None:
        if amount_path is not None:
            raise MappingError("Betrag ist mehrfach gemappt.")
        unknown = set(amount_spec) - {"soll", "haben", "invert_sign"}
        if unknown:
            raise MappingError(f"Unbekannte Option(en) im Betrags-Mapping: {', '.join(sorted(unknown))}.")
        if "soll" in amount_spec:
            soll_path = _checked_path(amount_spec["soll"], "amount_cents.soll")
        if "haben" in amount_spec:
            haben_path = _checked_path(amount_spec["haben"], "amount_cents.haben")
        if (soll_path is None) != (haben_path is None):
            raise MappingError("Soll/Haben-Mapping braucht beide Quellfelder ('soll' und 'haben').")
        if soll_path is None and haben_path is None:
            raise MappingError("Soll/Haben-Mapping braucht mindestens ein Quellfeld.")
        if "invert_sign" in amount_spec:
            options = {**options, "invert_sign": amount_spec["invert_sign"]}

    if amount_path is None and soll_path is None and haben_path is None:
        raise MappingError("Kein Betrag gemappt ('amount_cents' oder Soll/Haben fehlt).")
    if "booking_date" not in fields:
        raise MappingError("Pflichtfeld 'booking_date' ist nicht gemappt.")

    return _MappingSpec(
        fields=fields,
        amount_path=amount_path,
        soll_path=soll_path,
        haben_path=haben_path,
        decimal_separator=_validated_choice(
            _option(options, format_opts, "decimal_separator"),
            _DECIMAL_SEPARATORS,
            "decimal_separator",
        ),
        date_format=_validated_choice(
            _option(options, format_opts, "date_format"),
            SUPPORTED_DATE_FORMATS,
            "date_format",
        ),
        invert_sign=_validated_bool(_option(options, format_opts, "invert_sign"), "invert_sign"),
        currency_default=_validated_currency_default(_option(options, format_opts, "currency_default")),
        kind=_validated_choice(_option(options, format_opts, "kind"), _KINDS, "kind"),
        row_offset=_validated_row_offset(_option(options, format_opts, "row_offset")),
    )


def _map_row(row: Mapping[str, Any], spec: _MappingSpec, row_no: int) -> TransactionData:
    """Bildet eine einzelne, nicht leere Zeile auf :class:`TransactionData` ab."""
    booking_date = _required_date(
        _lookup(row, spec.fields["booking_date"], spec.kind),
        spec.date_format,
        row_no=row_no,
        field="booking_date",
        source=spec.fields["booking_date"],
    )

    value_date: date | None = None
    if "value_date" in spec.fields:
        value_date = _parse_date_value(
            _lookup(row, spec.fields["value_date"], spec.kind),
            spec.date_format,
            row_no=row_no,
            field="value_date",
        )

    return TransactionData(
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=_row_amount_cents(row, spec, row_no),
        currency=_row_currency(row, spec, row_no),
        counterparty=_optional_text(row, spec, "counterparty"),
        counterparty_iban=_optional_text(row, spec, "counterparty_iban"),
        purpose=_optional_text(row, spec, "purpose"),
    )


def _row_amount_cents(row: Mapping[str, Any], spec: _MappingSpec, row_no: int) -> int:
    """Liest den Betrag als Centzahl, inklusive Soll/Haben und Vorzeichenumkehr."""
    if spec.soll_path is not None and spec.haben_path is not None:
        soll_raw = _lookup(row, spec.soll_path, spec.kind)
        haben_raw = _lookup(row, spec.haben_path, spec.kind)
        soll_filled = not _is_missing_or_blank(soll_raw)
        haben_filled = not _is_missing_or_blank(haben_raw)
        if soll_filled and haben_filled:
            raise MappingError(
                f"Betrag: Soll ({spec.soll_path!r}) und Haben ({spec.haben_path!r}) sind in derselben Zeile gefuellt.",
                row_no=row_no,
                field="amount_cents",
            )
        if not soll_filled and not haben_filled:
            raise MappingError(
                f"Pflichtfeld 'amount_cents' fehlt: Soll ({spec.soll_path!r}) "
                f"und Haben ({spec.haben_path!r}) sind leer.",
                row_no=row_no,
                field="amount_cents",
            )
        if soll_filled:
            cents = -abs(_parse_cents(soll_raw, spec.decimal_separator, row_no=row_no, field="amount_cents.soll"))
        else:
            cents = abs(_parse_cents(haben_raw, spec.decimal_separator, row_no=row_no, field="amount_cents.haben"))
    elif spec.amount_path is not None:
        raw = _lookup(row, spec.amount_path, spec.kind)
        if _is_missing_or_blank(raw):
            raise MappingError(
                f"Pflichtfeld 'amount_cents' fehlt (Quellfeld {spec.amount_path!r} nicht gefunden oder leer).",
                row_no=row_no,
                field="amount_cents",
            )
        cents = _parse_cents(raw, spec.decimal_separator, row_no=row_no, field="amount_cents")
    else:
        raise MappingError("Kein Betrag gemappt.", row_no=row_no, field="amount_cents")

    if spec.invert_sign:
        cents = -cents
    return cents


def _row_currency(row: Mapping[str, Any], spec: _MappingSpec, row_no: int) -> str:
    """Liest die Waehrung; fehlt sie, gilt ``currency_default``."""
    source = spec.fields.get("currency")
    if source is None:
        return spec.currency_default
    raw = _lookup(row, source, spec.kind)
    if _is_missing_or_blank(raw):
        return spec.currency_default
    text = str(raw).strip().upper()
    if not _CURRENCY_PATTERN.fullmatch(text):
        raise MappingError(
            f"Waehrung {raw!r} ist kein ISO-4217-Code aus drei Buchstaben.",
            row_no=row_no,
            field="currency",
        )
    return text


def _optional_text(row: Mapping[str, Any], spec: _MappingSpec, target: str) -> str | None:
    """Liest ein optionales Textfeld; fehlend oder leer ergibt ``None``."""
    source = spec.fields.get(target)
    if source is None:
        return None
    return _text_or_none(_lookup(row, source, spec.kind))


def _required_date(
    value: object,
    date_format: str | None,
    *,
    row_no: int,
    field: str,
    source: str,
) -> date:
    """Liest ein Pflichtdatum und meldet ein fehlendes Quellfeld mit Zeilennummer."""
    parsed = _parse_date_value(value, date_format, row_no=row_no, field=field)
    if parsed is None:
        raise MappingError(
            f"Pflichtfeld {field!r} fehlt (Quellfeld {source!r} nicht gefunden oder leer).",
            row_no=row_no,
            field=field,
        )
    return parsed


def _parse_date_value(
    value: object,
    date_format: str | None,
    *,
    row_no: int,
    field: str,
) -> date | None:
    """Parst ein Datum nach dem vorgegebenen oder einem unterstuetzten Format."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if _is_missing_or_blank(value):
        return None

    text = str(value).strip()
    formats = (date_format,) if date_format is not None else SUPPORTED_DATE_FORMATS
    for candidate in formats:
        parsed = _try_parse_date(text, candidate)
        if parsed is not None:
            return parsed

    expected = date_format or ", ".join(SUPPORTED_DATE_FORMATS)
    raise MappingError(
        f"{field}: Datum {text!r} passt nicht zum Format {expected}.",
        row_no=row_no,
        field=field,
    )


def _try_parse_date(text: str, date_format: str) -> date | None:
    """Wendet genau ein Datumsformat an und liefert ``None`` bei Abweichung."""
    match = _DATE_PATTERNS[date_format].fullmatch(text)
    if match is None:
        return None

    first, second, third = (int(group) for group in match.groups())
    if date_format == "JJJJ-MM-TT":
        year, month, day = first, second, third
    elif date_format == "TT.MM.JJJJ":
        day, month, year = first, second, third
    elif date_format == "MM/TT/JJJJ":
        month, day, year = first, second, third
    else:  # TT/MM/JJJJ
        day, month, year = first, second, third

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_cents(
    value: object,
    decimal_separator: str | None,
    *,
    row_no: int,
    field: str,
) -> int:
    """Wandelt einen Betrag in ganze Cent um (N-DB-02).

    Der Betrag wird als :class:`decimal.Decimal` gelesen und kaufmaennisch auf
    Cent gerundet; Fliesskommazahlen entstehen nicht.
    """
    decimal_value = _parse_decimal(value, decimal_separator, row_no=row_no, field=field)
    try:
        cents = (decimal_value.quantize(_CENT, rounding=ROUND_HALF_UP) * 100).to_integral_value()
    except InvalidOperation as exc:
        raise MappingError(
            f"{field}: Betrag {value!r} liegt ausserhalb des darstellbaren Bereichs.",
            row_no=row_no,
            field=field,
        ) from exc
    return int(cents)


def _parse_decimal(
    value: object,
    decimal_separator: str | None,
    *,
    row_no: int,
    field: str,
) -> Decimal:
    """Parst einen Betrag gemaess Dezimalformat in eine Dezimalzahl."""
    if isinstance(value, bool):
        raise MappingError(f"{field}: {value!r} ist kein Betrag.", row_no=row_no, field=field)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = _normalise_amount_text(value)
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.endswith("-"):
        negative = not negative
        text = text[:-1]
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]

    if not text:
        raise MappingError(f"{field}: Betrag fehlt oder ist leer.", row_no=row_no, field=field)

    separator = decimal_separator or _infer_decimal_separator(text)
    if separator == "comma":
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    if not _AMOUNT_TEXT_PATTERN.fullmatch(text):
        raise MappingError(
            f"{field}: Betrag {value!r} ist keine gueltige Zahl fuer das Dezimalformat {separator!r}.",
            row_no=row_no,
            field=field,
        )

    try:
        decimal_value = Decimal(text)
    except InvalidOperation as exc:
        raise MappingError(
            f"{field}: Betrag {value!r} ist keine gueltige Zahl.",
            row_no=row_no,
            field=field,
        ) from exc

    return -decimal_value if negative else decimal_value


def _normalise_amount_text(value: object) -> str:
    """Entfernt Leerraum, Waehrungssymbole und Waehrungskuerzel aus einem Betrag."""
    text = str(value).strip()
    text = text.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    for symbol in ("€", "$", "£", "¥"):
        text = text.replace(symbol, "")
    return _CURRENCY_WORDS.sub("", text)


def _infer_decimal_separator(text: str) -> str:
    """Leitet das Dezimaltrennzeichen aus einem einzelnen Betrag ab.

    Bei zwei Trennzeichen entscheidet das zuletzt stehende. Steht nur ein
    Trennzeichen und folgen ihm ein oder zwei Ziffern, gilt es als
    Dezimaltrennzeichen; sonst wird es als Tausendertrennzeichen entfernt.
    """
    last_dot = text.rfind(".")
    last_comma = text.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        return "comma" if last_comma > last_dot else "point"
    if last_comma >= 0:
        fraction = text[last_comma + 1 :]
        return "comma" if fraction.isdigit() and len(fraction) <= 2 else "point"
    if last_dot >= 0:
        fraction = text[last_dot + 1 :]
        return "point" if fraction.isdigit() and len(fraction) <= 2 else "comma"
    return "point"


def _lookup(row: Mapping[str, Any], path: str, kind: str | None) -> object:
    """Loest ein Quellfeld auf: direkter Schluessel vor Punkt-Pfad.

    Ein exakt vorhandener Schluessel gewinnt immer, damit CSV-Spalten mit Punkt
    im Namen unveraendert funktionieren. Bei ``kind == 'csv'`` wird nicht weiter
    in verschachtelte Werte abgestiegen.
    """
    if path in row:
        return row[path]
    if kind == "csv":
        return _MISSING

    current: object = row
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, (list, tuple)):
            if not segment.isdigit():
                return _MISSING
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _entry_target(key: object, value: object) -> tuple[str, str] | None:
    """Erkennt (Zielfeld, Quellfeld) aus einem Mapping-Eintrag in beide Richtungen."""
    if not isinstance(key, str) or not key:
        return None
    if key in _TARGETS and isinstance(value, str) and value:
        return key, value
    if isinstance(value, str) and value in _TARGETS:
        return value, key
    return None


def _is_field_entry(value: object) -> bool:
    """True, wenn ein reservierter Optionsname als Quellfeld gemeint ist."""
    return isinstance(value, str) and value in _TARGETS


def _checked_path(value: object, target: str) -> str:
    """Prueft, dass ein Quellpfad ein nicht leerer String ist."""
    if not isinstance(value, str) or not value:
        raise MappingError(f"Quellfeld fuer {target!r} muss ein nicht leerer String sein.")
    return value


def _option(options: Mapping[str, Any], format_opts: object, name: str) -> object:
    """Liest eine Option: explizites Mapping vor Formatoptionen."""
    if name in options:
        return options[name]
    return _format_option(format_opts, name)


def _format_option(format_opts: object, name: str) -> object:
    """Liest eine Option aus :class:`CsvFormatGuess` oder einem Mapping."""
    if format_opts is None:
        return _MISSING
    if isinstance(format_opts, CsvFormatGuess):
        if name == "decimal_separator":
            return format_opts.decimal_separator
        if name == "date_format":
            return format_opts.date_format
        return _MISSING
    if isinstance(format_opts, Mapping):
        return format_opts.get(name, _MISSING)
    raise MappingError("format_opts muss ein CsvFormatGuess oder ein Objekt mit Formatoptionen sein.")


def _validated_choice(value: object, allowed: Iterable[str], name: str) -> str | None:
    """Prueft eine Auswahloption gegen die erlaubten Werte."""
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise MappingError(f"Ungueltige Option {name!r}: {value!r}. Erlaubt: {', '.join(allowed)}.")
    return value


def _validated_bool(value: object, name: str) -> bool:
    """Prueft eine boolesche Option."""
    if value is _MISSING or value is None:
        return False
    if not isinstance(value, bool):
        raise MappingError(f"Option {name!r} muss true oder false sein.")
    return value


def _validated_currency_default(value: object) -> str:
    """Prueft die Standardwaehrung; ohne Angabe gilt ``EUR``."""
    if value is _MISSING or value is None:
        return "EUR"
    if not isinstance(value, str) or not _CURRENCY_PATTERN.fullmatch(value.strip().upper()):
        raise MappingError("Option 'currency_default' muss ein ISO-4217-Code aus drei Buchstaben sein.")
    return value.strip().upper()


def _validated_row_offset(value: object) -> int:
    """Prueft den Zeilenversatz fuer die Fehlermeldung."""
    if value is _MISSING or value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MappingError("Option 'row_offset' muss eine nicht negative Ganzzahl sein.")
    return value


def _is_blank_row(row: Mapping[str, Any]) -> bool:
    """True, wenn alle Zellwerte leer sind."""
    return all(_is_blank(value) for value in row.values())


def _is_missing_or_blank(value: object) -> bool:
    """True, wenn ein Quellfeld nicht existiert oder leer ist."""
    return value is _MISSING or _is_blank(value)


def _is_blank(value: object) -> bool:
    """True fuer ``None`` und reine Leerstrings."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _text_or_none(value: object) -> str | None:
    """Trimmt Text; leere Werte ergeben ``None``."""
    if _is_missing_or_blank(value):
        return None
    return str(value).strip()
