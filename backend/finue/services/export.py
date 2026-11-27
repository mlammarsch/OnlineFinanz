"""Export-Service fuer JSON, Markdown und CSV (F-EXP-01..06).

Der Export baut auf zwei vorhandenen Bausteinen auf:

* :mod:`finue.services.search` liefert die gefilterte Buchungsmenge. Der
  Umfang (Konto, Gruppe, Suchtreffer) und alle Filter/Zeitraeume der Ansicht
  werden ueber eine :class:`~finue.services.search.TransactionQuery` beschrieben,
  damit Suche, Buchungsliste (T-093) und Export dieselben Grenzen und Regeln
  verwenden (D10: MCP nutzt spaeter dieselbe Schicht).
* :mod:`finue.services.export_schema` liefert das JSON-Zieldokument v1 inklusive
  Zeitzonen- und Dezimal-String-Pruefung und schliesst Zugangsdaten aus
  (N-SEC-01, N-SEC-02).

Umfang und Anforderungen:

* **F-EXP-01** Export von Konto, Gruppe, Suchtreffern (TransactionQuery-Filter)
  oder einer Auswahl (ID-Liste mit Grenze), je als JSON oder Markdown.
* **F-EXP-02** Zeitraum und Filter der Ansicht gelten; der Dateiname nennt
  Konto bzw. Gruppe und den Zeitraum in ASCII (Umlaute transkribiert). Ist der
  Zeitraum ``alle``, stammt der Dateiname-Zeitraum aus Minimum/Maximum der
  exportierten Buchungen.
* **F-EXP-04** JSON nach Schema v1, verlustfrei inklusive Herkunft der
  Kennzeichen; die Kontostammdaten tragen keine Zugangsdaten. Der Vorkommen-Index
  wird im Bestand 0-basiert gefuehrt (F-DUP-02, z. B. Demo- und Importdaten),
  das Schema v1 verlangt jedoch mindestens 1; der Export gibt ihn deshalb
  1-basiert aus. Der Re-Import vergibt den Index ohnehin neu (Multimengen-
  Abgleich), sodass kein Datenstand verloren geht.
* **F-EXP-05** Markdown mit Kopf (Konto/Gruppe, Zeitraum, Anzahl, Summe),
  Tabelle (Datum, Gegenpartei, Verwendungszweck, Betrag, Notiz; in der
  Gruppenansicht zusaetzlich Konto), Summenzeile, deutschem Zahlen- und
  Datumsformat und Maskierung von ``|``, Backslash und Zeilenumbruechen.
* **F-EXP-03** CSV mit Optionen Trennzeichen, Dezimalformat, Datumsformat,
  Kodierung, Spaltenauswahl und -reihenfolge sowie den Zusatzspalten Notiz,
  geprueft, abgestimmt und externe Referenz. Das Standardprofil ist an den
  Finanzblick-Export angelehnt und **unbelegt** (Owner-Beispieldatei fehlt,
  SPEC Abschnitt 7); die Optionen sind ueber :class:`CsvExportOptions`
  zentral validiert. Formel-Injection: frei wirkende Textzellen mit einem
  fuehrenden Formelzeichen werden mit einem Apostroph entschaerft
  (:func:`strip_export_guard` macht das beim Import rueckgaengig, siehe
  ``decisions.md``); Betrag und Datum bleiben unveraendert, damit der
  Roundtrip intakt bleibt.
* **F-EXP-06** Der CSV-Export traegt mit der passenden Importvorlage
  ("Eigener CSV-Export", F-IMP-05) alle Fingerprint-Felder; ein Re-Import
  ergibt dadurch 0 neue Buchungen.
* Zusammengefuehrte Buchungen (``merged``, F-DUP-04) sind immer ausgeschlossen.

Grenze: Mehr als ``max_rows`` Buchungen lehnt der Service mit
:class:`ExportLimitExceeded` ab, damit ein unbeabsichtigter Massenexport nicht
den Prozess oder die Antwort sprengt. N-DB-01: kein rohes SQL, nur
SQLAlchemy-Ausdruecke.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.csv_detect import (
    SUPPORTED_DELIMITERS,
    SUPPORTED_ENCODINGS,
)
from finue.services.export_schema import (
    EXPORT_FORMAT_NAME,
    SUPPORTED_EXPORT_VERSION,
    ExportDocumentV1,
)
from finue.services.search import (
    SearchPeriod,
    SearchScope,
    SearchValidationError,
    SortDirection,
    SortField,
    TransactionQuery,
    build_count_select,
    build_query,
    build_select,
)

__all__ = [
    "CLIPBOARD_MEDIA_TYPE",
    "CSV_COLUMN_LABELS",
    "CSV_MEDIA_TYPE",
    "DEFAULT_CSV_COLUMNS",
    "EXPORT_GUARD_PRE_FILTER",
    "JSON_MEDIA_TYPE",
    "MARKDOWN_MEDIA_TYPE",
    "SUPPORTED_CSV_COLUMNS",
    "CsvExportOptions",
    "ExportFormat",
    "ExportLimitExceeded",
    "ExportRequest",
    "ExportResult",
    "ExportScope",
    "ExportValidationError",
    "build_csv",
    "build_filename",
    "build_export_document",
    "encode_content",
    "format_amount_de",
    "format_date_de",
    "merge_csv_options",
    "render_markdown",
    "run_export",
    "slugify",
    "strip_export_guard",
    "validate_csv_columns",
    "validate_csv_options",
]

#: Medientypen der Exportantworten (F-EXP-01, F-EXP-03, F-EXP-05).
JSON_MEDIA_TYPE: Final = "application/json"
MARKDOWN_MEDIA_TYPE: Final = "text/markdown"
CSV_MEDIA_TYPE: Final = "text/csv"
CLIPBOARD_MEDIA_TYPE: Final = "text/plain"

#: Deutsche Umlaute und das scharfe s fuer den ASCII-Dateinamen (F-EXP-02).
_TRANSCRIPTION: Final = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})

#: Ersatzlabel, wenn ein Name keinen verwertbaren ASCII-Rest hat (F-EXP-02).
_FALLBACK_SLUG: Final = "export"

#: Spalten des CSV-Exports: stabile Schluessel -> deutsche Kopfzeile (F-EXP-03).
#: Die Kopfzeilen sind ASCII (Umlaute ausgeschrieben), damit cp1252-Exporte
#: ohne Sonderzeichen auskommen. Die Importvorlage "Eigener CSV-Export" mappt
#: ueber genau diese Kopfzeilen (F-IMP-05, F-EXP-06).
CSV_COLUMN_LABELS: Final[dict[str, str]] = {
    "booking_date": "Buchungstag",
    "value_date": "Wertstellung",
    "counterparty": "Gegenpartei",
    "counterparty_iban": "IBAN Gegenpartei",
    "purpose": "Verwendungszweck",
    "amount": "Betrag",
    "currency": "Waehrung",
    "note": "Notiz",
    "checked": "Geprueft",
    "reconciled": "Abgestimmt",
    "external_ref": "Externe Referenz",
    "status": "Status",
    "account": "Konto",
}

#: Erlaubte Spalten (Reihenfolge der Definition, nicht der Ausgabe).
SUPPORTED_CSV_COLUMNS: Final[tuple[str, ...]] = tuple(CSV_COLUMN_LABELS)

#: Standardprofil "Finanzblick-angelehnt" -- **UNBELEGT, NACH BESTEM WISSEN**
#: (Owner-Beispieldatei fehlt, SPEC Abschnitt 7). Enthaelt alle Fingerprint-
#: Felder fuer den Roundtrip (F-EXP-06) sowie die Zusatzspalten Notiz,
#: geprueft, abgestimmt und externe Referenz (F-EXP-03).
DEFAULT_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "booking_date",
    "value_date",
    "counterparty",
    "counterparty_iban",
    "purpose",
    "amount",
    "currency",
    "note",
    "checked",
    "reconciled",
    "external_ref",
)

#: Spalten, die als Freitext gegen Formel-Injection entschaerft werden.
_CSV_FREE_TEXT_COLUMNS: Final[frozenset[str]] = frozenset(
    {"counterparty", "counterparty_iban", "purpose", "note", "external_ref", "account"}
)

#: Erste Zeichen, die eine Tabellenkalkulation als Formel auswerten kann
#: (OWASP). Ein Apostroph davor entschaerft die Zelle.
_FORMULA_TRIGGERS: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r")

#: Flag einer Importvorlage, das die Formel-Entschaerfung rueckgaengig macht.
EXPORT_GUARD_PRE_FILTER: Final = "own_csv"

#: Zeilenende des CSV-Exports (RFC 4180).
_CSV_LINE_TERMINATOR: Final = "\r\n"

#: Regel fuer in cp1252 nicht darstellbare Zeichen (kuerzt nichts weg, ersetzt
#: nur einzeln); dokumentiert in decisions.md.
_CP1252_ERRORS: Final = "replace"


class ExportFormat(StrEnum):
    """Ausgabeformat eines Exports (F-EXP-01, F-EXP-03)."""

    JSON = "json"
    MARKDOWN = "md"
    CSV = "csv"


class ExportScope(StrEnum):
    """Umfang eines Exports (F-EXP-01)."""

    ALL = "all"
    ACCOUNT = "account"
    GROUP = "group"
    SEARCH = "search"
    SELECTION = "selection"


@dataclass(frozen=True, slots=True)
class CsvExportOptions:
    """Optionen des CSV-Exports (F-EXP-03).

    Trennzeichen, Dezimalformat, Datumsformat, Kodierung sowie Auswahl und
    Reihenfolge der Spalten. Die Wertebereiche sind zentral in
    :func:`validate_csv_options` und :func:`validate_csv_columns` geprueft.
    ``guard_formulas`` schaltet den Formel-Injection-Schutz fuer Freitextzellen
    ein (Standard) oder aus.
    """

    delimiter: str = ";"
    decimal_separator: str = "comma"
    date_format: str = "TT.MM.JJJJ"
    encoding: str = "utf-8"
    columns: tuple[str, ...] = DEFAULT_CSV_COLUMNS
    guard_formulas: bool = True


class ExportValidationError(ValueError):
    """Ungueltige Exportparameter; die Meldung ist deutscher Klartext."""


class ExportLimitExceeded(ExportValidationError):
    """Die angeforderte Buchungsmenge ueberschreitet die Exportgrenze."""


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """Beschreibung eines Exports (F-EXP-01, F-EXP-02).

    Bei ``scope=account``/``group``/``search`` gelten die Filter- und
    Zeitraumfelder wie bei der Suche; bei ``scope=selection`` wird die
    Buchungsmenge ueber ``transaction_ids`` bestimmt.
    """

    format: ExportFormat = ExportFormat.JSON
    scope: ExportScope = ExportScope.ALL
    account_id: int | None = None
    group_id: int | None = None
    search: str | None = None
    period: SearchPeriod = SearchPeriod.DAYS_365
    from_date: date | None = None
    to_date: date | None = None
    checked: bool | None = None
    reconciled: bool | None = None
    with_note: bool = False
    status: str | None = None
    sort: SortField = SortField.DATE
    direction: SortDirection = SortDirection.ASC
    transaction_ids: Sequence[int] | None = None
    csv: CsvExportOptions | None = None


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Fertiger Export: Inhalt, Medientyp und Dateiname (F-EXP-02).

    ``encoding`` gilt fuer die Bytes-Kodierung des Inhalts; JSON und Markdown
    sind immer UTF-8, CSV kann UTF-8 mit BOM oder Windows-1252 sein (F-EXP-03).
    """

    content: str
    media_type: str
    filename: str
    encoding: str = "utf-8"


# --------------------------------------------------------------- Dateiname


def slugify(value: str) -> str:
    """Bildet einen ASCII-Dateinamensteil ohne Leerzeichen (F-EXP-02).

    Umlaute und das scharfe s werden transkribiert (``ae``/``oe``/``ue``/``ss``),
    sonstige diakritische Zeichen entfernt. Alles ausserhalb ``[a-z0-9]`` wird
    zu einem einzelnen Bindestrich zusammengezogen. Bleibt nichts uebrig, gilt
    :data:`_FALLBACK_SLUG`.
    """

    transcribed = value.translate(_TRANSCRIPTION)
    ascii_text = unicodedata.normalize("NFKD", transcribed).encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return collapsed or _FALLBACK_SLUG


def build_filename(label: str, start: date, end: date, export_format: ExportFormat) -> str:
    """Baut den Export-Dateinamen ``slug_von_bis.ext`` (F-EXP-02).

    Der Zeitraum steht als ``JJJJ-MM-TT_JJJJ-MM-TT``; die Endung ist ``json``,
    ``md`` bzw. ``csv`` (F-EXP-03).
    """

    extension = {ExportFormat.JSON: "json", ExportFormat.MARKDOWN: "md", ExportFormat.CSV: "csv"}[export_format]
    return f"{slugify(label)}_{start.isoformat()}_{end.isoformat()}.{extension}"


# --------------------------------------------------------------- Auswahl


def _build_query(request: ExportRequest, today: date) -> TransactionQuery:
    """Uebersetzt die Filter des Exports in eine Suchabfrage (F-EXP-01/02).

    Nutzt dieselbe Abfragelogik wie die Suche; ein Mapping auf die Suchbereiche
    ``all``/``account``/``group`` (F-SUC-03) sorgt fuer identische Grenzen.
    """

    scope_map = {
        ExportScope.ALL: SearchScope.ALL,
        ExportScope.SEARCH: SearchScope.ALL,
        ExportScope.ACCOUNT: SearchScope.ACCOUNT,
        ExportScope.GROUP: SearchScope.GROUP,
    }
    try:
        return build_query(
            today=today,
            scope=scope_map[request.scope],
            account_id=request.account_id,
            group_id=request.group_id,
            search=request.search,
            period=request.period,
            from_date=request.from_date,
            to_date=request.to_date,
            checked=request.checked,
            reconciled=request.reconciled,
            with_note=request.with_note,
            status=request.status,
            sort=request.sort,
            direction=request.direction,
        )
    except SearchValidationError as exc:
        raise ExportValidationError(str(exc)) from exc


def _load_selection(session: Session, request: ExportRequest, max_rows: int) -> list[Transaction]:
    """Laedt die Buchungen einer Auswahl ueber ihre IDs (F-EXP-01)."""

    ids = list(request.transaction_ids or [])
    if not ids:
        raise ExportValidationError("Fuer den Umfang 'selection' muss 'transaction_ids' gefuellt sein.")
    if len(ids) != len(set(ids)):
        raise ExportValidationError("'transaction_ids' darf keine doppelten IDs enthalten.")
    if len(ids) > max_rows:
        raise ExportLimitExceeded(
            f"Die Auswahl umfasst {len(ids)} Buchungen und ueberschreitet die Grenze von {max_rows}."
        )

    statement = select(Transaction).where(Transaction.id.in_(ids)).order_by(Transaction.booking_date, Transaction.id)
    rows = list(session.scalars(statement).all())
    found = {row.id for row in rows}
    missing = [transaction_id for transaction_id in ids if transaction_id not in found]
    if missing:
        raise ExportValidationError(f"Unbekannte Buchungs-ID(s): {missing}.")
    # Zusammengefuehrte Buchungen nehmen nie an einem Export teil (F-DUP-04).
    return [row for row in rows if not row.merged]


def _load_query(session: Session, query: TransactionQuery, max_rows: int) -> list[Transaction]:
    """Laedt die gefilterte Buchungsmenge und prueft die Exportgrenze (F-EXP-01)."""

    total = int(session.scalar(build_count_select(query)) or 0)
    if total > max_rows:
        raise ExportLimitExceeded(f"Der Export umfasst {total} Buchungen und ueberschreitet die Grenze von {max_rows}.")
    return list(session.scalars(build_select(query).limit(max_rows)).all())


def _referenced_accounts(session: Session, transactions: Sequence[Transaction]) -> list[Account]:
    """Laedt die Konten der exportierten Buchungen in ID-Reihenfolge (F-EXP-04)."""

    account_ids = sorted({transaction.account_id for transaction in transactions})
    if not account_ids:
        return []
    statement = select(Account).where(Account.id.in_(account_ids)).order_by(Account.id)
    return list(session.scalars(statement).all())


# --------------------------------------------------------------- Zeitraum/Label


def _effective_bounds(
    request: ExportRequest,
    query: TransactionQuery | None,
    transactions: Sequence[Transaction],
    today: date,
) -> tuple[date, date]:
    """Bestimmt den Dateiname-Zeitraum (F-EXP-02).

    Ist der Zeitraum ``alle`` (bzw. bei einer Auswahl ohne Zeitraum), stammen
    die Grenzen aus Minimum/Maximum der exportierten Buchungen. Fehlen Daten,
    gilt der heutige Tag.
    """

    data_start = min((transaction.booking_date for transaction in transactions), default=None)
    data_end = max((transaction.booking_date for transaction in transactions), default=None)

    requested = query.date_range if query is not None else None
    start = (requested.start if requested is not None else None) or data_start or today
    end = (requested.end if requested is not None else None) or data_end or today
    return start, end


def _scope_label(session: Session, request: ExportRequest) -> str:
    """Ermittelt das Beschriftungsobjekt fuer Dateiname und Kopf (F-EXP-02/05)."""

    if request.scope is ExportScope.ACCOUNT:
        if request.account_id is None:
            raise ExportValidationError("Fuer den Umfang 'account' ist 'account_id' erforderlich.")
        account = session.get(Account, request.account_id)
        if account is None:
            raise ExportValidationError(f"Konto {request.account_id} wurde nicht gefunden.")
        return account.name
    if request.scope is ExportScope.GROUP:
        if request.group_id is None:
            raise ExportValidationError("Fuer den Umfang 'group' ist 'group_id' erforderlich.")
        group = session.get(AccountGroup, request.group_id)
        if group is None:
            raise ExportValidationError(f"Gruppe {request.group_id} wurde nicht gefunden.")
        return group.name
    return {
        ExportScope.ALL: "Alle Konten",
        ExportScope.SEARCH: "Suche",
        ExportScope.SELECTION: "Auswahl",
    }[request.scope]


# --------------------------------------------------------------- JSON


def _as_aware(moment: datetime) -> datetime:
    """Stellt einen Zeitzonenbezug sicher, damit der Export schema-valide ist."""

    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def cents_to_amount_string(cents: int) -> str:
    """Bildet ganze Cent als Dezimal-String mit Punkt (F-EXP-04)."""

    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}{whole}.{remainder:02d}"


def _iso_or_none(value: object) -> str | None:
    """Serialisiert Datum/Zeitstempel als ISO-String oder ``None``.

    Akzeptiert bewusst ``object``, weil die ORM-Typdeklaration der Zeitstempel
    (:class:`~finue.db.mixins.UtcDateTime`) zur Laufzeit ein ``datetime`` ist.
    """

    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return None


def _clean_iban(iban: str | None) -> str | None:
    """Entfernt Leerzeichen aus einer IBAN, damit sie dem Schema entspricht."""

    if iban is None:
        return None
    cleaned = iban.replace(" ", "")
    return cleaned or None


def _document_payload(
    request: ExportRequest,
    transactions: Sequence[Transaction],
    accounts: Sequence[Account],
    *,
    now: datetime,
    start: date,
    end: date,
) -> dict[str, object]:
    """Baut den Rohkoerper des JSON-Exports v1 (F-EXP-04)."""

    filter_payload: dict[str, object] = {
        "scope": request.scope.value,
        "account_ids": [request.account_id] if request.scope is ExportScope.ACCOUNT and request.account_id else None,
        "group_ids": [request.group_id] if request.scope is ExportScope.GROUP and request.group_id else None,
        "transaction_ids": list(request.transaction_ids or []) if request.scope is ExportScope.SELECTION else None,
        "search": request.search,
        "checked": request.checked,
        "reconciled": request.reconciled,
        "with_note": request.with_note,
    }

    return {
        "format": EXPORT_FORMAT_NAME,
        "version": SUPPORTED_EXPORT_VERSION,
        "created_at": _as_aware(now).isoformat(),
        "filter": filter_payload,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "accounts": [
            {
                "id": account.id,
                "name": account.name,
                "iban": _clean_iban(account.iban),
                "currency": account.currency,
                "account_type": account.account_type,
            }
            for account in accounts
        ],
        "transactions": [
            {
                "id": transaction.id,
                "account_id": transaction.account_id,
                "status": transaction.status,
                "booking_date": transaction.booking_date.isoformat(),
                "value_date": _iso_or_none(transaction.value_date),
                "counterparty": transaction.counterparty,
                "counterparty_iban": transaction.counterparty_iban,
                "purpose": transaction.purpose,
                "note": transaction.note,
                "checked": transaction.checked,
                "checked_at": _iso_or_none(transaction.checked_at),
                "checked_origin": transaction.checked_origin,
                "reconciled": transaction.reconciled,
                "reconciled_at": _iso_or_none(transaction.reconciled_at),
                "reconciled_origin": transaction.reconciled_origin,
                "origin": transaction.origin,
                "external_ref": transaction.external_ref,
                "fingerprint": transaction.fingerprint,
                "occurrence_index": transaction.occurrence_index + 1,
                "merged": transaction.merged,
                "amount": cents_to_amount_string(transaction.amount_cents),
                "currency": transaction.currency,
            }
            for transaction in transactions
        ],
    }


def build_export_document(
    request: ExportRequest,
    transactions: Sequence[Transaction],
    accounts: Sequence[Account],
    *,
    now: datetime,
    start: date,
    end: date,
) -> ExportDocumentV1:
    """Baut und validiert das JSON-Zieldokument v1 (F-EXP-04).

    Die Rueckgabe ist ein geprueftes :class:`ExportDocumentV1`; die
    Schema-Grenzen (Dezimal-Strings, ISO-Daten, Zeitzonen, kein Zugangsdatum)
    greifen dadurch bereits hier.
    """

    payload = _document_payload(request, transactions, accounts, now=now, start=start, end=end)
    return ExportDocumentV1.model_validate(payload)


# --------------------------------------------------------------- Markdown


def format_date_de(value: date) -> str:
    """Formatiert ein Datum deutsch als ``TT.MM.JJJJ`` (F-EXP-05)."""

    return value.strftime("%d.%m.%Y")


def format_amount_de(cents: int) -> str:
    """Formatiert ganze Cent deutsch mit Punkt und Komma (F-EXP-05)."""

    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    grouped = f"{whole:,}".replace(",", ".")
    return f"{sign}{grouped},{remainder:02d}"


def escape_markdown_cell(value: str | None) -> str:
    """Maskiert eine Tabellenzelle fuer Markdown (F-EXP-05).

    Backslash, der Spaltentrenner ``|`` und Zeilenumbrueche werden so ersetzt,
    dass die Tabelle gueltig bleibt.
    """

    if not value:
        return ""
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("|", "\\|")
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
    return escaped.replace("\n", "<br>")


def _sums_by_currency(transactions: Sequence[Transaction]) -> list[tuple[str, int]]:
    """Summiert die Betraege je Waehrung, aufsteigend sortiert (F-EXP-05)."""

    totals: dict[str, int] = {}
    for transaction in transactions:
        totals[transaction.currency] = totals.get(transaction.currency, 0) + transaction.amount_cents
    return sorted(totals.items())


def _sum_line(transactions: Sequence[Transaction], *, bold: bool) -> list[str]:
    """Baut die Summenzeile(n) je Waehrung (F-EXP-05)."""

    lines: list[str] = []
    for currency, cents in _sums_by_currency(transactions):
        text = f"Summe: {format_amount_de(cents)} {currency}"
        lines.append(f"**{text}**" if bold else text)
    if not lines:
        lines.append("**Summe: 0,00**" if bold else "Summe: keine Buchungen")
    return lines


def render_markdown(
    *,
    label: str,
    start: date,
    end: date,
    transactions: Sequence[Transaction],
    accounts_by_id: dict[int, Account],
    group_view: bool,
) -> str:
    """Rendert den Markdown-Export (F-EXP-05).

    Kopf mit Konto/Gruppe, Zeitraum, Anzahl und Summe; Tabelle mit Datum,
    optional Konto (Gruppenansicht), Gegenpartei, Verwendungszweck, Betrag und
    Notiz; abschliessende Summenzeile. Zahlen und Daten sind deutsch formatiert,
    Sonderzeichen in Zellen maskiert.
    """

    lines: list[str] = [f"# Export: {label}", ""]
    lines.append(f"Zeitraum: {format_date_de(start)} - {format_date_de(end)}")
    lines.append(f"Anzahl: {len(transactions)}")
    lines.extend(_sum_line(transactions, bold=False))
    lines.append("")

    columns = ["Datum"]
    if group_view:
        columns.append("Konto")
    columns.extend(["Gegenpartei", "Verwendungszweck", "Betrag", "Notiz"])
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")

    for transaction in transactions:
        cells = [format_date_de(transaction.booking_date)]
        if group_view:
            account = accounts_by_id.get(transaction.account_id)
            cells.append(escape_markdown_cell(account.name if account is not None else ""))
        cells.append(escape_markdown_cell(transaction.counterparty))
        cells.append(escape_markdown_cell(transaction.purpose))
        cells.append(f"{format_amount_de(transaction.amount_cents)} {transaction.currency}")
        cells.append(escape_markdown_cell(transaction.note))
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.extend(_sum_line(transactions, bold=True))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------- CSV


def validate_csv_columns(columns: object) -> tuple[str, ...]:
    """Prueft Spaltenauswahl und -reihenfolge des CSV-Exports (F-EXP-03).

    ``None`` liefert das Standardprofil. Ein leerer Wert, unbekannte oder
    doppelte Schluessel werden mit Klartext abgelehnt.
    """

    if columns is None:
        return DEFAULT_CSV_COLUMNS
    if isinstance(columns, (str, bytes)) or not isinstance(columns, Sequence):
        raise ExportValidationError("Die Spaltenauswahl muss eine Liste sein.")
    keys = list(columns)
    if not keys:
        raise ExportValidationError("Die Spaltenauswahl darf nicht leer sein.")

    result: list[str] = []
    for key in keys:
        if not isinstance(key, str) or key not in CSV_COLUMN_LABELS:
            raise ExportValidationError(f"Unbekannte CSV-Spalte {key!r}. Erlaubt: {', '.join(SUPPORTED_CSV_COLUMNS)}.")
        if key in result:
            raise ExportValidationError(f"CSV-Spalte {key!r} ist doppelt gewaehlt.")
        result.append(key)
    return tuple(result)


def _checked_choice(value: object, allowed: Sequence[str], name: str) -> str:
    """Prueft einen Auswahlwert gegen die erlaubten Optionen (F-EXP-03)."""

    if not isinstance(value, str) or value not in allowed:
        raise ExportValidationError(f"Ungueltige Option {name!r}: {value!r}. Erlaubt: {', '.join(allowed)}.")
    return value


def validate_csv_options(options: Mapping[str, Any] | None) -> CsvExportOptions:
    """Prueft und normalisiert CSV-Optionen (F-EXP-03).

    ``None`` liefert das Standardprofil. Unbekannte Schluessel werden
    abgelehnt, damit ein Tippfehler in einem Exportprofil nicht still
    ignoriert wird.
    """

    if options is None:
        return CsvExportOptions()
    if not isinstance(options, Mapping):
        raise ExportValidationError("Die CSV-Optionen muessen ein Objekt sein.")

    allowed = {"delimiter", "decimal_separator", "date_format", "encoding", "columns", "guard_formulas"}
    unknown = set(options) - allowed
    if unknown:
        raise ExportValidationError(f"Unbekannte CSV-Option(en): {', '.join(sorted(unknown))}.")

    guard = options.get("guard_formulas", True)
    if not isinstance(guard, bool):
        raise ExportValidationError("Die Option 'guard_formulas' muss true oder false sein.")

    return CsvExportOptions(
        delimiter=_checked_choice(options.get("delimiter", ";"), SUPPORTED_DELIMITERS, "delimiter"),
        decimal_separator=_checked_choice(
            options.get("decimal_separator", "comma"), ("comma", "point"), "decimal_separator"
        ),
        date_format=_checked_choice(
            options.get("date_format", "TT.MM.JJJJ"), ("TT.MM.JJJJ", "JJJJ-MM-TT"), "date_format"
        ),
        encoding=_checked_choice(options.get("encoding", "utf-8"), SUPPORTED_ENCODINGS, "encoding"),
        columns=validate_csv_columns(options.get("columns", DEFAULT_CSV_COLUMNS)),
        guard_formulas=guard,
    )


def merge_csv_options(base: CsvExportOptions, overrides: Mapping[str, Any] | None) -> CsvExportOptions:
    """Ueberschreibt einzelne CSV-Optionen einer Basis (F-EXP-03).

    Die API legt so Anfragewerte ueber ein Exportprofil; nicht gesetzte Werte
    bleiben aus der Basis erhalten.
    """

    if not overrides:
        return base
    merged: dict[str, Any] = {
        "delimiter": base.delimiter,
        "decimal_separator": base.decimal_separator,
        "date_format": base.date_format,
        "encoding": base.encoding,
        "columns": list(base.columns),
        "guard_formulas": base.guard_formulas,
    }
    for key in ("delimiter", "decimal_separator", "date_format", "encoding", "columns", "guard_formulas"):
        if key in overrides and overrides[key] is not None:
            merged[key] = overrides[key]
    return validate_csv_options(merged)


def _guard_cell(value: str) -> str:
    """Entschaerft eine Freitextzelle mit fuehrendem Formelzeichen (F-EXP-03)."""

    if value and value[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def _unguard_cell(value: object) -> object:
    """Entfernt einen von der Entschaerfung gesetzten fuehrenden Apostroph."""

    if isinstance(value, str) and len(value) > 1 and value[0] == "'" and value[1] in _FORMULA_TRIGGERS:
        return value[1:]
    return value


def strip_export_guard(rows: list[dict[str, Any]], status_field: str = "") -> list[dict[str, Any]]:
    """Macht die Formel-Entschaerfung rueckgaengig (Import-Vorverarbeitung).

    Entfernt genau einen fuehrenden Apostroph, wenn darauf ein Formelzeichen
    folgt. So bleibt eine entschaerfte Zelle beim Re-Import identisch und der
    Roundtrip (F-EXP-06) bricht nicht. Die Signatur entspricht den uebrigen
    Import-Vorverarbeitungsschritten; ``status_field`` wird nicht gebraucht.
    """

    del status_field
    return [{key: _unguard_cell(value) for key, value in row.items()} for row in rows]


def _csv_amount(cents: int, decimal_separator: str) -> str:
    """Formatiert einen Betrag fuer den CSV-Export (F-EXP-03)."""

    return format_amount_de(cents) if decimal_separator == "comma" else cents_to_amount_string(cents)


def _csv_date(value: date | None, date_format: str) -> str:
    """Formatiert ein Datum fuer den CSV-Export (F-EXP-03)."""

    if value is None:
        return ""
    return format_date_de(value) if date_format == "TT.MM.JJJJ" else value.isoformat()


def _csv_cell(transaction: Transaction, account: Account | None, column: str, options: CsvExportOptions) -> str:
    """Liefert den Zellwert einer Spalte (F-EXP-03)."""

    if column == "booking_date":
        return _csv_date(transaction.booking_date, options.date_format)
    if column == "value_date":
        return _csv_date(transaction.value_date, options.date_format)
    if column == "amount":
        return _csv_amount(transaction.amount_cents, options.decimal_separator)
    if column == "currency":
        return transaction.currency
    if column == "checked":
        return "ja" if transaction.checked else "nein"
    if column == "reconciled":
        return "ja" if transaction.reconciled else "nein"
    if column == "status":
        return transaction.status
    if column == "account":
        return account.name if account is not None else ""

    raw = getattr(transaction, column, None)
    text = raw if isinstance(raw, str) else ""
    if column in _CSV_FREE_TEXT_COLUMNS and options.guard_formulas:
        return _guard_cell(text)
    return text


def build_csv(
    *,
    transactions: Sequence[Transaction],
    accounts_by_id: dict[int, Account],
    options: CsvExportOptions,
    group_view: bool,
) -> str:
    """Rendert den CSV-Export (F-EXP-03).

    Kopfzeile aus den Spaltenlabels, danach je Buchung eine Zeile. Werte werden
    per :mod:`csv` RFC-4180-konform gequotet. In der Gruppenansicht wird die
    Spalte ``Konto`` vorangestellt, wenn sie nicht schon gewaehlt ist.
    """

    columns = list(options.columns)
    if group_view and "account" not in columns:
        columns.insert(0, "account")

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=options.delimiter, lineterminator=_CSV_LINE_TERMINATOR)
    writer.writerow([CSV_COLUMN_LABELS[column] for column in columns])
    for transaction in transactions:
        account = accounts_by_id.get(transaction.account_id)
        writer.writerow([_csv_cell(transaction, account, column, options) for column in columns])
    return buffer.getvalue()


def encode_content(content: str, encoding: str) -> bytes:
    """Kodiert Exportinhalt byteweise (F-EXP-03).

    ``utf-8-sig`` setzt das BOM, ``cp1252`` ersetzt nicht darstellbare Zeichen
    mit ``?`` (dokumentierte Regel in ``decisions.md``). Andere Werte gelten als
    UTF-8.
    """

    if encoding == "utf-8-sig":
        return content.encode("utf-8-sig")
    if encoding == "cp1252":
        return content.encode("cp1252", errors=_CP1252_ERRORS)
    return content.encode("utf-8")


# --------------------------------------------------------------- Einstieg


def run_export(
    session: Session,
    request: ExportRequest,
    *,
    now: datetime,
    today: date,
    max_rows: int,
) -> ExportResult:
    """Fuehrt einen Export aus und liefert Inhalt, Medientyp und Dateiname.

    Args:
        session: SQLAlchemy-Session mit dem Datenbestand.
        request: Beschreibung des Exports (Umfang, Filter, Format).
        now: Erstellungszeitpunkt (aware; naive Werte gelten als UTC).
        today: Bezugstag fuer relative Zeitraeume (Europe/Berlin).
        max_rows: Hoechstzahl der Buchungen je Export (Grenze, F-EXP-01/02).

    Returns:
        Das fertige :class:`ExportResult`.

    Raises:
        ExportLimitExceeded: Die Buchungsmenge ueberschreitet ``max_rows``.
        ExportValidationError: Umfang, Filter oder Auswahl sind ungueltig.
    """

    if request.scope is ExportScope.SELECTION:
        transactions = _load_selection(session, request, max_rows)
        query: TransactionQuery | None = None
    else:
        query = _build_query(request, today)
        transactions = _load_query(session, query, max_rows)

    accounts = _referenced_accounts(session, transactions)
    start, end = _effective_bounds(request, query, transactions, today)
    label = _scope_label(session, request)
    filename = build_filename(label, start, end, request.format)

    if request.format is ExportFormat.JSON:
        document = build_export_document(request, transactions, accounts, now=now, start=start, end=end)
        return ExportResult(content=document.model_dump_json(indent=2), media_type=JSON_MEDIA_TYPE, filename=filename)

    accounts_by_id = {account.id: account for account in accounts}
    group_view = request.scope is ExportScope.GROUP or len(accounts_by_id) > 1

    if request.format is ExportFormat.CSV:
        options = request.csv or CsvExportOptions()
        content = build_csv(
            transactions=transactions,
            accounts_by_id=accounts_by_id,
            options=options,
            group_view=group_view,
        )
        return ExportResult(
            content=content,
            media_type=CSV_MEDIA_TYPE,
            filename=filename,
            encoding=options.encoding,
        )

    content = render_markdown(
        label=label,
        start=start,
        end=end,
        transactions=transactions,
        accounts_by_id=accounts_by_id,
        group_view=group_view,
    )
    return ExportResult(content=content, media_type=MARKDOWN_MEDIA_TYPE, filename=filename)
