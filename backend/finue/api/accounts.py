"""REST-Router fuer Konten (F-KTO-01..04, F-KTO-05b, F-KTO-06, F-CON-02).

Setzt die API-Konventionen aus ``docs/api.md`` um: einheitliches Fehlerformat,
Paginierung ``{items, next_cursor}``, Versionsfeld mit 409 und aktuellem Stand
(:class:`finue.api.errors.ConflictError`) sowie die Ereignisse
``account.changed`` (F-CON-04). Die Fachlogik liegt in
:mod:`finue.services.accounts`; dieser Router uebersetzt nur HTTP.

Maskierte IBAN: Die Kontenliste zeigt die IBAN nach der Regel in ``docs/api.md``
(erste und letzte vier Zeichen sichtbar); die Detailansicht liefert die volle
IBAN zum Pflegen (F-KTO-04).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.conventions import VersionedRequest
from finue.api.deps import get_event_bus, get_services, get_session, get_sync_registry, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.account import Account
from finue.services.accounts import (
    AccountNotFound,
    AccountSyncRunning,
    AccountValidationError,
    AccountVersionConflict,
    GroupNotFound,
    TotalComposition,
    compute_total,
    create_offline_account,
    delete_account,
    get_account,
    latest_sync_times,
    list_accounts,
    mask_iban,
    set_archived,
    update_account,
)
from finue.services.events import EventBus
from finue.services.sync_registry import SyncRegistry

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["accounts"], dependencies=[Depends(require_user)])

#: Vorgesehener Icon-Endpunkt je Konto (Auslieferung folgt mit F-KTO-11/T-090).
_ICON_URL_TEMPLATE = "/api/accounts/{account_id}/icon"


# ------------------------------------------------------------------- DTOs


class ForeignTotal(BaseModel):
    """Summe einer Nicht-Buchwaehrung, getrennt ausgewiesen (F-KTO-02)."""

    currency: str
    amount_cents: int
    account_ids: list[int]


class TotalEntry(BaseModel):
    """Ein Konto in der Zusammensetzung des Gesamtsaldos (F-KTO-02)."""

    id: int
    name: str
    currency: str
    amount_cents: int | None


class AccountTotal(BaseModel):
    """Gesamtsaldo samt Zusammensetzung fuer das Info-Symbol (F-KTO-02)."""

    currency: str
    amount_cents: int
    included: list[TotalEntry]
    excluded: list[TotalEntry]
    foreign: list[ForeignTotal]


class AccountSummary(BaseModel):
    """Eintrag der Kontenliste (F-KTO-01)."""

    id: int
    name: str
    account_type: str | None
    iban_masked: str | None
    currency: str
    balance_cents: int | None
    balance_at: datetime | None
    last_sync_at: datetime | None
    source: str
    group_id: int | None
    include_in_total: bool
    has_icon: bool
    icon_url: str | None
    version: int


class AccountDetail(AccountSummary):
    """Kontodetails zum Ansehen und Pflegen (F-KTO-04)."""

    iban: str | None
    opening_balance_cents: int
    active: bool
    bank_access_id: int | None


class AccountListResponse(BaseModel):
    """Kontenliste mit Gesamtsaldo (F-KTO-01, F-KTO-02, F-MCP-11)."""

    items: list[AccountSummary]
    total: AccountTotal
    next_cursor: str | None = None


class AccountCreateRequest(BaseModel):
    """Anlage eines Offline-Kontos fuer den Datei-Import (F-KTO-05b)."""

    name: str = Field(min_length=1, max_length=255)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    account_type: str | None = None
    iban: str | None = None
    opening_balance_cents: int | None = None
    opening_balance_date: date | None = None
    group_id: int | None = None
    include_in_total: bool = True


class AccountUpdateRequest(VersionedRequest):
    """Aenderung von Kontodetails mit Versionspruefung (F-KTO-04, F-CON-02)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    account_type: str | None = None
    iban: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    group_id: int | None = None
    include_in_total: bool | None = None


# ---------------------------------------------------------------- Uebersetzung


def _icon_url(has_icon: bool, account_id: int) -> str | None:
    """Liefert den vorgesehenen Icon-Verweis oder ``None`` (F-KTO-01)."""

    return _ICON_URL_TEMPLATE.format(account_id=account_id) if has_icon else None


def _summary(account: Account, last_sync: datetime | None) -> AccountSummary:
    """Baut einen Listeneintrag aus einem Konto."""

    has_icon = account.icon is not None
    return AccountSummary(
        id=account.id,
        name=account.name,
        account_type=account.account_type,
        iban_masked=mask_iban(account.iban),
        currency=account.currency,
        balance_cents=account.balance_cents,
        balance_at=cast("datetime | None", account.balance_at),
        last_sync_at=last_sync,
        source=account.source,
        group_id=account.group_id,
        include_in_total=account.include_in_total,
        has_icon=has_icon,
        icon_url=_icon_url(has_icon, account.id),
        version=account.version,
    )


def _detail(account: Account, last_sync: datetime | None) -> AccountDetail:
    """Baut die Detailansicht eines Kontos (mit voller IBAN, F-KTO-04)."""

    base = _summary(account, last_sync)
    return AccountDetail(
        **base.model_dump(),
        iban=account.iban,
        opening_balance_cents=account.opening_balance_cents,
        active=account.active,
        bank_access_id=account.bank_access_id,
    )


def _total_dto(composition: TotalComposition, accounts_by_id: dict[int, Account]) -> AccountTotal:
    """Uebersetzt die Gesamtsaldo-Zusammensetzung in das API-DTO (F-KTO-02)."""

    def entry(account_id: int) -> TotalEntry:
        account = accounts_by_id[account_id]
        return TotalEntry(
            id=account.id,
            name=account.name,
            currency=account.currency,
            amount_cents=account.balance_cents,
        )

    return AccountTotal(
        currency=composition.currency,
        amount_cents=composition.amount_cents,
        included=[entry(i) for i in composition.included_ids],
        excluded=[entry(i) for i in composition.excluded_ids],
        foreign=[
            ForeignTotal(
                currency=bucket.currency,
                amount_cents=bucket.amount_cents,
                account_ids=list(bucket.account_ids),
            )
            for bucket in composition.foreign
        ],
    )


@contextmanager
def _account_errors(session: Session, account_id: int) -> Iterator[None]:
    """Uebersetzt Service-Fehler eines Kontos in HTTP-Fehler (F-CON-02)."""

    try:
        yield
    except AccountNotFound as exc:
        raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.") from exc
    except AccountVersionConflict as exc:
        current = get_account(session, account_id)
        raise ConflictError(current=_detail(current, None)) from exc
    except AccountSyncRunning as exc:
        # Laufender Sync des Bankzugangs: 409 mit Klartext, ohne Loeschung (F-KTO-07).
        current = get_account(session, account_id)
        raise ConflictError(str(exc), current=_detail(current, None)) from exc
    except AccountValidationError as exc:
        raise DomainError(str(exc)) from exc


def _page(items: list[AccountSummary], limit: int, cursor: str | None) -> tuple[list[AccountSummary], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.get("/accounts", response_model=AccountListResponse)
def list_accounts_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    archived: bool = False,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> AccountListResponse:
    """Kontenliste mit Gesamtsaldo; archivierte Konten nur auf Wunsch (F-KTO-01/02/06).

    Standardmaessig werden nur aktive Konten geliefert; ``archived=true`` liefert
    ausschliesslich archivierte Konten (separat abrufbar, F-KTO-06). Der
    Gesamtsaldo zaehlt immer ueber die aktiven Konten (F-KTO-02).
    """

    params = resolve_page_params(request, services.settings)

    active_accounts = list_accounts(session, archived=False)
    source = list_accounts(session, archived=True) if archived else active_accounts

    sync_times = latest_sync_times(session, [account.id for account in source])
    summaries = [_summary(account, sync_times.get(account.id)) for account in source]
    page_items, next_cursor = _page(summaries, params.limit, params.cursor)

    composition = compute_total(active_accounts)
    accounts_by_id = {account.id: account for account in active_accounts}
    return AccountListResponse(
        items=page_items,
        total=_total_dto(composition, accounts_by_id),
        next_cursor=next_cursor,
    )


@router.get("/accounts/{account_id}", response_model=AccountDetail)
def get_account_route(
    account_id: int,
    session: Session = Depends(get_session),
) -> AccountDetail:
    """Kontodetails zum Ansehen (F-KTO-04), auch fuer archivierte Konten."""

    with _account_errors(session, account_id):
        account = get_account(session, account_id)
    sync_times = latest_sync_times(session, [account.id])
    return _detail(account, sync_times.get(account.id))


@router.post("/accounts", response_model=AccountDetail, status_code=status.HTTP_201_CREATED)
def create_account_route(
    payload: AccountCreateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> AccountDetail:
    """Legt ein Offline-Konto fuer den Datei-Import an (F-KTO-05b)."""

    try:
        account = create_offline_account(
            session,
            name=payload.name,
            currency=payload.currency,
            account_type=payload.account_type,
            iban=payload.iban,
            opening_balance_cents=payload.opening_balance_cents,
            opening_balance_date=payload.opening_balance_date,
            group_id=payload.group_id,
            include_in_total=payload.include_in_total,
        )
    except GroupNotFound as exc:
        raise DomainError(f"Die Gruppe {payload.group_id} wurde nicht gefunden.") from exc
    except AccountValidationError as exc:
        raise DomainError(str(exc)) from exc

    event_bus.emit("account.changed", account=account.id)
    return _detail(account, None)


@router.patch("/accounts/{account_id}", response_model=AccountDetail)
def update_account_route(
    account_id: int,
    payload: AccountUpdateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> AccountDetail:
    """Aendert Kontodetails mit Versionspruefung und 409 (F-KTO-04, F-CON-02)."""

    changes = payload.model_dump(exclude_unset=True)
    changes.pop("version", None)
    with _account_errors(session, account_id):
        account = update_account(session, account_id, expected_version=payload.version, changes=changes)
    event_bus.emit("account.changed", account=account.id)
    return _detail(account, None)


@router.post("/accounts/{account_id}/archive", response_model=AccountDetail)
def archive_account_route(
    account_id: int,
    payload: VersionedRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> AccountDetail:
    """Archiviert ein Konto; es fehlt danach in Liste, Gesamtsaldo und Batch-Sync (F-KTO-06)."""

    with _account_errors(session, account_id):
        account = set_archived(session, account_id, archived=True, expected_version=payload.version)
    event_bus.emit("account.changed", account=account.id)
    return _detail(account, None)


@router.post("/accounts/{account_id}/restore", response_model=AccountDetail)
def restore_account_route(
    account_id: int,
    payload: VersionedRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> AccountDetail:
    """Stellt ein archiviertes Konto wieder her (F-KTO-06)."""

    with _account_errors(session, account_id):
        account = set_archived(session, account_id, archived=False, expected_version=payload.version)
    event_bus.emit("account.changed", account=account.id)
    return _detail(account, None)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account_route(
    account_id: int,
    version: int = Query(..., ge=1, description="Bekannte Versionsnummer des Kontos"),
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
    sync_registry: SyncRegistry = Depends(get_sync_registry),
) -> Response:
    """Loescht ein Konto mit vollstaendiger Bereinigung und Versionspruefung (F-KTO-07).

    Entfernt in einer Transaktion Buchungen samt Notizen und Vormerkungen,
    Import-Laeufe mit ihren Fehlern, Auftraege, Dublettenentscheidungen und
    kontogebundene Audit-Eintraege; kontogebundene Importvorlagen werden nur
    entkoppelt, der Bankzugang bleibt erhalten. Laeuft fuer den Bankzugang
    gerade ein Sync, antwortet der Server mit 409 und Klartext (F-SYN-11). Den
    vorherigen verlustfreien JSON-Export (F-EXP-04) prueft das API-E2E (T-106).
    """

    with _account_errors(session, account_id):
        delete_account(
            session,
            account_id,
            expected_version=version,
            sync_running=sync_registry.is_active,
        )
    event_bus.emit("account.deleted", account=account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
