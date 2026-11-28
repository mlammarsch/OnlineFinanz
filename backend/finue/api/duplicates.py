"""REST-Router der Dublettenaufloesung: Liste, Zusammenfuehren, Rueckgaengig, Ignorieren.

Setzt F-DUP-05 um (Aufloesung nur durch den Nutzer):

- ``GET /api/duplicates?account_id|group_id`` - offene Verdachtspaare aus
  F-DUP-03, ohne ignorierte oder aufgeloeste Paare, mit Vorschlag der bleibenden
  Buchung (FinTS vor Datei, sonst die aeltere).
- ``POST /api/duplicates/merge`` - Paar zusammenfuehren (F-DUP-05, F-CON-02).
- ``POST /api/duplicates/{decision_id}/undo`` - Zusammenfuehren rueckgaengig
  machen (409 mit aktuellem Stand, wenn die bleibende Buchung seither geaendert
  wurde).
- ``POST /api/duplicates/ignore`` - Paar als "keine Dublette" speichern.

Die Fachlogik liegt in :mod:`finue.services.duplicate_resolution` (gemeinsam mit
MCP, D10); dieser Router uebersetzt nur HTTP: Body, Fehlerformat und DTOs. Der
Router bringt ``prefix="/api"`` und die Auth-Naht ``require_user`` selbst mit
(FastAPI 0.165 setzt das Elternpraefix nicht in ``APIRoute.path``, siehe
``decisions.md`` T-088).

Referenzierte Anforderungen: F-DUP-03, F-DUP-05, F-CON-01/02, N-SEC-02.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.conventions import REST_ORIGIN
from finue.api.deps import get_clock, get_event_bus, get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError, ValidationApiError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.accounts import list_accounts
from finue.services.duplicate_resolution import (
    DuplicateCandidate,
    DuplicateNotFound,
    DuplicateResolutionError,
    DuplicateVersionConflict,
    IgnoreResult,
    MergeResult,
    SuspectedDuplicate,
    UndoResult,
    ignore_duplicates,
    list_suspected_duplicates,
    merge_duplicates,
    undo_merge,
)
from finue.services.events import EventBus

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["duplicates"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class DuplicateCandidateDTO(BaseModel):
    """Eine Buchung als Seite eines Verdachtspaares (F-DUP-03)."""

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
    origin: str
    note: str | None
    checked: bool
    reconciled: bool
    external_ref: str | None
    merged: bool
    version: int


class SuspectedDuplicateDTO(BaseModel):
    """Ein offenes Verdachtspaar mit Vorschlag der bleibenden Buchung (F-DUP-03)."""

    account_id: int
    suggested_keep_id: int
    reason: str
    keep: DuplicateCandidateDTO
    other: DuplicateCandidateDTO


class DuplicateListResponse(BaseModel):
    """Liste offener Verdachtspaare mit Paginierung (SPEC F-MCP-11)."""

    items: list[SuspectedDuplicateDTO]
    next_cursor: str | None = None


class MergeRequest(BaseModel):
    """Anfrage zum Zusammenfuehren einer Verdachtsdublette (F-DUP-05, F-CON-02).

    ``versions`` traegt fuer **beide** Buchungen die bekannte Version; passt eine
    nicht, liefert der Server 409 mit dem aktuellen Stand. ``external_ref_choice``
    ist Pflicht, wenn beide Buchungen eine verschiedene externe Referenz tragen.
    """

    keep_id: int
    other_id: int
    versions: dict[int, int]
    external_ref_choice: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> MergeRequest:
        if self.keep_id == self.other_id:
            raise ValueError("Eine Buchung laesst sich nicht mit sich selbst zusammenfuehren.")
        for transaction_id in (self.keep_id, self.other_id):
            version = self.versions.get(transaction_id)
            if version is None or version < 1:
                raise ValueError("Fuer beide Buchungen wird die aktuelle Version erwartet.")
        return self


class MergeResponse(BaseModel):
    """Ergebnis eines Zusammenfuehrens (F-DUP-05)."""

    decision_id: int
    keep: DuplicateCandidateDTO
    other: DuplicateCandidateDTO
    external_ref: str | None


class IgnoreRequest(BaseModel):
    """Anfrage zum Ignorieren einer Verdachtsdublette (F-DUP-05).

    ``versions`` ist optional; sind Werte enthalten, prueft der Server
    optimistisch (F-CON-02).
    """

    keep_id: int
    other_id: int
    versions: dict[int, int] | None = Field(default=None)

    @model_validator(mode="after")
    def _validate(self) -> IgnoreRequest:
        if self.keep_id == self.other_id:
            raise ValueError("Ein Paar aus einer Buchung ist nicht sinnvoll.")
        if self.versions is not None and any(version < 1 for version in self.versions.values()):
            raise ValueError("Die Versionsnummer muss mindestens 1 sein.")
        return self


class IgnoreResponse(BaseModel):
    """Ergebnis eines Ignorierens (F-DUP-05)."""

    decision_id: int
    keep_id: int
    other_id: int
    version: int


class UndoResponse(BaseModel):
    """Ergebnis eines Rueckgaengigmachens (F-DUP-05)."""

    decision_id: int
    keep: DuplicateCandidateDTO
    other: DuplicateCandidateDTO


# ---------------------------------------------------------------- Uebersetzung


def _candidate_dto(candidate: DuplicateCandidate) -> DuplicateCandidateDTO:
    """Uebersetzt eine Buchungsansicht in das API-DTO (F-DUP-03)."""

    return DuplicateCandidateDTO(
        id=candidate.id,
        account_id=candidate.account_id,
        status=candidate.status,
        booking_date=candidate.booking_date,
        value_date=candidate.value_date,
        amount_cents=candidate.amount_cents,
        currency=candidate.currency,
        counterparty=candidate.counterparty,
        counterparty_iban=candidate.counterparty_iban,
        purpose=candidate.purpose,
        origin=candidate.origin,
        note=candidate.note,
        checked=candidate.checked,
        reconciled=candidate.reconciled,
        external_ref=candidate.external_ref,
        merged=candidate.merged,
        version=candidate.version,
    )


def _pair_dto(pair: SuspectedDuplicate) -> SuspectedDuplicateDTO:
    """Uebersetzt ein Verdachtspaar in das API-DTO (F-DUP-03)."""

    return SuspectedDuplicateDTO(
        account_id=pair.account_id,
        suggested_keep_id=pair.keep.id,
        reason=pair.reason,
        keep=_candidate_dto(pair.keep),
        other=_candidate_dto(pair.other),
    )


def _merge_dto(result: MergeResult) -> MergeResponse:
    """Uebersetzt das Service-Ergebnis in das API-DTO (F-DUP-05)."""

    return MergeResponse(
        decision_id=result.decision_id,
        keep=_candidate_dto(result.keep),
        other=_candidate_dto(result.other),
        external_ref=result.external_ref,
    )


def _ignore_dto(result: IgnoreResult) -> IgnoreResponse:
    """Uebersetzt das Ignorier-Ergebnis in das API-DTO (F-DUP-05)."""

    return IgnoreResponse(
        decision_id=result.decision_id,
        keep_id=result.keep_id,
        other_id=result.other_id,
        version=result.version,
    )


def _undo_dto(result: UndoResult) -> UndoResponse:
    """Uebersetzt das Rueckgaengig-Ergebnis in das API-DTO (F-DUP-05)."""

    return UndoResponse(
        decision_id=result.decision_id,
        keep=_candidate_dto(result.keep),
        other=_candidate_dto(result.other),
    )


def _current_state(session: Session, transaction_id: int) -> dict[str, Any]:
    """Liefert den aktuellen Stand einer Buchung fuer ein 409 (F-CON-02).

    Wird nach einem Rollback aufgerufen; unbekannte Buchungen ergeben einen
    leeren Stand, weil der Fehlerhandler nur das Feld ``current`` traegt.
    """

    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        return {"id": transaction_id}
    return {
        "id": transaction.id,
        "account_id": transaction.account_id,
        "status": transaction.status,
        "note": transaction.note,
        "checked": transaction.checked,
        "reconciled": transaction.reconciled,
        "external_ref": transaction.external_ref,
        "merged": transaction.merged,
        "version": transaction.version,
    }


def _resolve_scope(session: Session, account_id: int | None, group_id: int | None) -> list[int]:
    """Ermittelt die Konten des abgefragten Bereichs (F-DUP-03).

    Es muss genau ein ``account_id`` oder ``group_id`` angegeben sein.

    Raises:
        ValidationApiError: Weder noch beides ist angegeben.
        NotFoundError: Konto oder Gruppe existieren nicht.
    """

    if (account_id is None) == (group_id is None):
        raise ValidationApiError(
            "Bitte genau ein Konto oder eine Gruppe angeben (account_id oder group_id).",
            details={"field": "account_id"},
        )
    if account_id is not None:
        if session.get(Account, account_id) is None:
            raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.")
        return [account_id]
    if session.get(AccountGroup, group_id) is None:
        raise NotFoundError(f"Gruppe {group_id} wurde nicht gefunden.")
    return [account.id for account in list_accounts(session, archived=False) if account.group_id == group_id]


# ------------------------------------------------------------------- Routen


@router.get("/duplicates", response_model=DuplicateListResponse)
def list_duplicates_route(
    request: Request,
    account_id: int | None = None,
    group_id: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> DuplicateListResponse:
    """Listet offene Verdachtspaare eines Kontos oder einer Gruppe (F-DUP-03/05)."""

    params = resolve_page_params(request, services.settings)
    account_ids = _resolve_scope(session, account_id, group_id)
    pairs = list_suspected_duplicates(session, account_ids=account_ids)
    offset = decode_cursor(params.cursor) if params.cursor else 0
    window = pairs[offset : offset + params.limit]
    next_offset = offset + len(window)
    next_cursor = encode_cursor(next_offset) if next_offset < len(pairs) else None
    return DuplicateListResponse(items=[_pair_dto(pair) for pair in window], next_cursor=next_cursor)


@router.post("/duplicates/merge", response_model=MergeResponse)
def merge_duplicates_route(
    payload: MergeRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> MergeResponse:
    """Fuehrt ein Verdachtspaar zusammen (F-DUP-05, F-CON-01/02)."""

    try:
        result = merge_duplicates(
            session,
            keep_id=payload.keep_id,
            other_id=payload.other_id,
            versions=payload.versions,
            external_ref_choice=payload.external_ref_choice,
            origin=REST_ORIGIN,
            events=events,
            now=clock,
        )
    except DuplicateNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except DuplicateVersionConflict as exc:
        session.rollback()
        raise ConflictError(current=_current_state(session, exc.transaction_id)) from exc
    except DuplicateResolutionError as exc:
        raise DomainError(str(exc)) from exc
    return _merge_dto(result)


@router.post("/duplicates/{decision_id}/undo", response_model=UndoResponse)
def undo_merge_route(
    decision_id: int,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> UndoResponse:
    """Macht ein Zusammenfuehren rueckgaengig (F-DUP-05, F-CON-02)."""

    try:
        result = undo_merge(session, decision_id, origin=REST_ORIGIN, events=events, now=clock)
    except DuplicateNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except DuplicateVersionConflict as exc:
        session.rollback()
        raise ConflictError(current=_current_state(session, exc.transaction_id)) from exc
    return _undo_dto(result)


@router.post("/duplicates/ignore", response_model=IgnoreResponse)
def ignore_duplicates_route(
    payload: IgnoreRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> IgnoreResponse:
    """Speichert ein Paar als "keine Dublette" (F-DUP-05)."""

    try:
        result = ignore_duplicates(
            session,
            keep_id=payload.keep_id,
            other_id=payload.other_id,
            versions=payload.versions,
            origin=REST_ORIGIN,
            events=events,
            now=clock,
        )
    except DuplicateNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except DuplicateVersionConflict as exc:
        session.rollback()
        raise ConflictError(current=_current_state(session, exc.transaction_id)) from exc
    except DuplicateResolutionError as exc:
        raise DomainError(str(exc)) from exc
    return _ignore_dto(result)
