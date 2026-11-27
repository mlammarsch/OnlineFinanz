"""REST-Router fuer Sync-Anstoesse, TAN-Endpunkte und das Sync-Protokoll (T-112).

Der Router uebersetzt nur HTTP; die Fachlogik liegt im
:class:`~finue.services.sync_service.SyncService` und im
:class:`~finue.services.sync_runner.SyncRunner` (D3, D10).

- ``POST /api/sync``: Batch-Anstoss. ``bank_access_id`` ist optional; ohne
  Angabe werden **alle** Bankzugaenge mit aktiven FinTS-Konten gestartet (je
  Zugang ein Job, F-SYN-01/F-SYN-11). ``account_ids`` und ``pin`` sind nur bei
  genau einem Zugang zulaessig.
- ``POST /api/accounts/{id}/sync``: Einzel-Sync eines Kontos (F-SYN-02).
- ``GET /api/sync/jobs/{job_id}``: Status, Kontoergebnisse sowie Challenge-Text
  und -Art einer offenen TAN-Abfrage (F-SYN-04); nie Dialogdaten.
- ``POST /api/sync/jobs/{job_id}/tan``: TAN eingeben (``{tan}``). Die zweite
  Antwort liefert 409 (F-SYN-04).
- ``POST /api/sync/jobs/{job_id}/approve-poll``: App-Freigabe erneut abfragen.
- ``POST /api/sync/jobs/{job_id}/cancel``: Lauf abbrechen (F-SYN-10).
- ``GET /api/sync/runs``: Sync-Protokoll, paginiert und je Konto filterbar
  (F-SYN-07).

Ein Anstoss kehrt sofort mit der Job-ID zurueck; der Lauf laeuft in einem
Hintergrund-Thread des einen Worker-Prozesses (D3). Die PIN kommt bei
``store_pin = false`` im Anfragekoerper, bleibt nur im Arbeitsspeicher und
erscheint weder in Antwort, Ereignis noch Log (N-SEC-01..03). Der Router bringt
``prefix="/api"`` und die Auth-Naht ``require_user`` selbst mit (T-088).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, PositiveInt
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from finue.api.container import AppServices
from finue.api.deps import get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.api.imports import ImportRunView, run_view
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.account import Account
from finue.models.bank import BankAccess
from finue.models.import_run import ImportRun
from finue.services.bank_access import get_access
from finue.services.sync_runner import SyncJobNotFound, SyncJobState
from finue.services.sync_service import AccountSyncResult, SyncTanConflictError

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["sync"], dependencies=[Depends(require_user)])


# --------------------------------------------------------------------- DTOs


class SyncAccountResultView(BaseModel):
    """Ergebnis eines Kontos innerhalb eines Sync-Laufs (F-SYN-07)."""

    account_id: int
    status: str
    import_run_id: int
    attempts: int
    message: str | None = None


class SyncJobView(BaseModel):
    """Zustand eines Sync-Jobs (F-SYN-04/07).

    ``challenge`` und ``tan_kind`` beschreiben eine offene TAN-Abfrage;
    Dialogdaten, TAN und PIN sind nie enthalten (N-SEC-02/03).
    """

    job_id: str
    bank_access_id: int
    status: str
    account_ids: list[int]
    account_results: list[SyncAccountResultView]
    challenge: str | None = None
    tan_kind: str | None = None
    approval_polls: int = 0
    message: str | None = None
    started_at: datetime


class SyncStartRequest(BaseModel):
    """Anstoss eines Batch- oder Einzel-Syncs.

    ``account_ids`` und ``pin`` gelten nur fuer genau einen Bankzugang. Die PIN
    wird nur bei ``store_pin = false`` verwendet und nie zurueckgegeben
    (F-SYN-08, N-SEC-02).
    """

    bank_access_id: int | None = Field(default=None, ge=1)
    account_ids: list[PositiveInt] | None = None
    pin: str | None = Field(default=None, min_length=1, max_length=64)


class SyncStartResponse(BaseModel):
    """Die gestarteten Jobs; je Bankzugang hoechstens einer (F-SYN-11)."""

    jobs: list[SyncJobView]


class SyncAccountStartRequest(BaseModel):
    """Anstoss eines Einzel-Syncs; die PIN nur im Arbeitsspeicher (N-SEC-02)."""

    pin: str | None = Field(default=None, min_length=1, max_length=64)


class TanSubmitRequest(BaseModel):
    """TAN-Eingabe einer offenen Text-Abfrage (F-SYN-04).

    Reine Leerzeichen sind keine gueltige TAN: ``pattern=r"\\S"`` verlangt
    mindestens ein Nicht-Leerzeichen-Zeichen und lehnt sie bereits bei der
    Validierung als 422 ab (statt eines ``ValueError`` aus der Service-Schicht,
    der als 500 enden wuerde).
    """

    tan: str = Field(min_length=1, max_length=64, pattern=r"\S")


class SyncRunListResponse(BaseModel):
    """Sync-Protokoll mit Cursor (F-SYN-07, SPEC F-MCP-11)."""

    items: list[ImportRunView]
    next_cursor: str | None = None


# -------------------------------------------------------------- Uebersetzung


def _result_view(result: AccountSyncResult) -> SyncAccountResultView:
    """Uebersetzt ein Kontoergebnis in das API-DTO."""

    return SyncAccountResultView(
        account_id=result.account_id,
        status=result.status,
        import_run_id=result.import_run_id,
        attempts=result.attempts,
        message=result.message,
    )


def _job_view(state: SyncJobState) -> SyncJobView:
    """Uebersetzt eine Runner-Momentaufnahme in das API-DTO (F-SYN-04/07)."""

    return SyncJobView(
        job_id=state.job_id,
        bank_access_id=state.bank_access_id,
        status=state.status,
        account_ids=list(state.account_ids),
        account_results=[_result_view(result) for result in state.account_results],
        challenge=state.challenge,
        tan_kind=state.tan_kind,
        approval_polls=state.approval_polls,
        message=state.message,
        started_at=state.started_at,
    )


def _conflict(services: AppServices, job_id: str, message: str) -> ConflictError:
    """Baut einen 409 mit dem aktuellen Jobstand als ``current`` (F-CON-02)."""

    try:
        current: SyncJobView | dict[str, object] = _job_view(services.sync_runner.get(job_id))
    except SyncJobNotFound:
        current = {}
    return ConflictError(message, current=current)


def _require_access(session: Session, access_id: int) -> BankAccess:
    """Liefert den Bankzugang oder wirft 404."""

    access = get_access(session, access_id)
    if access is None:
        raise NotFoundError(f"Der Bankzugang {access_id} wurde nicht gefunden.")
    return access


def _require_pin(access: BankAccess, pin: str | None) -> None:
    """Verlangt bei nicht gespeicherter PIN eine Eingabe (F-SYN-08)."""

    if not access.store_pin and pin is None:
        raise DomainError("PIN bitte eingeben.", details={"field": "pin"})


def _active_fints_access_ids(session: Session) -> list[int]:
    """Liefert die Bankzugaenge mit mindestens einem aktiven FinTS-Konto (F-SYN-01)."""

    stmt = (
        select(Account.bank_access_id)
        .where(
            Account.source == "fints",
            Account.active.is_(True),
            Account.bank_access_id.is_not(None),
        )
        .distinct()
        .order_by(Account.bank_access_id)
    )
    return [int(value) for value in session.scalars(stmt) if value is not None]


def _require_accounts(session: Session, access_id: int, account_ids: list[int]) -> None:
    """Prueft ausdruecklich genannte Konten gegen den Zugang (F-SYN-02)."""

    stmt = select(Account.id).where(
        Account.id.in_(account_ids),
        Account.bank_access_id == access_id,
        Account.source == "fints",
        Account.active.is_(True),
    )
    found = set(session.scalars(stmt))
    missing = [account_id for account_id in account_ids if account_id not in found]
    if missing:
        raise DomainError(
            "Konto/Konten nicht gefunden oder nicht aktiv: " + ", ".join(str(item) for item in missing) + "."
        )


# ------------------------------------------------------------------- Routen


@router.post("/sync", response_model=SyncStartResponse, status_code=202)
def start_sync_route(
    payload: SyncStartRequest,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> SyncStartResponse:
    """Startet den Batch-Sync und kehrt sofort mit den Job-IDs zurueck (F-SYN-01/11)."""

    if payload.bank_access_id is not None:
        access = _require_access(session, payload.bank_access_id)
        _require_pin(access, payload.pin)
        if payload.account_ids is not None:
            _require_accounts(session, access.id, payload.account_ids)
        state = services.sync_runner.start(
            access.id,
            account_ids=payload.account_ids,
            pin=payload.pin,
        )
        return SyncStartResponse(jobs=[_job_view(state)])

    access_ids = _active_fints_access_ids(session)
    if not access_ids:
        raise DomainError("Es gibt keinen Bankzugang mit aktiven FinTS-Konten.")
    if (payload.account_ids is not None or payload.pin is not None) and len(access_ids) != 1:
        raise DomainError("account_ids und pin sind nur mit genau einem Bankzugang zulaessig.")
    if payload.account_ids is not None:
        _require_accounts(session, access_ids[0], payload.account_ids)
    jobs = [
        _job_view(services.sync_runner.start(access_id, account_ids=payload.account_ids, pin=payload.pin))
        for access_id in access_ids
    ]
    return SyncStartResponse(jobs=jobs)


@router.post("/accounts/{account_id}/sync", response_model=SyncJobView, status_code=202)
def start_account_sync_route(
    account_id: int,
    payload: SyncAccountStartRequest,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> SyncJobView:
    """Startet den Einzel-Sync eines Kontos (F-SYN-02)."""

    account = session.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"Das Konto {account_id} wurde nicht gefunden.")
    if account.bank_access_id is None or account.source != "fints" or not account.active:
        raise DomainError("Das Konto ist kein aktives FinTS-Konto.")
    access = _require_access(session, account.bank_access_id)
    _require_pin(access, payload.pin)
    state = services.sync_runner.start(account.bank_access_id, account_ids=[account.id], pin=payload.pin)
    return _job_view(state)


@router.get("/sync/jobs/{job_id}", response_model=SyncJobView)
def get_sync_job_route(
    job_id: str,
    services: AppServices = Depends(get_services),
) -> SyncJobView:
    """Liefert Status und Challenge einer offenen TAN-Abfrage (F-SYN-04)."""

    try:
        state = services.sync_runner.get(job_id)
    except SyncJobNotFound as exc:
        raise NotFoundError(f"Der Sync-Job {job_id} wurde nicht gefunden.") from exc
    return _job_view(state)


@router.post("/sync/jobs/{job_id}/tan", response_model=SyncJobView)
def submit_tan_route(
    job_id: str,
    payload: TanSubmitRequest,
    services: AppServices = Depends(get_services),
) -> SyncJobView:
    """Nimmt eine TAN an; die zweite Antwort liefert 409 (F-SYN-04)."""

    try:
        state = services.sync_runner.submit_tan(job_id, payload.tan)
    except SyncJobNotFound as exc:
        raise NotFoundError(f"Der Sync-Job {job_id} wurde nicht gefunden.") from exc
    except SyncTanConflictError as exc:
        raise _conflict(services, job_id, str(exc)) from exc
    return _job_view(state)


@router.post("/sync/jobs/{job_id}/approve-poll", response_model=SyncJobView)
def poll_app_approval_route(
    job_id: str,
    services: AppServices = Depends(get_services),
) -> SyncJobView:
    """Fragt eine App-Freigabe erneut ab und meldet den Fortschritt (F-SYN-04)."""

    try:
        state = services.sync_runner.poll_app_approval(job_id)
    except SyncJobNotFound as exc:
        raise NotFoundError(f"Der Sync-Job {job_id} wurde nicht gefunden.") from exc
    except SyncTanConflictError as exc:
        raise _conflict(services, job_id, str(exc)) from exc
    return _job_view(state)


@router.post("/sync/jobs/{job_id}/cancel", response_model=SyncJobView)
def cancel_sync_route(
    job_id: str,
    services: AppServices = Depends(get_services),
) -> SyncJobView:
    """Bricht den Lauf ab; ein bereits beendeter Job bleibt unveraendert (F-SYN-10)."""

    try:
        state = services.sync_runner.cancel(job_id)
    except SyncJobNotFound as exc:
        raise NotFoundError(f"Der Sync-Job {job_id} wurde nicht gefunden.") from exc
    except SyncTanConflictError as exc:
        raise _conflict(services, job_id, str(exc)) from exc
    return _job_view(state)


@router.get("/sync/runs", response_model=SyncRunListResponse)
def list_sync_runs_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    account_id: int | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> SyncRunListResponse:
    """Listet das Sync-Protokoll, paginiert und je Konto filterbar (F-SYN-07)."""

    params = resolve_page_params(request, services.settings)
    stmt = (
        select(ImportRun)
        .options(selectinload(ImportRun.errors))
        .where(ImportRun.kind == "sync")
        .order_by(ImportRun.started_at.desc(), ImportRun.id.desc())
    )
    if account_id is not None:
        stmt = stmt.where(ImportRun.account_id == account_id)
    runs = [run_view(run) for run in session.scalars(stmt)]
    offset = decode_cursor(params.cursor) if params.cursor is not None else 0
    window = runs[offset : offset + params.limit]
    next_offset = offset + params.limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(runs) else None
    return SyncRunListResponse(items=window, next_cursor=next_cursor)
