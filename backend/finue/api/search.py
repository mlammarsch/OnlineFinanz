"""REST-Router fuer die Buchungssuche (F-SUC-01..07, F-ABS-04/05).

Setzt die Suche aus :mod:`finue.services.search` als ``GET /api/search`` um.
Alle Abfragelogik liegt im Service (geteilt mit Buchungsliste T-093 und Export
T-097); dieser Router uebersetzt nur HTTP: Query-Parameter, Cursor-Paginierung
(``{items, next_cursor}``) sowie Trefferzahl und Trefferumme. Die Zeitraeume
werden mit der injizierbaren Uhr in Europe/Berlin aufgeloest (F-SUC-04).

Die Auth-Naht traegt der Router selbst (``require_user``); er bringt sein
``prefix="/api"`` mit (FastAPI 0.165 setzt das Elternpraefix nicht in
``APIRoute.path``, siehe ``decisions.md`` T-088).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.deps import get_clock, get_services, get_session, require_user
from finue.api.errors import ValidationApiError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.transaction import Transaction
from finue.services.search import (
    SearchPeriod,
    SearchScope,
    SearchValidationError,
    SortDirection,
    SortField,
    build_query,
    local_today,
    run_search,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_user)])

ScopeParam = Literal["all", "account", "group"]
PeriodParam = Literal["30", "90", "365", "current_year", "previous_year", "all", "custom"]
StatusParam = Literal["gebucht", "vorgemerkt"]
SortParam = Literal["date", "amount", "counterparty"]
DirectionParam = Literal["asc", "desc"]


class SearchItem(BaseModel):
    """Ein Suchtreffer: die wesentlichen Buchungsfelder (F-SUC-01, F-DET-01)."""

    id: int
    account_id: int
    status: str
    booking_date: date
    value_date: date | None
    amount_cents: int
    currency: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None
    note: str | None
    checked: bool
    reconciled: bool
    external_ref: str | None
    origin: str
    version: int


class SearchResponse(BaseModel):
    """Suchergebnis mit Paginierung, Trefferzahl und Trefferumme (F-SUC-06, F-MCP-11)."""

    items: list[SearchItem]
    next_cursor: str | None = None
    total_count: int
    total_cents: int


def _item(transaction: Transaction) -> SearchItem:
    """Uebersetzt eine Buchung in einen Suchtreffer."""

    return SearchItem(
        id=transaction.id,
        account_id=transaction.account_id,
        status=transaction.status,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
        note=transaction.note,
        checked=transaction.checked,
        reconciled=transaction.reconciled,
        external_ref=transaction.external_ref,
        origin=transaction.origin,
        version=transaction.version,
    )


@router.get("/search", response_model=SearchResponse)
def search_route(
    request: Request,
    q: str | None = None,
    scope: ScopeParam = "all",
    account_id: int | None = None,
    group_id: int | None = None,
    period: PeriodParam = "365",
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    checked: bool | None = None,
    reconciled: bool | None = None,
    with_note: bool = False,
    status: StatusParam | None = None,
    sort: SortParam = "date",
    direction: DirectionParam = "desc",
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> SearchResponse:
    """Sucht Buchungen mit Bereich, Zeitraum, Filtern, Sortierung und Paginierung.

    Alle Anforderungen F-SUC-01..07 und F-ABS-04/05 werden hier zusammen
    ausgeloest; die Auswertung liegt in :mod:`finue.services.search`.
    """

    params = resolve_page_params(request, services.settings)
    today = local_today(clock)
    try:
        query = build_query(
            today=today,
            scope=SearchScope(scope),
            account_id=account_id,
            group_id=group_id,
            search=q,
            period=SearchPeriod(period),
            from_date=from_date,
            to_date=to_date,
            checked=checked,
            reconciled=reconciled,
            with_note=with_note,
            status=status,
            sort=SortField(sort),
            direction=SortDirection(direction),
        )
    except SearchValidationError as exc:
        raise ValidationApiError(str(exc)) from exc

    offset = decode_cursor(params.cursor) if params.cursor else 0
    result = run_search(session, query, limit=params.limit, offset=offset)
    items = [_item(transaction) for transaction in result.items]
    next_offset = offset + len(items)
    next_cursor = encode_cursor(next_offset) if next_offset < result.total_count else None
    return SearchResponse(
        items=items,
        next_cursor=next_cursor,
        total_count=result.total_count,
        total_cents=result.total_cents,
    )
