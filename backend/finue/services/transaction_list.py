"""Buchungsliste mit Vormerkungen, Tagessalden, Gruppenansicht und Keyset-Paginierung.

Bündelt die Fachlogik der Konten-/Gruppen-Buchungsliste, damit REST (M4) und
MCP (M9) dieselbe Schicht nutzen (D10) und der Router nur HTTP uebersetzt.

Referenzierte Anforderungen (F-BUC-01..06):

- **F-BUC-01** Kopf mit Name, Icon-Verweis und Saldo (``ListHead``).
- **F-BUC-02** Bereich "Vorgemerkte Buchungen" mit Liste, Summe und Saldo
  inklusive Vormerkungen (``PendingSection``); erscheint nur auf der ersten
  Seite (ohne Cursor).
- **F-BUC-03** Gebuchte Umsaetze nach Buchungstag absteigend in Tagesgruppen
  (``DayGroup``); Tagessumme und Saldo am Tagesende, rueckwaerts vom aktuellen
  Saldo berechnet. Der aktuelle Saldo folgt der Ankerregel aus
  ``decisions.md`` (Datei-Konten: Ankersaldo plus Buchungen; FinTS-Konten: der
  uebernommene Banksaldo).
- **F-BUC-04** Zeilenfelder (``ListRow``): id, Status, Quelle, Gegenpartei,
  vollstaendiger Verwendungszweck, Betrag, Notiz-Marker, geprueft, abgestimmt,
  Version.
- **F-BUC-05** Gruppenansicht ueber alle aktiven Mitglieder mit
  Kontokennzeichnung je Zeile; der Tagessaldo summiert nur die Buchwaehrung,
  andere Waehrungen werden nicht addiert (Regel aus T-088).
- **F-BUC-06** Keyset-Paginierung (Sortierschluessel Buchungstag plus id); der
  Einstiegs-Tagessaldo einer Folgeseite kommt aus einem SQL-Aggregat ueber die
  juengeren Buchungen, nicht aus einem Python-Vollscan.

Die Filter, der Zeitraum und die Sortierung stammen aus
:class:`finue.services.search.TransactionQuery` (gemeinsam mit Suche T-092 und
Export T-097). Die Tagessalden beruecksichtigen **alle** gebuchten, nicht
zusammengefuehrten Buchungen der beteiligten Konten - auch die von einem Filter
ausgeblendeten (F-DUP-04: zusammengefuehrte Buchungen fehlen immer).

N-DB-01: kein rohes SQL, nur SQLAlchemy-Ausdruecke; N-DB-02: Betraege in Cent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.accounts import (
    DEFAULT_CURRENCY,
    AccountNotFound,
    GroupNotFound,
    list_accounts,
)
from finue.services.search import (
    SearchScope,
    SearchValidationError,
    SortDirection,
    SortField,
    TransactionQuery,
    build_count_select,
    build_select,
    build_sum_select,
)

__all__ = [
    "DayGroup",
    "ListCursor",
    "ListHead",
    "ListRow",
    "PendingSection",
    "TransactionListResult",
    "build_transaction_list",
]

#: Status einer gebuchten Buchung (Tagesgruppen, F-BUC-03).
BOOKED: Final = "gebucht"

#: Status einer vorgemerkten Buchung (Vormerkungsbereich, F-BUC-02).
PENDING: Final = "vorgemerkt"

#: Herkunft der FinTS-Konten; sie fuehren keinen Ankersaldo (Ankerregel in
#: ``decisions.md``, identisch mit :mod:`finue.services.reconciliation`).
FINTS_SOURCE: Final = "fints"

#: Hoechstzahl vorgemerkter Zeilen in der Liste (SPEC F-MCP-11); die Summe und
#: der Saldo inklusive Vormerkungen zaehlen trotzdem alle Vormerkungen.
_PENDING_MAX: Final = 200


@dataclass(frozen=True, slots=True)
class ListCursor:
    """Keyset-Cursor: Untergrenze (Buchungstag, id) der naechsten Seite (F-BUC-06).

    Die Sortierung ist Buchungstag absteigend mit ``id`` aufsteigendem
    Tiebreaker; die naechste Seite enthaelt alle Zeilen *nach* diesem Schluessel.
    """

    booking_date: date
    transaction_id: int


@dataclass(frozen=True, slots=True)
class ListRow:
    """Eine Zeile der Buchungsliste (F-BUC-04)."""

    id: int
    account_id: int
    account_name: str
    status: str
    origin: str
    counterparty: str | None
    purpose: str | None
    amount_cents: int
    currency: str
    has_note: bool
    checked: bool
    reconciled: bool
    version: int


@dataclass(frozen=True, slots=True)
class DayGroup:
    """Tagesgruppe mit Kopf und Zeilen (F-BUC-03).

    ``sum_cents`` ist die Tagessumme der auf der Seite gezeigten Zeilen;
    ``balance_end_cents`` ist der Saldo am Tagesende (unabhaengig vom Filter).
    """

    day: date
    sum_cents: int
    balance_end_cents: int
    transactions: tuple[ListRow, ...]


@dataclass(frozen=True, slots=True)
class PendingSection:
    """Bereich "Vorgemerkte Buchungen" (F-BUC-02)."""

    items: tuple[ListRow, ...]
    sum_cents: int
    total_count: int
    balance_with_pending_cents: int


@dataclass(frozen=True, slots=True)
class ListHead:
    """Kopf der Buchungsliste: Name, Icon-Verweis und Saldo (F-BUC-01)."""

    scope: str
    id: int
    name: str
    currency: str
    balance_cents: int
    icon_url: str | None
    account_count: int


@dataclass(frozen=True, slots=True)
class TransactionListResult:
    """Ergebnis der Buchungsliste (F-BUC-01..06)."""

    head: ListHead
    pending: PendingSection | None
    days: tuple[DayGroup, ...]
    next_cursor: ListCursor | None


@dataclass(frozen=True, slots=True)
class _Scope:
    """Aufgeloester Bereich der Liste (Konto oder Gruppe)."""

    scope: str
    id: int
    name: str
    currency: str
    icon_url: str | None
    account_count: int
    accounts: tuple[Account, ...]
    book_accounts: tuple[Account, ...]


def _icon_url(account: Account) -> str | None:
    """Vorgesehener Icon-Verweis des Kontos oder ``None`` (F-BUC-01)."""

    return f"/api/accounts/{account.id}/icon" if account.icon is not None else None


def _resolve_scope(session: Session, query: TransactionQuery) -> _Scope:
    """Loest den Bereich der Liste auf und prueft ihn (F-BUC-01/05).

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        GroupNotFound: Die Gruppe existiert nicht.
        SearchValidationError: Der Bereich ist weder Konto noch Gruppe.
    """

    if query.scope is SearchScope.ACCOUNT:
        if query.account_id is None:
            raise SearchValidationError("Fuer die Buchungsliste eines Kontos ist 'account_id' erforderlich.")
        account = session.get(Account, query.account_id)
        if account is None:
            raise AccountNotFound(f"Konto {query.account_id} nicht gefunden.")
        return _Scope(
            scope="account",
            id=account.id,
            name=account.name,
            currency=account.currency,
            icon_url=_icon_url(account),
            account_count=1,
            accounts=(account,),
            book_accounts=(account,),
        )
    if query.scope is SearchScope.GROUP:
        if query.group_id is None:
            raise SearchValidationError("Fuer die Buchungsliste einer Gruppe ist 'group_id' erforderlich.")
        group = session.get(AccountGroup, query.group_id)
        if group is None:
            raise GroupNotFound(f"Gruppe {query.group_id} nicht gefunden.")
        members = tuple(account for account in list_accounts(session, archived=False) if account.group_id == group.id)
        book_accounts = tuple(account for account in members if account.currency == DEFAULT_CURRENCY)
        return _Scope(
            scope="group",
            id=group.id,
            name=group.name,
            currency=DEFAULT_CURRENCY,
            icon_url=None,
            account_count=len(members),
            accounts=members,
            book_accounts=book_accounts,
        )
    raise SearchValidationError("Die Buchungsliste kennt nur die Bereiche 'account' und 'group'.")


def _account_current_balance(account: Account, booked_sum_cents: int) -> int:
    """Aktueller Saldo (Tagesende des neuesten Tags) eines Kontos (F-BUC-03).

    Ankerregel aus ``decisions.md``: FinTS-Konten fuehren den Banksaldo
    (Autoritaet); alle anderen Konten (Datei) leiten den aktuellen Saldo aus dem
    Ankersaldo plus den gebuchten Betraegen ab.
    """

    if account.source == FINTS_SOURCE and account.balance_cents is not None:
        return account.balance_cents
    return account.opening_balance_cents + booked_sum_cents


def _booked_conditions(account_ids: Sequence[int]) -> list[ColumnElement[bool]]:
    """WHERE-Bedingungen der gebuchten, nicht zusammengefuehrten Buchungen (F-DUP-04)."""

    return [
        Transaction.account_id.in_(list(account_ids)),
        Transaction.status == BOOKED,
        Transaction.merged.is_(False),
    ]


def _booked_sum_by_account(session: Session, account_ids: Sequence[int]) -> dict[int, int]:
    """Summe der gebuchten Betraege je Konto (ein SQL-Aggregat, N-DB-01)."""

    ids = list(account_ids)
    if not ids:
        return {}
    stmt = (
        select(Transaction.account_id, func.coalesce(func.sum(Transaction.amount_cents), 0))
        .where(*_booked_conditions(ids))
        .group_by(Transaction.account_id)
    )
    return {int(account_id): int(total) for account_id, total in session.execute(stmt).all()}


def _booked_sum_after(session: Session, account_ids: Sequence[int], day: date) -> int:
    """Summe der gebuchten Betraege juenger als ``day`` (SQL-Aggregat, F-BUC-06)."""

    ids = list(account_ids)
    if not ids:
        return 0
    stmt = select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
        *_booked_conditions(ids),
        Transaction.booking_date > day,
    )
    return int(session.scalar(stmt) or 0)


def _booked_day_sums(session: Session, account_ids: Sequence[int], days: Sequence[date]) -> dict[date, int]:
    """Tagessummen aller gebuchten Betraege je Tag (ein SQL-Aggregat, F-BUC-03)."""

    ids = list(account_ids)
    if not ids or not days:
        return {}
    stmt = (
        select(Transaction.booking_date, func.coalesce(func.sum(Transaction.amount_cents), 0))
        .where(*_booked_conditions(ids), Transaction.booking_date.in_(list(days)))
        .group_by(Transaction.booking_date)
    )
    return {booking_date: int(total) for booking_date, total in session.execute(stmt).all()}


def _after_cursor(cursor: ListCursor) -> ColumnElement[bool]:
    """Keyset-Bedingung: Zeilen nach dem Cursor in der Sortierreihenfolge (F-BUC-06)."""

    return or_(
        Transaction.booking_date < cursor.booking_date,
        and_(Transaction.booking_date == cursor.booking_date, Transaction.id > cursor.transaction_id),
    )


def _row(transaction: Transaction, names: dict[int, str]) -> ListRow:
    """Uebersetzt eine Buchung in eine Listenzeile (F-BUC-04)."""

    return ListRow(
        id=transaction.id,
        account_id=transaction.account_id,
        account_name=names.get(transaction.account_id, ""),
        status=transaction.status,
        origin=transaction.origin,
        counterparty=transaction.counterparty,
        purpose=transaction.purpose,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        has_note=bool(transaction.note),
        checked=transaction.checked,
        reconciled=transaction.reconciled,
        version=transaction.version,
    )


def _day_groups(
    session: Session,
    scope: _Scope,
    page_rows: Sequence[Transaction],
    current_balance_cents: int,
    book_ids: Sequence[int],
    names: dict[int, str],
) -> tuple[DayGroup, ...]:
    """Baut die Tagesgruppen mit Tagessumme und Tagessaldo (F-BUC-03, F-BUC-06).

    Der Tagessaldo laeuft rueckwaerts vom aktuellen Saldo: der Saldo am
    Tagesende des neuesten Tags ist der aktuelle Saldo minus alle juengeren
    gebuchten Betraege (SQL-Aggregat); aeltere Tage ziehen die Tagessumme des
    jeweils juengeren Tags ab.
    """

    if not page_rows:
        return ()

    days: list[date] = []
    for row in page_rows:
        if not days or days[-1] != row.booking_date:
            days.append(row.booking_date)

    day_sums = _booked_day_sums(session, book_ids, days)
    running = current_balance_cents - _booked_sum_after(session, book_ids, days[0])

    groups: list[DayGroup] = []
    for day in days:
        day_rows = tuple(_row(row, names) for row in page_rows if row.booking_date == day)
        groups.append(
            DayGroup(
                day=day,
                sum_cents=sum(row.amount_cents for row in day_rows),
                balance_end_cents=running,
                transactions=day_rows,
            )
        )
        running -= day_sums.get(day, 0)
    return tuple(groups)


def _pending_section(
    session: Session,
    query: TransactionQuery,
    *,
    current_balance_cents: int,
    limit: int,
    names: dict[int, str],
) -> PendingSection:
    """Baut den Vormerkungsbereich mit Liste, Summe und Saldo (F-BUC-02).

    Die Summe und der Saldo inklusive Vormerkungen zaehlen alle (gefilterten)
    Vormerkungen; die Liste ist auf :data:`_PENDING_MAX` Zeilen begrenzt
    (SPEC F-MCP-11, keine unbegrenzte Abfrage).
    """

    pending_query = replace(query, status=PENDING)
    sum_cents = int(session.scalar(build_sum_select(pending_query)) or 0)
    total_count = int(session.scalar(build_count_select(pending_query)) or 0)
    cap = min(limit, _PENDING_MAX)
    rows = tuple(session.scalars(build_select(pending_query).limit(cap)).all())
    return PendingSection(
        items=tuple(_row(transaction, names) for transaction in rows),
        sum_cents=sum_cents,
        total_count=total_count,
        balance_with_pending_cents=current_balance_cents + sum_cents,
    )


def build_transaction_list(
    session: Session,
    *,
    query: TransactionQuery,
    limit: int,
    cursor: ListCursor | None = None,
) -> TransactionListResult:
    """Baut die Buchungsliste eines Kontos oder einer Gruppe (F-BUC-01..06).

    Args:
        session: Offene SQLAlchemy-Session.
        query: Filter, Zeitraum und Sortierung (Bereich ``account`` oder
            ``group``). Der Status wird intern gesetzt (gebucht/vorgemerkt).
        limit: Seitengroesse der gebuchten Umsaetze (Keyset-Paginierung).
        cursor: Keyset-Cursor der naechsten Seite oder ``None`` fuer die erste.

    Returns:
        Kopf, Vormerkungsbereich (nur ohne Cursor), Tagesgruppen und den
        Cursor der naechsten Seite.

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        GroupNotFound: Die Gruppe existiert nicht.
        SearchValidationError: Der Bereich ist weder Konto noch Gruppe.
    """

    scope = _resolve_scope(session, query)
    names = {account.id: account.name for account in scope.accounts}
    book_ids = [account.id for account in scope.book_accounts]

    booked_by_account = _booked_sum_by_account(session, book_ids)
    current_balance_cents = sum(
        _account_current_balance(account, booked_by_account.get(account.id, 0)) for account in scope.book_accounts
    )

    booked_query = replace(query, status=BOOKED, sort=SortField.DATE, direction=SortDirection.DESC)
    statement = build_select(booked_query)
    if cursor is not None:
        statement = statement.where(_after_cursor(cursor))
    rows = tuple(session.scalars(statement.limit(limit + 1)).all())

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = ListCursor(page_rows[-1].booking_date, page_rows[-1].id) if has_more and page_rows else None

    days = _day_groups(session, scope, page_rows, current_balance_cents, book_ids, names)
    pending = (
        _pending_section(
            session,
            query,
            current_balance_cents=current_balance_cents,
            limit=limit,
            names=names,
        )
        if cursor is None
        else None
    )

    head = ListHead(
        scope=scope.scope,
        id=scope.id,
        name=scope.name,
        currency=scope.currency,
        balance_cents=current_balance_cents,
        icon_url=scope.icon_url,
        account_count=scope.account_count,
    )
    return TransactionListResult(head=head, pending=pending, days=days, next_cursor=next_cursor)
