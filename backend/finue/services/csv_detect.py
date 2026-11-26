"""CSV-Formaterkennung fuer den Import nach F-IMP-02.

Erkennt Kodierung, Trennzeichen, Dezimalformat, Datumsformat, Kopfzeile und
Vorspann-Zeilen. Jedes Feld kann ueber einen expliziten Parameter manuell
korrigiert werden; eine Vorgabe ersetzt dann die Erkennung vollstaendig.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

SUPPORTED_ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252")
SUPPORTED_DELIMITERS: tuple[str, ...] = (";", ",", "\t")
SUPPORTED_DECIMAL_SEPARATORS: tuple[str, ...] = ("comma", "point")
SUPPORTED_DATE_FORMATS: tuple[str, ...] = ("TT.MM.JJJJ", "JJJJ-MM-TT", "MM/TT/JJJJ", "TT/MM/JJJJ")

_UTF8_BOM = b"\xef\xbb\xbf"
_SAMPLE_CHARS = 64 * 1024
_MAX_SCAN_ROWS = 100
_HEADER_SCAN_ROWS = 8
_FOLLOWING_ROWS = 5

_GERMAN_GROUPED = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{1,2}$")
_GERMAN_PLAIN = re.compile(r"^-?\d+,\d{1,2}$")
_ENGLISH_GROUPED = re.compile(r"^-?\d{1,3}(?:,\d{3})*\.\d{1,2}$")
_ENGLISH_PLAIN = re.compile(r"^-?\d+\.\d{1,2}$")

_DD_MM_YYYY = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MM_DD_YYYY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

_HEADER_KEYWORDS = frozenset(
    {
        "datum",
        "date",
        "buchungstag",
        "buchungsdatum",
        "wertstellung",
        "verwendungszweck",
        "beschreibung",
        "description",
        "betrag",
        "amount",
        "umsatz",
        "saldo",
        "balance",
        "haben",
        "soll",
        "konto",
        "account",
        "iban",
        "empfaenger",
        "empfänger",
        "auftraggeber",
        "payee",
        "memo",
        "waehrung",
        "währung",
        "currency",
        "referenz",
        "reference",
        "nummer",
        "num",
        "kategorie",
        "category",
        "typ",
        "type",
        "status",
        "transaction",
        "transaktion",
    }
)


@dataclass(frozen=True, slots=True)
class CsvFormatGuess:
    """Erkanntes oder manuell vorgegebenes CSV-Format.

    delimiter enthaelt das tatsaechliche Zeichen, fuer Tabulator also "\\t".
    date_format ist None, wenn in den Beispieldaten kein Datum erkannt wurde.
    """

    encoding: str
    delimiter: str
    decimal_separator: str
    date_format: str | None
    has_header: bool
    skip_rows: int


def detect_csv_format(
    raw_bytes: bytes,
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    decimal_separator: str | None = None,
    date_format: str | None = None,
    has_header: bool | None = None,
    skip_rows: int | None = None,
) -> CsvFormatGuess:
    """Erkennt das CSV-Format und wendet manuelle Vorgaben an.

    Nicht gesetzte Parameter werden aus den Rohdaten abgeleitet. Gesetzte
    Parameter werden unveraendert uebernommen und nicht gegen die Erkennung
    verworfen (F-IMP-02, manuelle Korrektur).
    """
    data = bytes(raw_bytes)

    chosen_encoding = encoding if encoding is not None else _detect_encoding(data)
    if chosen_encoding not in SUPPORTED_ENCODINGS:
        raise ValueError(f"Unbekannte Kodierung: {chosen_encoding!r}")
    text = _decode_bytes(data, chosen_encoding)

    chosen_delimiter = delimiter if delimiter is not None else _detect_delimiter(text)
    if chosen_delimiter not in SUPPORTED_DELIMITERS:
        raise ValueError(f"Unbekanntes Trennzeichen: {chosen_delimiter!r}")

    if skip_rows is not None and skip_rows < 0:
        raise ValueError("skip_rows darf nicht negativ sein")

    rows = _parse_rows(text, chosen_delimiter)
    if skip_rows is None:
        detected_has_header, detected_skip_rows = _detect_layout(rows)
        chosen_skip_rows = detected_skip_rows
        chosen_has_header = has_header if has_header is not None else detected_has_header
    else:
        chosen_skip_rows = skip_rows
        if has_header is None:
            chosen_has_header, _ = _detect_layout(rows[skip_rows:], base_index=skip_rows)
        else:
            chosen_has_header = has_header

    data_start = chosen_skip_rows + (1 if chosen_has_header else 0)
    data_rows = rows[data_start:]

    chosen_decimal = decimal_separator if decimal_separator is not None else _detect_decimal_separator(data_rows)
    if chosen_decimal not in SUPPORTED_DECIMAL_SEPARATORS:
        raise ValueError(f"Unbekanntes Dezimalformat: {chosen_decimal!r}")

    chosen_date = date_format if date_format is not None else _detect_date_format(data_rows)
    if chosen_date is not None and chosen_date not in SUPPORTED_DATE_FORMATS:
        raise ValueError(f"Unbekanntes Datumsformat: {chosen_date!r}")

    return CsvFormatGuess(
        encoding=chosen_encoding,
        delimiter=chosen_delimiter,
        decimal_separator=chosen_decimal,
        date_format=chosen_date,
        has_header=chosen_has_header,
        skip_rows=chosen_skip_rows,
    )


def _detect_encoding(raw_bytes: bytes) -> str:
    """Waehlt utf-8-sig bei BOM, sonst utf-8, sonst cp1252."""
    if raw_bytes.startswith(_UTF8_BOM):
        return "utf-8-sig"
    try:
        raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8"


def _decode_bytes(raw_bytes: bytes, encoding: str) -> str:
    """Dekodiert und entfernt ein fuehrendes BOM-Zeichen."""
    try:
        text = raw_bytes.decode(encoding)
    except UnicodeDecodeError:
        text = raw_bytes.decode(encoding, errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _parse_rows(text: str, delimiter: str) -> list[list[str]]:
    """Zerlegt den Text mit dem csv-Modul in Zeilen und Spalten."""
    try:
        return list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return [line.split(delimiter) for line in text.splitlines()]


def _detect_delimiter(text: str) -> str:
    """Erkennt das Trennzeichen per csv.Sniffer plus Konsistenz-Heuristik."""
    sample = text[:_SAMPLE_CHARS]
    preferred = _delimiter_by_sniffer(sample)
    scores: dict[str, tuple[int, float, float]] = {}
    for candidate in SUPPORTED_DELIMITERS:
        consistent_rows, content_density = _delimiter_profile(sample, candidate)
        weight = sample.count(candidate) + (0.5 if candidate == preferred else 0.0)
        scores[candidate] = (consistent_rows, content_density, weight)
    return max(SUPPORTED_DELIMITERS, key=lambda candidate: scores[candidate])


def _delimiter_by_sniffer(text: str) -> str | None:
    try:
        candidate = csv.Sniffer().sniff(text, delimiters=";,\t").delimiter
    except csv.Error:
        return None
    return candidate if candidate in SUPPORTED_DELIMITERS else None


def _delimiter_profile(text: str, delimiter: str) -> tuple[int, float]:
    """Liefert (Zeilen mit haeufigster Breite > 1, Anteil Datum/Betrag)."""
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return (0, 0.0)
    non_empty = [row for row in rows[:50] if row and any(cell.strip() for cell in row)]
    if not non_empty:
        return (0, 0.0)
    counts: Counter[int] = Counter(len(row) for row in non_empty)
    width, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    if width <= 1:
        return (0, 0.0)
    with_width = [row for row in non_empty if len(row) == width]
    density = sum(_data_density(row) for row in with_width) / len(with_width)
    return (count, density)


def _detect_layout(rows: list[list[str]], *, base_index: int = 0) -> tuple[bool, int]:
    """Erkennt Kopfzeile und Anzahl der Vorspann-Zeilen.

    Rueckgabe: (has_header, skip_rows). skip_rows ist der nullbasierte Index
    der Kopfzeile bzw. der ersten Datenzeile, wenn keine Kopfzeile existiert.
    """
    indexed = [(base_index + index, row) for index, row in enumerate(rows) if any(cell.strip() for cell in row)]
    if not indexed:
        return (False, base_index)

    width_counts: Counter[int] = Counter(len(row) for _, row in indexed if len(row) > 1)
    if not width_counts:
        return (False, indexed[0][0])

    dominant_width = max(width_counts, key=lambda width: (width_counts[width], width))
    width_rows = [(index, row) for index, row in indexed if len(row) == dominant_width]
    if not width_rows:
        return (False, indexed[0][0])

    first_index = width_rows[0][0]
    best_index = first_index
    best_score = -1.0
    best_ratio = 0.0
    best_keyword_hits = 0
    best_following_density = 0.0

    for position, (index, row) in enumerate(width_rows[:_HEADER_SCAN_ROWS]):
        ratio = _header_like_ratio(row)
        keyword_hits = _header_keyword_hits(row)
        following = [following_row for _, following_row in width_rows[position + 1 : position + 1 + _FOLLOWING_ROWS]]
        density = (
            sum(_data_density(following_row) for following_row in following) / len(following) if following else 0.0
        )
        score = keyword_hits * 10.0 + ratio * 2.0 + density
        if score > best_score:
            best_index = index
            best_score = score
            best_ratio = ratio
            best_keyword_hits = keyword_hits
            best_following_density = density

    looks_like_header = best_ratio >= 0.5 and (
        best_following_density > 0.0 or best_keyword_hits > 0 or len(width_rows) == 1
    )
    if looks_like_header:
        return (True, best_index)
    return (False, first_index)


def _header_like_ratio(row: list[str]) -> float:
    """Anteil nicht leerer Zellen, die weder Datum noch Betrag sind."""
    values = [cell.strip() for cell in row if cell.strip()]
    if not values:
        return 0.0
    header_like = sum(1 for cell in values if not _is_amount_like(cell) and _classify_date(cell) is None)
    return header_like / len(values)


def _data_density(row: list[str]) -> float:
    """Anteil nicht leerer Zellen, die wie Datum oder Betrag aussehen."""
    values = [cell.strip() for cell in row if cell.strip()]
    if not values:
        return 0.0
    content = sum(1 for cell in values if _is_amount_like(cell) or _classify_date(cell) is not None)
    return content / len(values)


def _header_keyword_hits(row: list[str]) -> int:
    """Zaehlt Zellen mit bekannten Kopfzeilen-Begriffen."""
    hits = 0
    for cell in row:
        for token in _header_tokens(cell):
            if token in _HEADER_KEYWORDS or token.startswith(("konto", "buchung", "umsatz")):
                hits += 1
                break
    return hits


def _header_tokens(value: str) -> tuple[str, ...]:
    normalised = re.sub(r"[^0-9a-zA-ZäöüÄÖÜß]+", " ", value.strip().lower())
    return tuple(token for token in normalised.split() if token)


def _normalise_amount(value: str) -> str:
    """Entfernt Leerzeichen und Waehrungszeichen fuer die Zahlenklassifikation."""
    text = value.strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    for symbol in ("€", "$", "£"):
        text = text.replace(symbol, "")
    text = re.sub(r"(?i)(?:eur|usd|gbp|chf)", "", text)
    return text.strip()


def _is_amount_like(value: str) -> bool:
    text = _normalise_amount(value)
    return any(
        pattern.fullmatch(text) is not None
        for pattern in (_GERMAN_GROUPED, _GERMAN_PLAIN, _ENGLISH_GROUPED, _ENGLISH_PLAIN)
    )


def _detect_decimal_separator(rows: list[list[str]]) -> str:
    """Waehlt comma bei ueberwiegend deutschem, sonst point."""
    german = 0
    english = 0
    for row in rows[:_MAX_SCAN_ROWS]:
        for cell in row:
            text = _normalise_amount(cell)
            if _GERMAN_GROUPED.fullmatch(text) is not None or _GERMAN_PLAIN.fullmatch(text) is not None:
                german += 1
            if _ENGLISH_GROUPED.fullmatch(text) is not None or _ENGLISH_PLAIN.fullmatch(text) is not None:
                english += 1
    return "comma" if german > english else "point"


def _detect_date_format(rows: list[list[str]]) -> str | None:
    """Waehlt das haeufigste erkannte Datumsformat, bei Gleichstand nach Prioritaet."""
    counts: Counter[str] = Counter()
    for row in rows[:_MAX_SCAN_ROWS]:
        for cell in row:
            detected = _classify_date(cell)
            if detected is not None:
                counts[detected] += 1
    if not counts:
        return None
    return max(counts, key=lambda fmt: (counts[fmt], -SUPPORTED_DATE_FORMATS.index(fmt)))


def _classify_date(value: str) -> str | None:
    """Ordnet einen Zellwert einem unterstuetzten Datumsformat zu."""
    text = value.strip()

    match = _DD_MM_YYYY.fullmatch(text)
    if match and _valid_date(int(match.group(3)), int(match.group(2)), int(match.group(1))):
        return "TT.MM.JJJJ"

    match = _ISO_DATE.fullmatch(text)
    if match and _valid_date(int(match.group(1)), int(match.group(2)), int(match.group(3))):
        return "JJJJ-MM-TT"

    match = _MM_DD_YYYY.fullmatch(text)
    if match:
        first = int(match.group(1))
        second = int(match.group(2))
        year = int(match.group(3))
        if _valid_date(year, first, second):
            return "MM/TT/JJJJ"
        if _valid_date(year, second, first):
            return "TT/MM/JJJJ"
    return None


def _valid_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True
