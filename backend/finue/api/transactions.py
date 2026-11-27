"""REST-Router der Einzelbuchung: Detail und Kennzeichen (F-ABS-01/02, F-DET-01).

Setzt die schreibenden und lesenden Routen der Einzelbuchung um:

- ``GET /api/transactions/{transaction_id}`` - alle Felder fuer den
  Detailbereich (F-DET-01).
- ``PUT /api/transactions/{transaction_id}/checked`` - "geprueft" setzen.
- ``PUT /api/transactions/{transaction_id}/reconciled`` - "abgestimmt" setzen.

Die Fachlogik liegt in :mod:`finue.services.transaction_edit` (gemeinsam mit
MCP, D10); dieser Router uebersetzt nur HTTP: Body, Fehlerformat und DTOs. Die
Kennzeichen folgen der Setz-Semantik aus F-CON-01 (setzen statt umschalten,
idempotent); ein bereits gesetzter Wert liefert denselben Stand ohne
Versionssprung. Der optionale ``version``-Wert im Body prueft optimistisch
(F-CON-02) und liefert bei Abweichung 409 mit dem aktuellen Stand. Vorgemerkte
Buchungen lassen sich nicht abstimmen (422 mit Klartext, F-ABS-01).

Die Auth-Naht traegt der Router selbst (``require_user``); er bringt sein
``prefix="/api"`` mit (FastAPI 0.165 setzt das Elternpraefix nicht in
``APIRoute.path``, siehe ``decisions.md`` T-088).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from finue.api.conventions import REST_ORIGIN
from finue.api.deps import get_clock, get_event_bus, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.services.events import EventBus
from finue.services.transaction_edit import (
    ImportRunReference,
    PendingReconciliationError,
    TransactionDetail,
    TransactionEditValidationError,
    TransactionNotFound,
    TransactionVersionConflict,
    set_flag,
    transaction_detail,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["transactions"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class ImportRunDTO(BaseModel):
    """Verweis auf den Import-Lauf einer Buchung (F-DET-01)."""

    id: int
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None


class TransactionDetailResponse(BaseModel):
    """Alle Felder einer Einzelbuchung (F-DET-01)."""

    id: int
    account_id: int
    account_name: str
    status: str
    origin: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None
    amount_cents: int
    currency: str
    booking_date: date
    value_date: date | None
    note: str | None
    checked: bool
    checked_at: datetime | None
    checked_origin: str | None
    reconciled: bool
    reconciled_at: datetime | None
    reconciled_origin: str | None
    external_ref: str | None
    merged: bool
    version: int
    import_run: ImportRunDTO | None


class FlagRequest(BaseModel):
    """Anfrage zum Setzen eines Kennzeichens (F-CON-01/02).

    ``value`` ist der Zielwert (setzen statt umschalten). ``version`` ist
    optional: mitgesendet prueft der Server optimistisch und liefert bei einem
    veralteten Stand 409 mit dem aktuellen Stand (F-CON-02).
    """

    value: bool
    version: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------- Uebersetzung


def _import_run_dto(reference: ImportRunReference | None) -> ImportRunDTO | None:
    """Uebersetzt den Import-Lauf-Verweis in das DTO (F-DET-01)."""

    if reference is None:
        return None
    return ImportRunDTO(
        id=reference.id,
        kind=reference.kind,
        status=reference.status,
        started_at=reference.started_at,
        finished_at=reference.finished_at,
    )


def _detail_dto(detail: TransactionDetail) -> TransactionDetailResponse:
    """Uebersetzt das Service-Detail in das API-DTO (F-DET-01)."""

    return TransactionDetailResponse(
        id=detail.id,
        account_id=detail.account_id,
        account_name=detail.account_name,
        status=detail.status,
        origin=detail.origin,
        counterparty=detail.counterparty,
        counterparty_iban=detail.counterparty_iban,
        purpose=detail.purpose,
        amount_cents=detail.amount_cents,
        currency=detail.currency,
        booking_date=detail.booking_date,
        value_date=detail.value_date,
        note=detail.note,
        checked=detail.checked,
        checked_at=detail.checked_at,
        checked_origin=detail.checked_origin,
        reconciled=detail.reconciled,
        reconciled_at=detail.reconciled_at,
        reconciled_origin=detail.reconciled_origin,
        external_ref=detail.external_ref,
        merged=detail.merged,
        version=detail.version,
        import_run=_import_run_dto(detail.import_run),
    )


def _load_detail(session: Session, transaction_id: int) -> TransactionDetailResponse:
    """Laedt die Detailansicht oder meldet 404 (F-CON-02)."""

    try:
        return _detail_dto(transaction_detail(session, transaction_id))
    except TransactionNotFound as exc:
        raise NotFoundError(f"Buchung {transaction_id} wurde nicht gefunden.") from exc


def _set_flag(
    transaction_id: int,
    flag: str,
    payload: FlagRequest,
    session: Session,
    events: EventBus,
    clock: Callable[[], datetime],
) -> TransactionDetailResponse:
    """Gemeinsamer Ablauf beider Kennzeichen-Routen (F-ABS-01/02, F-CON-01/02)."""

    try:
        set_flag(
            session,
            transaction_id,
            flag=flag,
            value=payload.value,
            origin=REST_ORIGIN,
            expected_version=payload.version,
            events=events,
            now=clock,
        )
    except TransactionNotFound as exc:
        raise NotFoundError(f"Buchung {transaction_id} wurde nicht gefunden.") from exc
    except TransactionVersionConflict as exc:
        session.rollback()
        raise ConflictError(current=_load_detail(session, transaction_id)) from exc
    except (PendingReconciliationError, TransactionEditValidationError) as exc:
        raise DomainError(str(exc)) from exc
    return _load_detail(session, transaction_id)


# ------------------------------------------------------------------- Routen


@router.get("/transactions/{transaction_id}", response_model=TransactionDetailResponse)
def get_transaction_route(
    transaction_id: int,
    session: Session = Depends(get_session),
) -> TransactionDetailResponse:
    """Liefert alle Felder einer Einzelbuchung (F-DET-01)."""

    return _load_detail(session, transaction_id)


@router.put("/transactions/{transaction_id}/checked", response_model=TransactionDetailResponse)
def set_checked_route(
    transaction_id: int,
    payload: FlagRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> TransactionDetailResponse:
    """Setzt das "geprueft"-Kennzeichen (Setz-Semantik, F-ABS-01/02, F-CON-01)."""

    return _set_flag(transaction_id, "checked", payload, session, events, clock)


@router.put("/transactions/{transaction_id}/reconciled", response_model=TransactionDetailResponse)
def set_reconciled_route(
    transaction_id: int,
    payload: FlagRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> TransactionDetailResponse:
    """Setzt das "abgestimmt"-Kennzeichen (vorgemerkt -> 422, F-ABS-01/02, F-CON-01)."""

    return _set_flag(transaction_id, "reconciled", payload, session, events, clock)
