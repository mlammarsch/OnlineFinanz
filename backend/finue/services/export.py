"""Export-Service fuer JSON und Markdown (F-EXP-01, F-EXP-02, F-EXP-04, F-EXP-05).

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
* Zusammengefuehrte Buchungen (``merged``, F-DUP-04) sind immer ausgeschlossen.

Grenze: Mehr als ``max_rows`` Buchungen lehnt der Service mit
:class:`ExportLimitExceeded` ab, damit ein unbeabsichtigter Massenexport nicht
den Prozess oder die Antwort sprengt. N-DB-01: kein rohes SQL, nur
SQLAlchemy-Ausdruecke.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
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
    "JSON_MEDIA_TYPE",
    "MARKDOWN_MEDIA_TYPE",
    "ExportFormat",
    "ExportLimitExceeded",
    "ExportRequest",
    "ExportResult",
    "ExportScope",
    "ExportValidationError",
    "build_filename",
    "build_export_document",
    "format_amount_de",
    "format_date_de",
    "render_markdown",
    "run_export",
    "slugify",
]

#: Medientypen der Exportantworten (F-EXP-01, F-EXP-05).
JSON_MEDIA_TYPE: Final = "application/json"
MARKDOWN_MEDIA_TYPE: Final = "text/markdown"
CLIPBOARD_MEDIA_TYPE: Final = "text/plain"

#: Deutsche Umlaute und das scharfe s fuer den ASCII-Dateinamen (F-EXP-02).
_TRANSCRIPTION: Final = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})

#: Ersatzlabel, wenn ein Name keinen verwertbaren ASCII-Rest hat (F-EXP-02).
_FALLBACK_SLUG: Final = "export"


class ExportFormat(StrEnum):
    """Ausgabeformat eines Exports (F-EXP-01)."""

    JSON = "json"
    MARKDOWN = "md"


class ExportScope(StrEnum):
    """Umfang eines Exports (F-EXP-01)."""

    ALL = "all"
    ACCOUNT = "account"
    GROUP = "group"
    SEARCH = "search"
    SELECTION = "selection"


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


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Fertiger Export: Inhalt, Medientyp und Dateiname (F-EXP-02)."""

    content: str
    media_type: str
    filename: str


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

    Der Zeitraum steht als ``JJJJ-MM-TT_JJJJ-MM-TT``; die Endung ist ``json``
    bzw. ``md``.
    """

    extension = "json" if export_format is ExportFormat.JSON else "md"
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
    content = render_markdown(
        label=label,
        start=start,
        end=end,
        transactions=transactions,
        accounts_by_id=accounts_by_id,
        group_view=group_view,
    )
    return ExportResult(content=content, media_type=MARKDOWN_MEDIA_TYPE, filename=filename)
