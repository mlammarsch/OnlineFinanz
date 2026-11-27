"""REST-Router fuer Termin- und Dauerauftraege (F-AUF-02, F-AUF-04, F-AUF-05).

Rein lesende Auslieferung des zuletzt synchronisierten Auftragsbestands. Es gibt
hier **keine** schreibende Route: Auftraege werden bei der Bank nie geaendert
(F-AUF-03). Der Bestand entsteht ausschliesslich im FinTS-Sync
(:func:`finue.services.standing_orders.get_orders_state`).

- ``GET /api/accounts/{id}/standing-orders`` liefert den Bereich eines Kontos:
  ``available`` mit Zeitstempel und Zeilen, ``unavailable`` mit festem Klartext
  ("Keine Auftragsdaten von der Bank verfuegbar", F-AUF-05) oder ``none`` fuer
  ein Datei-Konto ohne Auftragsbereich.
- ``GET /api/standing-orders`` liefert dieselben Bereiche aller aktiven
  FinTS-Konten, paginiert (F-MCP-11).

Der Router bringt ``prefix="/api"`` und die Auth-Naht ``require_user`` selbst
mit (T-088-Konvention).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.deps import get_services, get_session, require_user
from finue.api.errors import NotFoundError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.standing_order import StandingOrder
from finue.services.accounts import AccountNotFound, list_accounts
from finue.services.standing_orders import OrdersState, get_orders_state

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["standing-orders"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class StandingOrderView(BaseModel):
    """Ein Termin- oder Dauerauftrag als Anzeige (F-AUF-02)."""

    id: int
    kind: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None
    amount_cents: int | None
    currency: str | None
    interval: str | None
    next_execution: date | None
    last_seen_at: datetime


class AccountStandingOrdersView(BaseModel):
    """Auftragsbereich eines Kontos (F-AUF-04, F-AUF-05)."""

    account_id: int
    status: str
    fetched_at: datetime | None = None
    message: str | None = None
    orders: list[StandingOrderView]


class StandingOrdersListResponse(BaseModel):
    """Auftragsbereiche aller FinTS-Konten mit Cursor (F-MCP-11)."""

    items: list[AccountStandingOrdersView]
    next_cursor: str | None = None


# ---------------------------------------------------------------- Uebersetzung


def _order_dto(order: StandingOrder) -> StandingOrderView:
    """Uebersetzt einen gespeicherten Auftrag in das API-DTO (F-AUF-02)."""

    return StandingOrderView(
        id=order.id,
        kind=order.kind,
        counterparty=order.counterparty,
        counterparty_iban=order.counterparty_iban,
        purpose=order.purpose,
        amount_cents=order.amount_cents,
        currency=order.currency,
        interval=order.interval,
        next_execution=order.next_execution,
        last_seen_at=cast(datetime, order.last_seen_at),
    )


def _account_view(account_id: int, state: OrdersState | None) -> AccountStandingOrdersView:
    """Baut den Bereich eines Kontos aus dem Service-Ergebnis (F-AUF-05).

    ``None`` steht fuer ein Datei-Konto ohne Auftragsbereich.
    """

    if state is None:
        return AccountStandingOrdersView(account_id=account_id, status="none", orders=[])
    return AccountStandingOrdersView(
        account_id=account_id,
        status=state.status,
        fetched_at=state.fetched_at,
        message=state.message,
        orders=[_order_dto(order) for order in state.orders],
    )


def _page(
    items: list[AccountStandingOrdersView], limit: int, cursor: str | None
) -> tuple[list[AccountStandingOrdersView], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.get("/accounts/{account_id}/standing-orders", response_model=AccountStandingOrdersView)
def get_account_standing_orders(
    account_id: int,
    session: Session = Depends(get_session),
) -> AccountStandingOrdersView:
    """Auftragsbereich eines Kontos (F-AUF-02/04/05); Datei-Konto ohne Bereich."""

    try:
        state = get_orders_state(session, account_id)
    except LookupError as exc:
        raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.") from exc
    return _account_view(account_id, state)


@router.get("/standing-orders", response_model=StandingOrdersListResponse)
def list_standing_orders(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> StandingOrdersListResponse:
    """Auftragsbereiche aller aktiven FinTS-Konten (F-AUF-04/05); Datei-Konten fehlen."""

    params = resolve_page_params(request, services.settings)
    views: list[AccountStandingOrdersView] = []
    for account in list_accounts(session, archived=False):
        try:
            state = get_orders_state(session, account.id)
        except AccountNotFound:  # pragma: no cover - Konto kam gerade aus der Liste
            continue
        if state is None:
            # Datei-Konto ohne Auftragsbereich: entfaellt in der Gesamtliste.
            continue
        views.append(_account_view(account.id, state))
    page_items, next_cursor = _page(views, params.limit, params.cursor)
    return StandingOrdersListResponse(items=page_items, next_cursor=next_cursor)
