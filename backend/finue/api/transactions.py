"""REST-Router der Einzelbuchung: Detail, Kennzeichen, Notiz und Referenz.

Setzt die schreibenden und lesenden Routen der Einzelbuchung um:

- ``GET /api/transactions/{transaction_id}`` - alle Felder fuer den
  Detailbereich (F-DET-01).
- ``PUT /api/transactions/{transaction_id}/checked`` - "geprueft" setzen.
- ``PUT /api/transactions/{transaction_id}/reconciled`` - "abgestimmt" setzen.
- ``PUT /api/transactions/{transaction_id}/note`` - Notiz ersetzen (F-CON-02).
- ``POST /api/transactions/{transaction_id}/note/append`` - Notiz atomar
  anhaengen (F-CON-01).
- ``PUT /api/transactions/{transaction_id}/external-ref`` - externe Referenz
  setzen (F-ABS-05).
- ``POST /api/transactions/batch`` - Batch-Aktion auf eine Auswahl von Buchungen
  mit Ergebnis je Eintrag (F-CON-03, F-ABS-02, F-SEL-03).

Die Fachlogik liegt in :mod:`finue.services.transaction_edit` (gemeinsam mit
MCP, D10); dieser Router uebersetzt nur HTTP: Body, Fehlerformat und DTOs. Die
Kennzeichen folgen der Setz-Semantik aus F-CON-01 (setzen statt umschalten,
idempotent); ein bereits gesetzter Wert liefert denselben Stand ohne
Versionssprung. Der ``version``-Wert im Body prueft optimistisch (F-CON-02) und
liefert bei Abweichung 409 mit dem aktuellen Stand; er ist bei den
**ersetzenden** Routen (Notiz ersetzen, externe Referenz) Pflicht. Vorgemerkte
Buchungen lassen sich nicht abstimmen (422 mit Klartext, F-ABS-01).

Der **Batch** ruft je Eintrag genau den Kern aus T-108/T-109 auf (keine zweite
Schreibpfad-Logik); ein Konflikt oder Fehler eines Eintrags bricht den Rest
**nicht** ab (F-CON-03). Die Ergebnisfolge bleibt in der Reihenfolge der
angefragten IDs.

Die Auth-Naht traegt der Router selbst (``require_user``); er bringt sein
``prefix="/api"`` mit (FastAPI 0.165 setzt das Elternpraefix nicht in
``APIRoute.path``, siehe ``decisions.md`` T-088).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.conventions import (
    REST_ORIGIN,
    BatchItemResult,
    BatchOutcome,
    BatchResponse,
    check_batch_size,
)
from finue.api.deps import get_clock, get_event_bus, get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.services.events import EventBus
from finue.services.transaction_edit import (
    ImportRunReference,
    PendingReconciliationError,
    TransactionDetail,
    TransactionEditValidationError,
    TransactionNotFound,
    TransactionVersionConflict,
    append_note,
    set_external_ref,
    set_flag,
    set_note,
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


class NoteReplaceRequest(BaseModel):
    """Anfrage zum Ersetzen der Notiz (F-CON-02, F-ABS-05).

    ``text`` ersetzt die Notiz vollstaendig; ``null`` oder ``""`` loeschen sie.
    ``version`` ist Pflicht (ersetzende Aenderung, optimistisches Sperren).
    """

    text: str | None
    version: int = Field(ge=1)


class NoteAppendRequest(BaseModel):
    """Anfrage zum atomaren Anhaengen an die Notiz (F-CON-01, F-ABS-05).

    ``text`` darf nicht leer (auch nicht nur Leerraum) sein. Es gibt bewusst
    keine ``version``: Anhaengen ist eine atomare Operation (F-CON-01), kein
    Ersetzen.
    """

    text: str = Field(min_length=1, pattern=r"\S")


class ExternalRefRequest(BaseModel):
    """Anfrage zum Setzen der externen Referenz (F-ABS-05, F-CON-02).

    ``value`` ersetzt die Referenz; ``null`` oder ``""`` loeschen sie.
    ``version`` ist Pflicht (ersetzende Aenderung, optimistisches Sperren).
    """

    value: str | None
    version: int = Field(ge=1)


class BatchEditAction(StrEnum):
    """Erlaubte Batch-Aktionen auf eine Buchungsauswahl (F-ABS-02, F-SEL-03).

    Deckt genau die Kennzeichen- und Notiz-Aktionen der Auswahl ab; die
    externe Referenz ist bewusst nicht Teil des Batches (F-SEL-03 nennt nur
    Notiz, geprueft und abgestimmt).
    """

    CHECKED = "checked"
    RECONCILED = "reconciled"
    NOTE_SET = "note_set"
    NOTE_APPEND = "note_append"


class TransactionBatchRequest(BaseModel):
    """Anfrage einer Batch-Aktion auf eine Buchungsauswahl (F-CON-03, F-ABS-02).

    ``ids`` ist die Auswahl (Reihenfolge bestimmt die Ergebnisfolge, keine
    Duplikate), ``action`` die Aktion, ``value`` der Zielwert (Kennzeichen:
    wahr/falsch; Notiz ersetzen: Text oder ``null``; Notiz anhaengen:
    nichtleerer Text). ``versions`` ist optional: eine Abbildung
    Buchungs-ID -> bekannte Version; wo ein Eintrag eine Version traegt, prueft
    der Server optimistisch und meldet einen veralteten Stand je Eintrag als
    Konflikt (F-CON-02/03), ohne den Rest abzubrechen.
    """

    ids: list[int] = Field(min_length=1, description="Auswahl an Buchungs-IDs")
    action: BatchEditAction
    value: bool | str | None = None
    versions: dict[int, int] | None = None

    @model_validator(mode="after")
    def _validate(self) -> TransactionBatchRequest:
        """Prueft Wert je Aktion, die Versionsnummern und die Eindeutigkeit der IDs."""

        if len(set(self.ids)) != len(self.ids):
            raise ValueError("Die Liste enthaelt doppelte Buchungs-IDs.")
        if self.action in (BatchEditAction.CHECKED, BatchEditAction.RECONCILED):
            if not isinstance(self.value, bool):
                raise ValueError("Der Kennzeichen-Wert muss wahr oder falsch sein.")
        elif self.action is BatchEditAction.NOTE_SET:
            if self.value is not None and not isinstance(self.value, str):
                raise ValueError("Die Notiz muss eine Zeichenkette oder leer sein.")
        elif not isinstance(self.value, str) or self.value.strip() == "":
            raise ValueError("Der anzuhaengende Notiztext darf nicht leer sein.")
        if self.versions is not None and any(version < 1 for version in self.versions.values()):
            raise ValueError("Die Versionsnummer muss mindestens 1 sein.")
        return self


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


def _run_edit(
    operation: Callable[[], object],
    session: Session,
    transaction_id: int,
) -> TransactionDetailResponse:
    """Fuehrt eine Edit-Operation aus und uebersetzt Fachfehler in HTTP (F-CON-02).

    Gemeinsamer Ablauf aller schreibenden Routen der Einzelbuchung: 404 bei
    unbekannter Buchung, 409 mit aktuellem Stand bei Versionskonflikt, 422 mit
    Klartext bei Fachfehlern. Bei Erfolg wird der neue Stand zurueckgegeben.
    """

    try:
        operation()
    except TransactionNotFound as exc:
        raise NotFoundError(f"Buchung {transaction_id} wurde nicht gefunden.") from exc
    except TransactionVersionConflict as exc:
        session.rollback()
        raise ConflictError(current=_load_detail(session, transaction_id)) from exc
    except (PendingReconciliationError, TransactionEditValidationError) as exc:
        raise DomainError(str(exc)) from exc
    return _load_detail(session, transaction_id)


def _set_flag(
    transaction_id: int,
    flag: str,
    payload: FlagRequest,
    session: Session,
    events: EventBus,
    clock: Callable[[], datetime],
) -> TransactionDetailResponse:
    """Gemeinsamer Ablauf beider Kennzeichen-Routen (F-ABS-01/02, F-CON-01/02)."""

    return _run_edit(
        lambda: set_flag(
            session,
            transaction_id,
            flag=flag,
            value=payload.value,
            origin=REST_ORIGIN,
            expected_version=payload.version,
            events=events,
            now=clock,
        ),
        session,
        transaction_id,
    )


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


@router.put("/transactions/{transaction_id}/note", response_model=TransactionDetailResponse)
def set_note_route(
    transaction_id: int,
    payload: NoteReplaceRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
) -> TransactionDetailResponse:
    """Ersetzt die Notiz mit Versionspruefung (F-CON-02, F-ABS-05)."""

    return _run_edit(
        lambda: set_note(
            session,
            transaction_id,
            text=payload.text,
            origin=REST_ORIGIN,
            expected_version=payload.version,
            events=events,
        ),
        session,
        transaction_id,
    )


@router.post("/transactions/{transaction_id}/note/append", response_model=TransactionDetailResponse)
def append_note_route(
    transaction_id: int,
    payload: NoteAppendRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
) -> TransactionDetailResponse:
    """Haengt einen Text atomar an die Notiz an (F-CON-01, F-ABS-05)."""

    return _run_edit(
        lambda: append_note(
            session,
            transaction_id,
            text=payload.text,
            origin=REST_ORIGIN,
            events=events,
        ),
        session,
        transaction_id,
    )


@router.put("/transactions/{transaction_id}/external-ref", response_model=TransactionDetailResponse)
def set_external_ref_route(
    transaction_id: int,
    payload: ExternalRefRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
) -> TransactionDetailResponse:
    """Setzt die externe Referenz mit Versionspruefung (F-ABS-05, F-CON-02)."""

    return _run_edit(
        lambda: set_external_ref(
            session,
            transaction_id,
            value=payload.value,
            origin=REST_ORIGIN,
            expected_version=payload.version,
            events=events,
        ),
        session,
        transaction_id,
    )


# ------------------------------------------------------------------- Batch

#: Deutscher Klartext fuer einen Konflikt-Eintrag im Batch (F-CON-02/03).
_BATCH_CONFLICT_MESSAGE = "Die Buchung wurde zwischenzeitlich geaendert."


def _safe_current(session: Session, transaction_id: int) -> dict[str, Any] | None:
    """Liefert den aktuellen Stand als DTO-Dict oder ``None`` (F-CON-02)."""

    try:
        return _load_detail(session, transaction_id).model_dump(mode="json")
    except (TransactionNotFound, NotFoundError):
        return None


def _apply_batch_action(
    payload: TransactionBatchRequest,
    transaction_id: int,
    session: Session,
    events: EventBus,
    clock: Callable[[], datetime],
    expected_version: int | None,
) -> bool:
    """Fuehrt die Batch-Aktion fuer eine Buchung auf dem T-108/T-109-Kern aus.

    Es gibt **keine** zweite Schreibpfad-Logik: jede Aktion ruft genau die
    Service-Funktion auf, die auch die Einzelrouten nutzen. Rueckgabe ist
    ``changed`` (F-CON-01); Konflikte und Fachfehler wirft die Kernfunktion.
    """

    action = payload.action
    if action is BatchEditAction.CHECKED:
        result = set_flag(
            session,
            transaction_id,
            flag="checked",
            value=bool(payload.value),
            origin=REST_ORIGIN,
            expected_version=expected_version,
            events=events,
            now=clock,
        )
        return result.changed
    if action is BatchEditAction.RECONCILED:
        result = set_flag(
            session,
            transaction_id,
            flag="reconciled",
            value=bool(payload.value),
            origin=REST_ORIGIN,
            expected_version=expected_version,
            events=events,
            now=clock,
        )
        return result.changed
    if action is BatchEditAction.NOTE_SET:
        note = payload.value if isinstance(payload.value, str) else None
        return set_note(
            session,
            transaction_id,
            text=note,
            origin=REST_ORIGIN,
            expected_version=expected_version,
            events=events,
        ).changed
    return append_note(session, transaction_id, text=str(payload.value), origin=REST_ORIGIN, events=events).changed


def _batch_entry(
    payload: TransactionBatchRequest,
    transaction_id: int,
    session: Session,
    events: EventBus,
    clock: Callable[[], datetime],
) -> BatchItemResult:
    """Verarbeitet einen Batch-Eintrag und uebersetzt Fehler in ein Ergebnis (F-CON-03).

    Konflikte und Fehler eines Eintrags werden zu einem ``konflikt``- bzw.
    ``fehler``-Ergebnis; der Rest des Batches laeuft weiter.
    """

    expected_version = payload.versions.get(transaction_id) if payload.versions else None
    try:
        changed = _apply_batch_action(payload, transaction_id, session, events, clock, expected_version)
    except TransactionNotFound as exc:
        return BatchItemResult(
            id=transaction_id,
            outcome=BatchOutcome.FEHLER,
            message=str(exc),
        )
    except TransactionVersionConflict:
        session.rollback()
        return BatchItemResult(
            id=transaction_id,
            outcome=BatchOutcome.KONFLIKT,
            current=_safe_current(session, transaction_id),
            message=_BATCH_CONFLICT_MESSAGE,
        )
    except (PendingReconciliationError, TransactionEditValidationError) as exc:
        session.rollback()
        return BatchItemResult(id=transaction_id, outcome=BatchOutcome.FEHLER, message=str(exc))
    return BatchItemResult(
        id=transaction_id,
        outcome=BatchOutcome.GEAENDERT if changed else BatchOutcome.UNVERAENDERT,
    )


@router.post("/transactions/batch", response_model=BatchResponse)
def batch_route(
    payload: TransactionBatchRequest,
    session: Session = Depends(get_session),
    events: EventBus = Depends(get_event_bus),
    clock: Callable[[], datetime] = Depends(get_clock),
    services: AppServices = Depends(get_services),
) -> BatchResponse:
    """Batch-Aktion auf eine Auswahl mit Ergebnis je Buchung (F-CON-03, F-ABS-02).

    Nutzt je Eintrag genau den Kern aus T-108/T-109 (keine zweite
    Schreibpfad-Logik); Konflikte oder Fehler eines Eintrags brechen den Rest
    nicht ab. Die Batchgrenze prueft die zentrale Einstellung
    ``api_max_batch_size`` (F-CON-03).
    """

    check_batch_size(len(payload.ids), services.settings)
    results = [_batch_entry(payload, transaction_id, session, events, clock) for transaction_id in payload.ids]
    return BatchResponse(results=results)
