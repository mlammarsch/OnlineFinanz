"""REST-Router der Buchungsliste (F-BUC-01..06).

Setzt die Buchungsliste je Konto und je Gruppe um:

- ``GET /api/accounts/{account_id}/transactions``
- ``GET /api/groups/{group_id}/transactions``

Die Fachlogik liegt in :mod:`finue.services.transaction_list` (gemeinsam mit
MCP, D10); dieser Router uebersetzt nur HTTP: Query-Parameter, Keyset-Cursor
(``{items, next_cursor}``) und die DTOs. Filter, Zeitraum und Sortierung stammen
aus :class:`finue.services.search.TransactionQuery` (T-092); die Tagesgruppen
sind immer nach Buchungstag absteigend (F-BUC-03). Der Keyset-Cursor traegt
Buchungstag und ID; ungueltige Cursor liefern 422 (F-CON-02).

Die Auth-Naht traegt der Router selbst (``require_user``); er bringt sein
``prefix="/api"`` mit (FastAPI 0.165 setzt das Elternpraefix nicht in
``APIRoute.path``, siehe ``decisions.md`` T-088).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from datetime import date, datetime
from typing import Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.deps import get_clock, get_services, get_session, require_user
from finue.api.errors import NotFoundError, ValidationApiError
from finue.api.pagination import resolve_limit
from finue.services.accounts import AccountNotFound, GroupNotFound
from finue.services.search import (
    SearchPeriod,
    SearchScope,
    SearchValidationError,
    SortDirection,
    SortField,
    TransactionQuery,
    build_query,
    local_today,
)
from finue.services.transaction_list import (
    ListCursor,
    ListRow,
    TransactionListResult,
    build_transaction_list,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["transactions"], dependencies=[Depends(require_user)])

PeriodParam = Literal["30", "90", "365", "current_year", "previous_year", "all", "custom"]

#: Praefix eines Keyset-Cursors (unterscheidet ihn vom Offset-Cursor der Suche).
_CURSOR_PREFIX: Final = "k"

#: Hoechstzahl an Ziffern der ID (19 fuer einen 64-Bit-Wert). Eine laengere
#: Ziffernfolge wird vor ``int`` abgelehnt, damit ein Riesen-Cursor 422 statt
#: 500 liefert (F-CON-02).
_MAX_ID_DIGITS: Final = 19


# ------------------------------------------------------------------- DTOs


class TransactionRow(BaseModel):
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


class TransactionDay(BaseModel):
    """Tagesgruppe mit Kopf und Zeilen (F-BUC-03)."""

    day: date
    sum_cents: int
    balance_end_cents: int
    transactions: list[TransactionRow]


class PendingSectionDTO(BaseModel):
    """Bereich "Vorgemerkte Buchungen" (F-BUC-02)."""

    items: list[TransactionRow]
    sum_cents: int
    total_count: int
    balance_with_pending_cents: int


class TransactionListHead(BaseModel):
    """Kopf der Buchungsliste: Name, Icon-Verweis und Saldo (F-BUC-01)."""

    scope: str
    id: int
    name: str
    currency: str
    balance_cents: int
    icon_url: str | None
    account_count: int


class TransactionListResponse(BaseModel):
    """Antwort der Buchungsliste mit Keyset-Cursor (SPEC F-MCP-11)."""

    head: TransactionListHead
    pending: PendingSectionDTO | None = None
    days: list[TransactionDay]
    next_cursor: str | None = None


# ------------------------------------------------------------------- Cursor


def encode_keyset_cursor(cursor: ListCursor) -> str:
    """Kodiert einen Keyset-Cursor als undurchsichtige Zeichenkette (F-BUC-06)."""

    raw = f"{_CURSOR_PREFIX}{cursor.booking_date.isoformat()}:{cursor.transaction_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_keyset_cursor(cursor: str) -> ListCursor:
    """Dekodiert einen Keyset-Cursor; ungueltige Werte werden als 422 gemeldet.

    Raises:
        ValidationApiError: Der Cursor ist nicht base64, hat kein gueltiges
            Datum oder keine gueltige ID.
    """

    invalid = ValidationApiError("Der Cursor ist ungueltig.", details={"field": "cursor"})
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise invalid from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise invalid
    body = raw[1:]
    day_text, separator, id_text = body.partition(":")
    if not separator:
        raise invalid
    try:
        booking_date = date.fromisoformat(day_text)
    except ValueError as exc:
        raise invalid from exc
    if not id_text or not id_text.isdigit() or len(id_text) > _MAX_ID_DIGITS:
        raise invalid
    transaction_id = int(id_text)
    if transaction_id < 1:
        raise invalid
    return ListCursor(booking_date=booking_date, transaction_id=transaction_id)


# ---------------------------------------------------------------- Uebersetzung


def _response(result: TransactionListResult) -> TransactionListResponse:
    """Uebersetzt das Service-Ergebnis in das API-DTO (F-BUC-01..06)."""

    head = TransactionListHead(
        scope=result.head.scope,
        id=result.head.id,
        name=result.head.name,
        currency=result.head.currency,
        balance_cents=result.head.balance_cents,
        icon_url=result.head.icon_url,
        account_count=result.head.account_count,
    )
    pending = (
        PendingSectionDTO(
            items=[_row_dto(row) for row in result.pending.items],
            sum_cents=result.pending.sum_cents,
            total_count=result.pending.total_count,
            balance_with_pending_cents=result.pending.balance_with_pending_cents,
        )
        if result.pending is not None
        else None
    )
    days = [
        TransactionDay(
            day=group.day,
            sum_cents=group.sum_cents,
            balance_end_cents=group.balance_end_cents,
            transactions=[_row_dto(row) for row in group.transactions],
        )
        for group in result.days
    ]
    next_cursor = encode_keyset_cursor(result.next_cursor) if result.next_cursor is not None else None
    return TransactionListResponse(head=head, pending=pending, days=days, next_cursor=next_cursor)


def _row_dto(row: ListRow) -> TransactionRow:
    """Uebersetzt eine Service-Zeile in das API-DTO (F-BUC-04)."""

    return TransactionRow(
        id=row.id,
        account_id=row.account_id,
        account_name=row.account_name,
        status=row.status,
        origin=row.origin,
        counterparty=row.counterparty,
        purpose=row.purpose,
        amount_cents=row.amount_cents,
        currency=row.currency,
        has_note=row.has_note,
        checked=row.checked,
        reconciled=row.reconciled,
        version=row.version,
    )


def _build_query(
    services: AppServices,
    clock: Callable[[], datetime],
    *,
    scope: SearchScope,
    account_id: int | None,
    group_id: int | None,
    q: str | None,
    period: str,
    from_date: date | None,
    to_date: date | None,
    checked: bool | None,
    reconciled: bool | None,
    with_note: bool,
) -> TransactionQuery:
    """Baut die gemeinsame :class:`TransactionQuery` (F-SUC-04, F-BUC-03)."""

    try:
        return build_query(
            today=local_today(clock),
            scope=scope,
            account_id=account_id,
            group_id=group_id,
            search=q,
            period=SearchPeriod(period),
            from_date=from_date,
            to_date=to_date,
            checked=checked,
            reconciled=reconciled,
            with_note=with_note,
            status=None,
            sort=SortField.DATE,
            direction=SortDirection.DESC,
        )
    except SearchValidationError as exc:
        raise ValidationApiError(str(exc)) from exc


def _load_list(
    request: Request,
    session: Session,
    services: AppServices,
    clock: Callable[[], datetime],
    *,
    scope: SearchScope,
    account_id: int | None,
    group_id: int | None,
    q: str | None,
    period: str,
    from_date: date | None,
    to_date: date | None,
    checked: bool | None,
    reconciled: bool | None,
    with_note: bool,
    cursor: str | None,
) -> TransactionListResponse:
    """Gemeinsamer Ablauf beider Listenrouten (F-BUC-01..06)."""

    query = _build_query(
        services,
        clock,
        scope=scope,
        account_id=account_id,
        group_id=group_id,
        q=q,
        period=period,
        from_date=from_date,
        to_date=to_date,
        checked=checked,
        reconciled=reconciled,
        with_note=with_note,
    )
    limit = resolve_limit(request, services.settings)
    list_cursor = decode_keyset_cursor(cursor) if cursor else None
    try:
        result = build_transaction_list(session, query=query, limit=limit, cursor=list_cursor)
    except AccountNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except GroupNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    return _response(result)


# ------------------------------------------------------------------- Routen


@router.get("/accounts/{account_id}/transactions", response_model=TransactionListResponse)
def account_transactions_route(
    account_id: int,
    request: Request,
    q: str | None = None,
    period: PeriodParam = "365",
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    checked: bool | None = None,
    reconciled: bool | None = None,
    with_note: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> TransactionListResponse:
    """Buchungsliste eines Kontos: Kopf, Vormerkungen und Tagesgruppen (F-BUC-01..06)."""

    return _load_list(
        request,
        session,
        services,
        clock,
        scope=SearchScope.ACCOUNT,
        account_id=account_id,
        group_id=None,
        q=q,
        period=period,
        from_date=from_date,
        to_date=to_date,
        checked=checked,
        reconciled=reconciled,
        with_note=with_note,
        cursor=cursor,
    )


@router.get("/groups/{group_id}/transactions", response_model=TransactionListResponse)
def group_transactions_route(
    group_id: int,
    request: Request,
    q: str | None = None,
    period: PeriodParam = "365",
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    checked: bool | None = None,
    reconciled: bool | None = None,
    with_note: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> TransactionListResponse:
    """Buchungsliste einer Gruppe mit Kontokennzeichnung je Zeile (F-BUC-05)."""

    return _load_list(
        request,
        session,
        services,
        clock,
        scope=SearchScope.GROUP,
        account_id=None,
        group_id=group_id,
        q=q,
        period=period,
        from_date=from_date,
        to_date=to_date,
        checked=checked,
        reconciled=reconciled,
        with_note=with_note,
        cursor=cursor,
    )
