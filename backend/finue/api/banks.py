"""REST-Router fuer Banken-Stammdaten (F-KTO-10, F-CON-02).

Setzt die API-Konventionen aus ``docs/api.md`` um: einheitliches Fehlerformat,
Paginierung ``{items, next_cursor}``, Versionsfeld mit 409 und aktuellem Stand
(:class:`finue.api.errors.ConflictError`) sowie das Ereignis ``bank.changed``
(F-CON-04). Die Fachlogik liegt in :mod:`finue.services.banks`; dieser Router
uebersetzt nur HTTP.

FinTS-Endpunkte sind nur per HTTPS erlaubt (N-SEC-05). Eine Bank mit
Bankzugaengen laesst sich nicht loeschen (409 im Klartext, F-KTO-10). Es werden
keine Endpunkte vorbelegt (F-KTO-10: nur mit belegter Quelle, siehe
``decisions.md``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.conventions import VersionedRequest
from finue.api.deps import get_event_bus, get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.bank import Bank
from finue.services import banks as service
from finue.services.events import EventBus

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["banks"], dependencies=[Depends(require_user)])

_ICON_URL_TEMPLATE = "/api/banks/{bank_id}/icon"


# ------------------------------------------------------------------- DTOs


class BankView(BaseModel):
    """Banken-Stammdaten (F-KTO-10)."""

    id: int
    blz: str
    name: str
    website: str | None
    fints_url: str | None
    has_icon: bool
    icon_url: str | None
    version: int


class BankListResponse(BaseModel):
    """Bankenliste mit Cursor (F-KTO-10, F-MCP-11)."""

    items: list[BankView]
    next_cursor: str | None = None


class BankCreateRequest(BaseModel):
    """Anlage einer Bank (F-KTO-10); der FinTS-Endpunkt ist optional und HTTPS."""

    blz: str = Field(min_length=8, max_length=8)
    name: str = Field(min_length=1, max_length=255)
    website: str | None = None
    fints_url: str | None = None


class BankUpdateRequest(VersionedRequest):
    """Aenderung der Banken-Stammdaten mit Versionspruefung (F-KTO-10, F-CON-02)."""

    blz: str | None = Field(default=None, min_length=8, max_length=8)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    website: str | None = None
    fints_url: str | None = None


# ---------------------------------------------------------------- Uebersetzung


def _icon_url(has_icon: bool, bank_id: int) -> str | None:
    """Liefert den Icon-Verweis der Bank oder ``None`` (F-KTO-10)."""

    return _ICON_URL_TEMPLATE.format(bank_id=bank_id) if has_icon else None


def _bank_dto(bank: Bank) -> BankView:
    """Baut die API-Ansicht aus einer Bank."""

    has_icon = bank.icon is not None
    return BankView(
        id=bank.id,
        blz=bank.blz,
        name=bank.name,
        website=bank.website,
        fints_url=bank.fints_url,
        has_icon=has_icon,
        icon_url=_icon_url(has_icon, bank.id),
        version=bank.version,
    )


@contextmanager
def _bank_errors(session: Session, bank_id: int) -> Iterator[None]:
    """Uebersetzt Service-Fehler einer Bank in HTTP-Fehler (F-CON-02)."""

    try:
        yield
    except service.BankNotFound as exc:
        raise NotFoundError(f"Bank {bank_id} wurde nicht gefunden.") from exc
    except service.BankVersionConflict as exc:
        raise ConflictError(current=_bank_dto(service.get_bank(session, bank_id))) from exc
    except service.BankInUse as exc:
        raise ConflictError(
            "Die Bank hat Bankzugaenge und kann nicht geloescht werden.",
            current=_bank_dto(service.get_bank(session, bank_id)),
            details={"access_count": exc.access_count},
        ) from exc
    except service.BankValidationError as exc:
        raise DomainError(str(exc)) from exc


def _page(items: list[BankView], limit: int, cursor: str | None) -> tuple[list[BankView], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.get("/banks", response_model=BankListResponse)
def list_banks_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> BankListResponse:
    """Listet alle Banken-Stammdaten (F-KTO-10)."""

    params = resolve_page_params(request, services.settings)
    views = [_bank_dto(bank) for bank in service.list_banks(session)]
    page_items, next_cursor = _page(views, params.limit, params.cursor)
    return BankListResponse(items=page_items, next_cursor=next_cursor)


@router.post("/banks", response_model=BankView, status_code=status.HTTP_201_CREATED)
def create_bank_route(
    payload: BankCreateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> BankView:
    """Legt eine Bank an (F-KTO-10); es werden keine Endpunkte vorbelegt."""

    try:
        bank = service.create_bank(
            session,
            blz=payload.blz,
            name=payload.name,
            website=payload.website,
            fints_url=payload.fints_url,
        )
    except service.BankValidationError as exc:
        raise DomainError(str(exc)) from exc

    event_bus.emit("bank.changed", bank=bank.id)
    return _bank_dto(bank)


@router.get("/banks/{bank_id}", response_model=BankView)
def get_bank_route(
    bank_id: int,
    session: Session = Depends(get_session),
) -> BankView:
    """Liefert die Stammdaten einer Bank (F-KTO-10)."""

    with _bank_errors(session, bank_id):
        bank = service.get_bank(session, bank_id)
    return _bank_dto(bank)


@router.patch("/banks/{bank_id}", response_model=BankView)
def update_bank_route(
    bank_id: int,
    payload: BankUpdateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> BankView:
    """Aendert Banken-Stammdaten mit Versionspruefung und 409 (F-KTO-10, F-CON-02)."""

    changes = payload.model_dump(exclude_unset=True)
    changes.pop("version", None)
    with _bank_errors(session, bank_id):
        bank = service.update_bank(session, bank_id, expected_version=payload.version, changes=changes)
    event_bus.emit("bank.changed", bank=bank.id)
    return _bank_dto(bank)


@router.delete("/banks/{bank_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bank_route(
    bank_id: int,
    version: int,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> Response:
    """Loescht eine Bank; nur ohne Bankzugaenge (F-KTO-10, F-CON-02)."""

    with _bank_errors(session, bank_id):
        service.delete_bank(session, bank_id, expected_version=version)
    event_bus.emit("bank.changed", bank=bank_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
