"""Formaterkennung und Zeilenaufbereitung fuer den Datei-Import (F-IMP-01..04).

Der Service verbindet die vorhandenen Bausteine zu zwei Schritten, die der
REST-Assistent (``POST /api/import/detect`` und ``POST /api/import/preview``)
nutzt:

- :func:`detect_import` erkennt, ob die Datei CSV, fremdes JSON oder das eigene
  JSON-Format (``format == "finanzuebersicht"``, F-IMP-04) ist. Bei CSV liefert
  es den Vorschlag fuer Kodierung, Trennzeichen, Dezimal- und Datumsformat
  samt Kopfzeile und Vorspann (:func:`finue.services.csv_detect.detect_csv_format`,
  F-IMP-02).
- :func:`load_rows` bereitet die Bytes zu der Eingabe auf, die
  :func:`finue.services.import_preview.build_preview` und
  :func:`finue.services.import_run.execute_import` erwarten: bei CSV/fremdem JSON
  eine Liste von Zeilen-Woertern, beim eigenen JSON das geparste Wurzelobjekt.

Manuelle Korrekturwerte (F-IMP-02) ueberschreiben die Erkennung vollstaendig.
Die Funktionen sind rein lesend und schreiben nichts in die Datenbank
(N-DB-01); Fehler sind Standard-Ausnahmen, die die API-Schicht uebersetzt.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Final, Literal

from finue.services.csv_detect import CsvFormatGuess, detect_csv_format
from finue.services.import_json import ImportFormatError, detect_own_format

__all__ = [
    "CsvOptions",
    "ImportDetection",
    "Kind",
    "detect_import",
    "load_rows",
]

#: Import-Art: CSV, fremdes JSON oder eigenes JSON-Format (F-IMP-01/04).
Kind = Literal["csv", "json", "own_json"]

#: Kodierungen, die als JSON in Frage kommen (JSON ist per Spezifikation UTF-8).
_JSON_ENCODINGS: Final[tuple[str, ...]] = ("utf-8-sig", "utf-8")

#: Praefix-Zeichen, die auf ein JSON-Dokument hindeuten.
_JSON_PREFIXES: Final[tuple[str, ...]] = ("{", "[")


@dataclass(frozen=True, slots=True)
class CsvOptions:
    """Manuelle Korrekturwerte fuer die CSV-Erkennung (F-IMP-02).

    Nicht gesetzte Felder (``None``) werden aus den Rohdaten erkannt.
    """

    encoding: str | None = None
    delimiter: str | None = None
    decimal_separator: str | None = None
    date_format: str | None = None
    has_header: bool | None = None
    skip_rows: int | None = None


@dataclass(frozen=True, slots=True)
class ImportDetection:
    """Ergebnis der Formaterkennung (F-IMP-01..04).

    ``csv`` traegt nur bei ``kind == "csv"`` den erkannten bzw. vorgegebenen
    Formatvorschlag, sonst ``None``.
    """

    kind: Kind
    csv: CsvFormatGuess | None = None


def detect_import(content: bytes, options: CsvOptions | None = None) -> ImportDetection:
    """Erkennt das Importformat einer Datei (F-IMP-01/02/04).

    Args:
        content: Rohe Dateibytes.
        options: Manuelle Korrekturwerte (F-IMP-02); ``None`` bedeutet reine
            Erkennung.

    Returns:
        :class:`ImportDetection` mit ``csv``, ``json`` oder ``own_json``.

    Raises:
        ValueError: Ein Korrekturwert ist unbekannt (z. B. Trennzeichen).
        ImportFormatError: Der Inhalt sieht wie JSON aus, ist aber ungueltig.
    """

    parsed, is_json = _maybe_json(content)
    if is_json:
        if detect_own_format(parsed):
            return ImportDetection(kind="own_json")
        return ImportDetection(kind="json")

    guess = _detect_csv(content, options)
    return ImportDetection(kind="csv", csv=guess)


def load_rows(content: bytes, *, kind: Kind, options: CsvOptions | None = None) -> object:
    """Bereitet Dateibytes zu der Parser-Eingabe des Imports auf.

    Args:
        content: Rohe Dateibytes.
        kind: Erkannte oder erzwungene Import-Art.
        options: Manuelle CSV-Korrekturwerte (F-IMP-02).

    Returns:
        - ``csv``: Liste von Zeilen-Woertern (Schluessel sind die Spalten).
        - ``json``: die geparste JSON-Liste der Buchungsobjekte.
        - ``own_json``: das geparste JSON-Wurzelobjekt (Dokument).

    Raises:
        ImportFormatError: Der Inhalt passt nicht zur geforderten Art.
        ValueError: Ein Korrekturwert ist unbekannt.
    """

    if kind == "csv":
        return _csv_rows(content, _detect_csv(content, options))

    parsed, is_json = _maybe_json(content)
    if not is_json:
        raise ImportFormatError("Die Datei ist kein gueltiges JSON.")
    if kind == "own_json":
        if not isinstance(parsed, dict):
            raise ImportFormatError("Das eigene JSON-Format muss ein Objekt sein.")
        return parsed
    if not isinstance(parsed, list):
        raise ImportFormatError("Fremdes JSON muss eine Liste von Buchungsobjekten sein.")
    return parsed


def _detect_csv(content: bytes, options: CsvOptions | None) -> CsvFormatGuess:
    """Erkennt das CSV-Format unter Anwendung der Korrekturwerte (F-IMP-02)."""

    opts = options if options is not None else CsvOptions()
    return detect_csv_format(
        content,
        encoding=opts.encoding,
        delimiter=opts.delimiter,
        decimal_separator=opts.decimal_separator,
        date_format=opts.date_format,
        has_header=opts.has_header,
        skip_rows=opts.skip_rows,
    )


def _maybe_json(content: bytes) -> tuple[object | None, bool]:
    """Versucht, den Inhalt als JSON zu lesen (nur bei JSON-typischem Praefix).

    Der Inhalt gilt nur dann als JSON, wenn er nach dem Dekodieren mit ``{`` oder
    ``[`` beginnt. Ein solcher Inhalt, der sich nicht parsen laesst, wird als
    Fehler gemeldet (statt stillschweigend als CSV fehlgedeutet), damit ein
    beschaedigter JSON-Export eine klare Meldung erhaelt.
    """

    text = _decode_json(content)
    if text is None:
        return None, False
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped.startswith(_JSON_PREFIXES):
        return None, False
    try:
        parsed: object = json.loads(stripped)
    except ValueError as exc:
        raise ImportFormatError("Die Datei beginnt wie JSON, ist aber kein gueltiges JSON.") from exc
    return parsed, True


def _decode_json(content: bytes) -> str | None:
    """Dekodiert JSON-Bytes als UTF-8; ``None``, wenn keine Kodierung passt."""

    for encoding in _JSON_ENCODINGS:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _csv_rows(content: bytes, guess: CsvFormatGuess) -> list[dict[str, str]]:
    """Zerlegt CSV-Bytes gemaess dem erkannten Format in Zeilen-Woerter.

    Bei einer Kopfzeile sind die Schluessel die Spaltennamen; ohne Kopfzeile
    werden Ersatznamen ``spalte_1`` ... verwendet, damit das namensbasierte
    Mapping ueberhaupt greifen kann. Vollstaendig leere Zeilen entfallen.
    """

    text = content.decode(guess.encoding, errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        raw_rows = list(csv.reader(io.StringIO(text), delimiter=guess.delimiter))
    except csv.Error:
        raw_rows = [line.split(guess.delimiter) for line in text.splitlines()]

    data = raw_rows[guess.skip_rows :] if guess.skip_rows else raw_rows
    header: list[str] = []
    body: list[list[str]] = data
    if guess.has_header and data:
        header = [cell.strip() for cell in data[0]]
        body = data[1:]
    else:
        width = max((len(row) for row in data), default=0)
        header = [f"spalte_{index + 1}" for index in range(width)]

    records: list[dict[str, str]] = []
    for row in body:
        if not any(cell.strip() for cell in row):
            continue
        record: dict[str, str] = {}
        for index, cell in enumerate(row):
            key = header[index] if index < len(header) else f"spalte_{index + 1}"
            record[key] = cell
        records.append(record)
    return records
