"""REST-Router fuer Icon-Upload und -Auslieferung (F-KTO-11, F-CON-02).

Es gibt keinen ausgehenden Abruf (F-KTO-08 gehoert zu M7, N-SEC-05): Icons werden
ausschliesslich hier hochgeladen oder geloescht. Die Verarbeitung (Groesse,
Typ am Inhalt, Pixelgrenze, Skalierung auf 128 x 128 PNG) liegt in
:mod:`finue.services.icons`; dieser Router uebersetzt nur HTTP.

- ``PUT /api/accounts/{id}/icon`` bzw. ``/api/banks/{id}/icon``: Upload als
  PNG/JPEG/WebP (hoechstens 512 KB) mit ``?version=`` (F-CON-02),
- ``DELETE`` derselben Pfade mit ``?version=``,
- ``GET`` liefert das gespeicherte PNG mit ``ETag`` (und ``304`` bei
  passendem ``If-None-Match``).

Die Groessengrenze aus F-KTO-11 (512 KB) wird vor dem Dekodieren geprueft und
als 413 ``payload_too_large`` gemeldet; ein unzulaessiger Inhalt als 422
``domain_error`` im Klartext.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finue.api.deps import get_event_bus, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError, PayloadTooLargeError
from finue.models.account import Account
from finue.models.bank import Bank
from finue.services import icons as icon_service
from finue.services.accounts import AccountNotFound, AccountVersionConflict, get_account
from finue.services.banks import BankNotFound, BankVersionConflict, get_bank
from finue.services.events import EventBus
from finue.services.icons import IconError, IconTooLarge

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["icons"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTO


class IconResponse(BaseModel):
    """Antwort nach einem Icon-Upload (F-KTO-11)."""

    id: int
    has_icon: bool
    icon_url: str | None
    version: int
    width: int | None
    height: int | None


def _account_icon_url(account_id: int) -> str:
    """Icon-Verweis eines Kontos."""

    return f"/api/accounts/{account_id}/icon"


def _bank_icon_url(bank_id: int) -> str:
    """Icon-Verweis einer Bank."""

    return f"/api/banks/{bank_id}/icon"


def _icon_http_error(exc: IconError) -> Exception:
    """Uebersetzt einen Verarbeitungsfehler in den passenden HTTP-Fehler."""

    if isinstance(exc, IconTooLarge):
        return PayloadTooLargeError(str(exc))
    return DomainError(str(exc))


def _account_state(session: Session, account_id: int) -> dict[str, object]:
    """Aktueller Kontostand fuer den 409-Konflikt (F-CON-02)."""

    account = session.get(Account, account_id)
    if account is None:
        return {"id": account_id, "version": -1, "has_icon": False}
    return {"id": account.id, "version": account.version, "has_icon": account.icon is not None}


def _bank_state(session: Session, bank_id: int) -> dict[str, object]:
    """Aktueller Bankstand fuer den 409-Konflikt (F-CON-02)."""

    bank = session.get(Bank, bank_id)
    if bank is None:
        return {"id": bank_id, "version": -1, "has_icon": False}
    return {"id": bank.id, "version": bank.version, "has_icon": bank.icon is not None}


def _icon_response(change: icon_service.IconChange, url: str) -> IconResponse:
    """Baut die Upload-Antwort aus dem Ergebnis der Speicherung."""

    return IconResponse(
        id=change.id,
        has_icon=change.has_icon,
        icon_url=url if change.has_icon else None,
        version=change.version,
        width=change.width,
        height=change.height,
    )


def _png_response(request: Request, data: bytes) -> Response:
    """Liefert PNG-Bytes mit ETag; bei passendem ``If-None-Match`` als 304 (F-KTO-11)."""

    etag = icon_service.icon_etag(data)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=data, media_type=icon_service.ICON_MIME, headers=headers)


# ------------------------------------------------------------------- Konten


@router.get("/accounts/{account_id}/icon")
def get_account_icon(
    account_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Liefert das gespeicherte Konto-Icon als PNG mit ETag (F-KTO-11)."""

    try:
        account = get_account(session, account_id)
    except AccountNotFound as exc:
        raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.") from exc
    if account.icon is None:
        raise NotFoundError("Fuer dieses Konto ist kein Icon gespeichert.")
    return _png_response(request, account.icon)


@router.put("/accounts/{account_id}/icon", response_model=IconResponse)
async def upload_account_icon(
    account_id: int,
    version: int,
    request: Request,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> IconResponse:
    """Setzt das Konto-Icon aus dem Anfragekoerper (F-KTO-11, F-CON-02)."""

    data = await request.body()
    try:
        change = icon_service.set_account_icon(session, account_id, expected_version=version, data=data)
    except AccountNotFound as exc:
        raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.") from exc
    except AccountVersionConflict as exc:
        raise ConflictError(current=_account_state(session, account_id)) from exc
    except IconError as exc:
        raise _icon_http_error(exc) from exc

    event_bus.emit("account.changed", account=account_id)
    return _icon_response(change, _account_icon_url(account_id))


@router.delete("/accounts/{account_id}/icon", status_code=status.HTTP_204_NO_CONTENT)
def clear_account_icon(
    account_id: int,
    version: int,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> Response:
    """Entfernt das Konto-Icon (F-KTO-11, F-CON-02)."""

    try:
        icon_service.clear_account_icon(session, account_id, expected_version=version)
    except AccountNotFound as exc:
        raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.") from exc
    except AccountVersionConflict as exc:
        raise ConflictError(current=_account_state(session, account_id)) from exc

    event_bus.emit("account.changed", account=account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------- Banken


@router.get("/banks/{bank_id}/icon")
def get_bank_icon(
    bank_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Liefert das gespeicherte Bank-Icon als PNG mit ETag (F-KTO-11)."""

    try:
        bank = get_bank(session, bank_id)
    except BankNotFound as exc:
        raise NotFoundError(f"Bank {bank_id} wurde nicht gefunden.") from exc
    if bank.icon is None:
        raise NotFoundError("Fuer diese Bank ist kein Icon gespeichert.")
    return _png_response(request, bank.icon)


@router.put("/banks/{bank_id}/icon", response_model=IconResponse)
async def upload_bank_icon(
    bank_id: int,
    version: int,
    request: Request,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> IconResponse:
    """Setzt das Bank-Icon aus dem Anfragekoerper (F-KTO-11, F-CON-02)."""

    data = await request.body()
    try:
        change = icon_service.set_bank_icon(session, bank_id, expected_version=version, data=data)
    except BankNotFound as exc:
        raise NotFoundError(f"Bank {bank_id} wurde nicht gefunden.") from exc
    except BankVersionConflict as exc:
        raise ConflictError(current=_bank_state(session, bank_id)) from exc
    except IconError as exc:
        raise _icon_http_error(exc) from exc

    event_bus.emit("bank.changed", bank=bank_id)
    return _icon_response(change, _bank_icon_url(bank_id))


@router.delete("/banks/{bank_id}/icon", status_code=status.HTTP_204_NO_CONTENT)
def clear_bank_icon(
    bank_id: int,
    version: int,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> Response:
    """Entfernt das Bank-Icon (F-KTO-11, F-CON-02)."""

    try:
        icon_service.clear_bank_icon(session, bank_id, expected_version=version)
    except BankNotFound as exc:
        raise NotFoundError(f"Bank {bank_id} wurde nicht gefunden.") from exc
    except BankVersionConflict as exc:
        raise ConflictError(current=_bank_state(session, bank_id)) from exc

    event_bus.emit("bank.changed", bank=bank_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
