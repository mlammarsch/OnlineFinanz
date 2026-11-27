"""Gemeinsamer Such- und Filterbaustein fuer Buchungen (F-SUC-01..07, F-ABS-04/05).

Dieses Modul enthaelt die einzige Abfragelogik fuer gefilterte Buchungsmengen.
Es wird von der Suche (``GET /api/search``), der Buchungsliste (T-093) und dem
Export (T-097) genutzt, damit alle drei dieselben Grenzen, Filter und die
Normalisierung aus :mod:`finue.services.search_text` verwenden (D10: MCP nutzt
spaeter dieselbe Schicht).

Umfang und Anforderungen:

* **F-SUC-01** Ein Suchfeld durchsucht Gegenpartei, Verwendungszweck, Notiz,
  IBAN und Betrag. Mehrere Begriffe werden UND-verknuepft. Die Suche laeuft
  portable ueber ``LIKE`` auf der normalisierten Spalte ``search_text``
  (Aufbau in :mod:`finue.services.search_text`); ``%`` und ``_`` im Begriff
  werden maskiert. Gross-/Kleinschreibung ist durch die Normalisierung egal.
* **F-SUC-02** Betragsbegriffe (``48,83``, ``48.83``, ``-48,83``, ``1.234,56``)
  treffen den Betrag: mit Vorzeichen exakt, ohne Vorzeichen als Absolutwert.
  Betrags- und Textbegriffe lassen sich in einer Eingabe kombinieren.
* **F-SUC-03** Bereich: ein Konto, eine Gruppe oder alle aktiven Konten.
* **F-SUC-04** Zeitraum 30/90/365 Tage (Standard)/laufendes Jahr/Vorjahr/
  von-bis/alle; ``heute`` wird aus der injizierbaren Uhr in der Zeitzone
  Europe/Berlin bestimmt.
* **F-SUC-05** Sortierung Datum, Betrag, Gegenpartei auf-/absteigend mit
  stabilem Tiebreaker auf ``id``.
* **F-SUC-06** Nur portable SQLAlchemy-Ausdruecke, keine datenbankspezifische
  Volltext-Erweiterung (D5).
* **F-SUC-07 / F-ABS-04** Zusatzfilter abgestimmt und geprueft (ja/nein/alle)
  sowie "mit Notiz".
* **F-ABS-05** Die optionale externe Referenz ist durchsuchbar (sie liegt in
  ``search_text``).
* Zusammengefuehrte Buchungen (``merged``) sind immer ausgeschlossen (F-DUP-04).

N-DB-01: Es wird kein SQL-Text geschrieben, nur SQLAlchemy-Ausdruecke.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.transaction import Transaction
from finue.services.search_text import normalize_query_term

__all__ = [
    "DEFAULT_TIMEZONE",
    "AmountTerm",
    "DateRange",
    "SearchPeriod",
    "SearchResult",
    "SearchScope",
    "SearchValidationError",
    "SortDirection",
    "SortField",
    "TransactionQuery",
    "build_count_select",
    "build_query",
    "build_select",
    "build_sum_select",
    "local_today",
    "parse_amount_term",
    "resolve_period",
    "run_search",
    "split_search_terms",
]

#: Zeitzone, in der "heute" fuer die relativen Zeitraeume bestimmt wird (F-SUC-04).
DEFAULT_TIMEZONE: Final = "Europe/Berlin"

#: Anzahl Tage der relativen Zeitraeume (F-SUC-04).
_DAY_PERIODS: Final[dict[str, int]] = {"30": 30, "90": 90, "365": 365}

#: Erlaubte Zeichen eines Betragsbegriffs (Vorzeichen, Ziffern, Trennzeichen).
_AMOUNT_SHAPE: Final = re.compile(r"[+-]?\d[\d.,]*")

_STATUS_VALUES: Final = ("gebucht", "vorgemerkt")


class SearchValidationError(ValueError):
    """Ungueltige Suchparameter; die Meldung ist deutscher Klartext."""


class SearchScope(StrEnum):
    """Bereich einer Suche (F-SUC-03)."""

    ALL = "all"
    ACCOUNT = "account"
    GROUP = "group"


class SearchPeriod(StrEnum):
    """Vordefinierter Zeitraum einer Suche (F-SUC-04)."""

    DAYS_30 = "30"
    DAYS_90 = "90"
    DAYS_365 = "365"
    CURRENT_YEAR = "current_year"
    PREVIOUS_YEAR = "previous_year"
    ALL = "all"
    CUSTOM = "custom"


class SortField(StrEnum):
    """Sortierfeld einer Suche (F-SUC-05)."""

    DATE = "date"
    AMOUNT = "amount"
    COUNTERPARTY = "counterparty"


class SortDirection(StrEnum):
    """Sortierrichtung einer Suche (F-SUC-05)."""

    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class DateRange:
    """Inklusiver Buchungstag-Zeitraum; ``None`` bedeutet offen (F-SUC-04)."""

    start: date | None
    end: date | None


@dataclass(frozen=True, slots=True)
class AmountTerm:
    """Ein als Betrag erkannter Suchbegriff (F-SUC-02).

    ``value_cents`` traegt das Vorzeichen bereits; ``explicit_sign`` sagt, ob
    der Nutzer ein ``+``/``-`` geschrieben hat (dann exakter Wert, sonst
    Absolutwert).
    """

    value_cents: int
    explicit_sign: bool


@dataclass(frozen=True, slots=True)
class TransactionQuery:
    """Beschreibung einer Buchungsabfrage (F-SUC-01..07, F-ABS-04/05).

    Die Zeitraeume sind bereits in absolute Grenzen aufgeloest
    (:func:`resolve_period`), damit die Abfrage deterministisch ist.
    """

    scope: SearchScope = SearchScope.ALL
    account_id: int | None = None
    group_id: int | None = None
    search: str | None = None
    date_range: DateRange | None = None
    checked: bool | None = None
    reconciled: bool | None = None
    with_note: bool = False
    status: str | None = None
    sort: SortField = SortField.DATE
    direction: SortDirection = SortDirection.DESC


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Ergebnis einer Suche: Seiteneintraege, Trefferzahl und Trefferumme."""

    items: tuple[Transaction, ...]
    total_count: int
    total_cents: int


def parse_amount_term(term: str | None) -> AmountTerm | None:
    """Erkennt einen Betragsbegriff und liefert ihn in Cent (F-SUC-02).

    Unterstuetzt die deutsche (``1.234,56``) und die englische Schreibweise
    (``1,234.56``) sowie das Vorzeichen. Ohne Dezimalteil (z. B. ``2020``) gilt
    der Begriff als Text, nicht als Betrag.

    Returns:
        Den erkannten Betrag oder ``None``, wenn der Begriff kein Betrag ist.
    """

    if not term:
        return None
    text = term.strip()
    if not text or _AMOUNT_SHAPE.fullmatch(text) is None:
        return None

    sign = 1
    explicit = False
    if text[0] in "+-":
        explicit = True
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    if not text or ("." not in text and "," not in text):
        return None

    last_dot = text.rfind(".")
    last_comma = text.rfind(",")
    if last_dot > last_comma:
        thousand_sep, dec_pos = ",", last_dot
    else:
        thousand_sep, dec_pos = ".", last_comma

    int_part = text[:dec_pos]
    frac_part = text[dec_pos + 1 :]
    if not frac_part.isdigit() or len(frac_part) > 2:
        return None

    if thousand_sep in int_part:
        groups = int_part.split(thousand_sep)
        if (
            not groups[0].isdigit()
            or not 1 <= len(groups[0]) <= 3
            or any(not group.isdigit() or len(group) != 3 for group in groups[1:])
        ):
            return None
        int_digits = "".join(groups)
    else:
        int_digits = int_part
    if not int_digits.isdigit():
        return None

    cents = int(int_digits) * 100 + int(frac_part.ljust(2, "0"))
    return AmountTerm(value_cents=sign * cents, explicit_sign=explicit)


def split_search_terms(search: str | None) -> tuple[list[str], list[AmountTerm]]:
    """Trennt eine Sucheingabe in Text- und Betragsbegriffe (F-SUC-01, F-SUC-02).

    Die Begriffe werden an Leerraum getrennt und mit derselben Normalisierung
    wie die Suchspalte vereinheitlicht (:func:`normalize_query_term`), damit
    Gross-/Kleinschreibung egal ist.
    """

    text_terms: list[str] = []
    amount_terms: list[AmountTerm] = []
    if not search:
        return text_terms, amount_terms
    for raw in search.split():
        amount = parse_amount_term(raw)
        if amount is not None:
            amount_terms.append(amount)
            continue
        normalized = normalize_query_term(raw)
        if normalized:
            text_terms.append(normalized)
    return text_terms, amount_terms


def local_today(clock: Callable[[], datetime], *, timezone_name: str = DEFAULT_TIMEZONE) -> date:
    """Bestimmt "heute" aus der injizierbaren Uhr in Europe/Berlin (F-SUC-04).

    Naive Zeitstempel gelten als UTC. So bleibt der Zeitraumfilter auch dann
    stabil, wenn die Uhr fest verdrahtet ist (Tests).
    """

    moment = clock()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(timezone_name)).date()


def resolve_period(
    period: SearchPeriod,
    *,
    today: date,
    from_date: date | None = None,
    to_date: date | None = None,
) -> DateRange | None:
    """Loest einen Zeitraum in absolute Grenzen auf (F-SUC-04).

    ``None`` als Ergebnis bedeutet "alle" (kein Zeitraumfilter).

    Raises:
        SearchValidationError: Bei ``custom`` fehlen beide Grenzen oder liegen
            in falscher Reihenfolge.
    """

    if period is SearchPeriod.ALL:
        return None
    if period is SearchPeriod.CUSTOM:
        if from_date is None and to_date is None:
            raise SearchValidationError("Fuer den Zeitraum 'custom' muss mindestens 'von' oder 'bis' angegeben werden.")
        if from_date is not None and to_date is not None and from_date > to_date:
            raise SearchValidationError("Das Von-Datum darf nicht nach dem Bis-Datum liegen.")
        return DateRange(start=from_date, end=to_date)
    if period is SearchPeriod.CURRENT_YEAR:
        return DateRange(start=date(today.year, 1, 1), end=date(today.year, 12, 31))
    if period is SearchPeriod.PREVIOUS_YEAR:
        year = today.year - 1
        return DateRange(start=date(year, 1, 1), end=date(year, 12, 31))
    days = _DAY_PERIODS[period.value]
    return DateRange(start=today - timedelta(days=days), end=today)


def build_query(
    *,
    today: date,
    scope: SearchScope = SearchScope.ALL,
    account_id: int | None = None,
    group_id: int | None = None,
    search: str | None = None,
    period: SearchPeriod = SearchPeriod.DAYS_365,
    from_date: date | None = None,
    to_date: date | None = None,
    checked: bool | None = None,
    reconciled: bool | None = None,
    with_note: bool = False,
    status: str | None = None,
    sort: SortField = SortField.DATE,
    direction: SortDirection = SortDirection.DESC,
) -> TransactionQuery:
    """Baut und prueft eine :class:`TransactionQuery` (F-SUC-03..07).

    Raises:
        SearchValidationError: Bereich, Zeitraum oder Status sind ungueltig.
    """

    if scope is SearchScope.ACCOUNT and account_id is None:
        raise SearchValidationError("Fuer den Bereich 'account' ist 'account_id' erforderlich.")
    if scope is SearchScope.GROUP and group_id is None:
        raise SearchValidationError("Fuer den Bereich 'group' ist 'group_id' erforderlich.")
    if status is not None and status not in _STATUS_VALUES:
        raise SearchValidationError("Der Status muss 'gebucht', 'vorgemerkt' oder leer sein.")
    date_range = resolve_period(period, today=today, from_date=from_date, to_date=to_date)
    return TransactionQuery(
        scope=scope,
        account_id=account_id,
        group_id=group_id,
        search=search,
        date_range=date_range,
        checked=checked,
        reconciled=reconciled,
        with_note=with_note,
        status=status,
        sort=sort,
        direction=direction,
    )


# ------------------------------------------------------------------- Bedingungen


def _escape_like(term: str) -> str:
    """Maskiert ``%``, ``_`` und den Escape-Char in einem LIKE-Begriff (F-SUC-01)."""

    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scope_condition(query: TransactionQuery) -> ColumnElement[bool]:
    """Baut die Konto-/Gruppen-/Alle-Bedingung (F-SUC-03)."""

    if query.scope is SearchScope.ACCOUNT:
        if query.account_id is None:
            raise SearchValidationError("Fuer den Bereich 'account' ist 'account_id' erforderlich.")
        return Transaction.account_id == query.account_id
    if query.scope is SearchScope.GROUP:
        if query.group_id is None:
            raise SearchValidationError("Fuer den Bereich 'group' ist 'group_id' erforderlich.")
        member_ids = select(Account.id).where(Account.group_id == query.group_id, Account.active.is_(True))
        return Transaction.account_id.in_(member_ids)
    active_ids = select(Account.id).where(Account.active.is_(True))
    return Transaction.account_id.in_(active_ids)


def _amount_condition(amount: AmountTerm) -> ColumnElement[bool]:
    """Baut die Betragsbedingung: exakt mit Vorzeichen, sonst Absolutwert (F-SUC-02)."""

    if amount.explicit_sign:
        return Transaction.amount_cents == amount.value_cents
    magnitude = abs(amount.value_cents)
    return Transaction.amount_cents.in_([magnitude, -magnitude])


def _conditions(query: TransactionQuery) -> list[ColumnElement[bool]]:
    """Sammelt alle WHERE-Bedingungen einer Abfrage (F-SUC-01..07, F-ABS-04/05)."""

    conditions: list[ColumnElement[bool]] = [
        # Zusammengefuehrte Buchungen sind nie Teil einer Suche (F-DUP-04).
        Transaction.merged.is_(False),
        _scope_condition(query),
    ]
    if query.status is not None:
        conditions.append(Transaction.status == query.status)
    if query.date_range is not None:
        if query.date_range.start is not None:
            conditions.append(Transaction.booking_date >= query.date_range.start)
        if query.date_range.end is not None:
            conditions.append(Transaction.booking_date <= query.date_range.end)
    if query.checked is not None:
        conditions.append(Transaction.checked.is_(query.checked))
    if query.reconciled is not None:
        conditions.append(Transaction.reconciled.is_(query.reconciled))
    if query.with_note:
        conditions.append(Transaction.note.is_not(None))
        conditions.append(Transaction.note != "")

    text_terms, amount_terms = split_search_terms(query.search)
    conditions.extend(Transaction.search_text.like(f"%{_escape_like(term)}%", escape="\\") for term in text_terms)
    conditions.extend(_amount_condition(amount) for amount in amount_terms)
    return conditions


def _sort_expression(query: TransactionQuery) -> ColumnElement[Any]:
    """Liefert den primaeren Sortierausdruck (F-SUC-05)."""

    if query.sort is SortField.AMOUNT:
        return cast("ColumnElement[Any]", Transaction.amount_cents)
    if query.sort is SortField.COUNTERPARTY:
        return cast("ColumnElement[Any]", func.lower(func.coalesce(Transaction.counterparty, "")))
    return cast("ColumnElement[Any]", Transaction.booking_date)


def _order_by(query: TransactionQuery) -> list[ColumnElement[Any]]:
    """Baut die Sortierung samt stabilem Tiebreaker ``id`` (F-SUC-05)."""

    primary: ColumnElement[Any] = _sort_expression(query)
    ordered: ColumnElement[Any] = primary.desc() if query.direction is SortDirection.DESC else primary.asc()
    return [ordered, Transaction.id.asc()]


# ------------------------------------------------------------------- Abfragebau


def build_select(query: TransactionQuery) -> Select[Transaction]:
    """Baut die SELECT-Abfrage der Suchtreffer (F-SUC-01..07).

    Nur portable SQLAlchemy-Ausdruecke (F-SUC-06); der Compile-Test prueft die
    Abfrage gegen den postgresql-, mysql- und sqlite-Dialekt.
    """

    return select(Transaction).where(*_conditions(query)).order_by(*_order_by(query))


def build_count_select(query: TransactionQuery) -> Select[int]:
    """Baut die Trefferzahl-Abfrage (DoD: Trefferzahl)."""

    return select(func.count()).select_from(Transaction).where(*_conditions(query))


def build_sum_select(query: TransactionQuery) -> Select[int]:
    """Baut die Treffersummen-Abfrage in Cent (DoD: Trefferumme)."""

    expression = func.coalesce(func.sum(Transaction.amount_cents), 0)
    return select(expression).select_from(Transaction).where(*_conditions(query))


def run_search(session: Session, query: TransactionQuery, *, limit: int, offset: int) -> SearchResult:
    """Fuehrt eine Abfrage seitenweise aus und liefert Zahl und Summe (F-SUC-01..07).

    ``total_count`` und ``total_cents`` beziehen sich auf die gesamte gefilterte
    Menge, nicht nur die Seite. ``total_cents`` ist die vorzeichenbehaftete
    Summe der Trefferbetraege (wie die Auswahlsumme in F-SEL-01); bei
    gemischten Waehrungen ist sie nur innerhalb eines Kontos/einer Gruppe
    aussagekraeftig.
    """

    statement = build_select(query).limit(limit).offset(offset)
    items = tuple(session.scalars(statement).all())
    total_count = int(session.scalar(build_count_select(query)) or 0)
    total_cents = int(session.scalar(build_sum_select(query)) or 0)
    return SearchResult(items=items, total_count=total_count, total_cents=total_cents)
