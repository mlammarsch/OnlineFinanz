"""REST-Router fuer Datei-Import, Lauf-Protokoll und Vorlagen-Nutzung (F-IMP-01..08).

Der Assistent laeuft in drei Schritten und nutzt die vorhandenen M2-Services, ohne
Fachlogik zu duplizieren (D10):

- ``POST /api/import/detect``: nimmt die rohe Datei (Groessengrenze aus
  F-MCP-11/``config``), erkennt CSV, fremdes JSON oder das eigene JSON-Format
  (F-IMP-04) und liefert Vorschlaege fuer Kodierung, Trennzeichen, Dezimal- und
  Datumsformat samt Kopfzeile/Vorspann (F-IMP-02). Die Bytes liegen danach nur im
  Arbeitsspeicher unter einem Token (Entscheidung in ``decisions.md``).
- ``POST /api/import/preview``: klassifiziert jede Zeile als neu/vorhanden/
  Verdacht gegen das Zielkonto, mit Mapping (F-IMP-03) und Wiederherstellungs-
  option (F-IMP-07, F-IMP-06).
- ``POST /api/import/execute``: fuehrt den Import aus, protokolliert den Lauf
  (Herkunft ``import``) und meldet ``import.finished`` und ``transaction.changed``.

``GET /api/import-runs`` liefert Sync- und Datei-Laeufe mit ihren Fehlerzeilen,
paginiert (F-SYN-07, F-IMP-06). Der Router bringt ``prefix="/api"`` und die
Auth-Naht ``require_user`` selbst mit (T-088-Konvention).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypeVar, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from finue.api.container import AppServices
from finue.api.conventions import check_upload_size
from finue.api.deps import get_event_bus, get_services, get_session, get_upload_store, require_user
from finue.api.errors import DomainError, NotFoundError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.import_template import ImportTemplate
from finue.services.accounts import AccountNotFound, get_account
from finue.services.events import EventBus
from finue.services.export import EXPORT_GUARD_PRE_FILTER, strip_export_guard
from finue.services.import_detect import CsvOptions, detect_import, load_rows
from finue.services.import_preview import ImportPreview, PreviewRow, build_preview
from finue.services.import_run import execute_import
from finue.services.import_templates import get_template, template_pre_filter
from finue.services.import_uploads import UploadExpired, UploadStore
from finue.services.paypal_filter import PRE_FILTERS, PreFilter

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["import"], dependencies=[Depends(require_user)])

Item = TypeVar("Item")

#: Vorverarbeitungsschritte des Imports: PayPal-Filter (F-IMP-08) und der
#: Ruecknahme der Formel-Entschaerfung des eigenen CSV-Exports (F-EXP-06, T-098).
_IMPORT_PRE_FILTERS: dict[str, PreFilter] = {
    **PRE_FILTERS,
    EXPORT_GUARD_PRE_FILTER: strip_export_guard,
}

#: Standardname der Statusspalte fuer den PayPal-Vorverarbeitungsschritt (F-IMP-08).
_DEFAULT_STATUS_FIELD = "Status"


# ------------------------------------------------------------------- DTOs


class ImportFormatOptions(BaseModel):
    """Manuelle Korrekturwerte fuer die CSV-Erkennung (F-IMP-02)."""

    encoding: str | None = None
    delimiter: str | None = None
    decimal_separator: str | None = None
    date_format: str | None = None
    has_header: bool | None = None
    skip_rows: int | None = Field(default=None, ge=0)


class CsvSuggestionView(BaseModel):
    """Erkannter bzw. korrigierter CSV-Formatvorschlag (F-IMP-02)."""

    encoding: str
    delimiter: str
    decimal_separator: str
    date_format: str | None
    has_header: bool
    skip_rows: int


class ImportDetectionResponse(BaseModel):
    """Ergebnis der Formaterkennung samt Upload-Token (F-IMP-01/02/04)."""

    kind: str
    token: str
    csv: CsvSuggestionView | None = None


class ImportSourceRequest(BaseModel):
    """Gemeinsame Felder fuer Vorschau und Ausfuehrung eines Datei-Imports."""

    token: str = Field(min_length=1, description="Token aus POST /api/import/detect")
    account_id: int = Field(ge=1, description="Zielkonto des Imports")
    kind: str | None = Field(default=None, pattern="^(csv|json|own_json)$")
    mapping: dict[str, Any] | None = None
    template_id: int | None = Field(default=None, ge=1)
    format: ImportFormatOptions | None = None
    status_field: str = Field(default=_DEFAULT_STATUS_FIELD, min_length=1, max_length=64)


class ImportPreviewRequest(ImportSourceRequest):
    """Anfrage der Import-Vorschau (F-IMP-06)."""


class ImportExecuteRequest(ImportSourceRequest):
    """Anfrage der Import-Ausfuehrung mit Wiederherstellungsoption (F-IMP-07)."""

    restore_flags_for_existing: bool = False


class ImportPreviewRow(BaseModel):
    """Eine klassifizierte Zeile der Import-Vorschau (F-IMP-06)."""

    row_no: int
    status: str
    fingerprint: str
    booking_date: str
    value_date: str | None
    amount_cents: int
    currency: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None


class ImportPreviewSummary(BaseModel):
    """Anzahl je Vorschau-Status (F-IMP-06)."""

    neu: int
    vorhanden: int
    verdachtsdublette: int


class ImportPreviewResponse(BaseModel):
    """Antwort der Import-Vorschau (F-IMP-06)."""

    account_id: int
    kind: str
    summary: ImportPreviewSummary
    rows: list[ImportPreviewRow]


class ImportRunErrorView(BaseModel):
    """Einzelner Fehler eines Import-Laufs (F-SYN-05, F-IMP-06)."""

    id: int
    row_no: int | None
    code: str
    message: str


class ImportRunView(BaseModel):
    """Ein Sync- oder Datei-Lauf mit Zaehlern und Fehlerzeilen (F-SYN-07, F-IMP-06)."""

    id: int
    account_id: int
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    new_count: int
    skipped_count: int
    suspected_count: int
    failed_count: int
    message: str | None
    errors: list[ImportRunErrorView]


class ImportRunListResponse(BaseModel):
    """Lauf-Protokoll mit Cursor (F-SYN-07, F-MCP-11)."""

    items: list[ImportRunView]
    next_cursor: str | None = None


# ---------------------------------------------------------------- Uebersetzung


def _require_account(session: Session, account_id: int) -> None:
    """Wirft ``NotFoundError``, wenn das Zielkonto unbekannt ist."""

    try:
        get_account(session, account_id)
    except AccountNotFound as exc:
        raise NotFoundError(f"Das Konto {account_id} wurde nicht gefunden.") from exc


def _load_upload(store: UploadStore, token: str) -> bytes:
    """Holt die hochgeladenen Bytes; abgelaufene Token werden als 422 gemeldet."""

    try:
        return store.get(token)
    except UploadExpired as exc:
        raise DomainError(
            "Der Upload ist abgelaufen oder unbekannt; bitte die Datei erneut hochladen.",
            details={"field": "token"},
        ) from exc


def _pick(*values: str | None) -> str | None:
    """Liefert den ersten gesetzten Wert (Anfrage hat Vorrang vor Vorlage)."""

    for value in values:
        if value is not None:
            return value
    return None


def _csv_options(
    request_format: ImportFormatOptions | None,
    template: ImportTemplate | None,
) -> CsvOptions:
    """Fasst Anfrage- und Vorlagenformat zur CSV-Korrektur zusammen (F-IMP-02)."""

    return CsvOptions(
        encoding=_pick(
            request_format.encoding if request_format else None,
            template.encoding if template is not None else None,
        ),
        delimiter=_pick(
            request_format.delimiter if request_format else None,
            template.delimiter if template is not None else None,
        ),
        decimal_separator=_pick(
            request_format.decimal_separator if request_format else None,
            template.decimal_separator if template is not None else None,
        ),
        date_format=_pick(
            request_format.date_format if request_format else None,
            template.date_format if template is not None else None,
        ),
        has_header=request_format.has_header if request_format else None,
        skip_rows=request_format.skip_rows if request_format else None,
    )


def _with_format_options(mapping: Mapping[str, Any], options: CsvOptions) -> Mapping[str, Any]:
    """Mischt erkannte/vorgegebene Datums- und Dezimalformate in eine Mapping-Kopie (F-IMP-02).

    ``build_preview``/``execute_import`` lesen Formatoptionen nur aus dem Mapping;
    Trennzeichen, Kodierung, Kopfzeile und Vorspann steuern bereits die
    Zeilenzerlegung. Die Kopie verhindert, dass die Anreicherung am
    ORM-Mapping einer Vorlage haengen bleibt.
    """

    enriched = dict(mapping)
    if options.date_format is not None and "date_format" not in enriched:
        enriched["date_format"] = options.date_format
    if options.decimal_separator is not None and "decimal_separator" not in enriched:
        enriched["decimal_separator"] = options.decimal_separator
    return enriched


def _resolve_import(
    session: Session,
    payload: ImportSourceRequest,
    content: bytes,
) -> tuple[object, str, Mapping[str, Any] | None]:
    """Loest Anfrage, Vorlage und Datei zu Parser-Eingabe, Art und Mapping auf."""

    template = None
    if payload.template_id is not None:
        template = get_template(session, payload.template_id)
        if template is None:
            raise DomainError(f"Die Import-Vorlage {payload.template_id} wurde nicht gefunden.")

    options = _csv_options(payload.format, template)
    detected = detect_import(content, options)

    if detected.kind == "own_json":
        # Das eigene Format (F-IMP-04) wird ohne Mapping gelesen; Vorlage und
        # Mapping der Anfrage sind hier unerheblich.
        data = load_rows(content, kind="own_json", options=options)
        return data, "own_json", None

    if template is not None and template.kind != detected.kind:
        raise DomainError("Die gewaehlte Vorlage passt nicht zum erkannten Dateiformat.")
    if payload.kind is not None and payload.kind != detected.kind:
        raise DomainError("Die angegebene Import-Art passt nicht zum erkannten Dateiformat.")

    mapping: Mapping[str, Any] | None = template.mapping if template is not None else payload.mapping
    if mapping is None:
        raise DomainError("Fuer CSV oder fremdes JSON ist ein Mapping erforderlich (F-IMP-03).")
    mapping = _with_format_options(mapping, options)

    data = load_rows(content, kind=detected.kind, options=options)
    data = _apply_pre_filter(data, template, payload.status_field)
    return data, detected.kind, mapping


def _apply_pre_filter(
    data: object,
    template: ImportTemplate | None,
    status_field: str,
) -> object:
    """Wendet den Vorverarbeitungsschritt einer Vorlage an (F-IMP-08).

    Nur Zeilenlisten kommen in Frage; das eigene JSON-Format hat keinen
    Vorverarbeitungsschritt.
    """

    if template is None:
        return data
    flag = template_pre_filter(template)
    if flag is None:
        return data
    filter_func = _IMPORT_PRE_FILTERS.get(flag)
    if filter_func is None:
        return data
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return filter_func(cast("list[dict[str, Any]]", data), status_field)
    return data


def _preview_row_dto(row: PreviewRow) -> ImportPreviewRow:
    """Uebersetzt eine Vorschauzeile in das API-DTO (F-IMP-06)."""

    data = row.parsed_data
    return ImportPreviewRow(
        row_no=row.row_no,
        status=row.status,
        fingerprint=row.fingerprint,
        booking_date=data.booking_date.isoformat(),
        value_date=data.value_date.isoformat() if data.value_date is not None else None,
        amount_cents=data.amount_cents,
        currency=data.currency,
        counterparty=data.counterparty,
        counterparty_iban=data.counterparty_iban,
        purpose=data.purpose,
    )


def _preview_dto(account_id: int, kind: str, preview: ImportPreview) -> ImportPreviewResponse:
    """Baut die Vorschauantwort (F-IMP-06)."""

    return ImportPreviewResponse(
        account_id=account_id,
        kind=kind,
        summary=ImportPreviewSummary(
            neu=preview.summary.neu,
            vorhanden=preview.summary.vorhanden,
            verdachtsdublette=preview.summary.verdachtsdublette,
        ),
        rows=[_preview_row_dto(row) for row in preview.rows],
    )


def _error_dto(error: ImportRunError) -> ImportRunErrorView:
    """Uebersetzt eine Fehlerzeile (F-SYN-05)."""

    return ImportRunErrorView(id=error.id, row_no=error.row_no, code=error.code, message=error.message)


def run_view(run: ImportRun) -> ImportRunView:
    """Uebersetzt einen Import-Lauf in das API-DTO (F-SYN-07, F-IMP-06).

    Oeffentlich, weil das Sync-Protokoll (``GET /api/sync/runs``, T-112)
    dieselbe Darstellung nutzt.
    """

    return ImportRunView(
        id=run.id,
        account_id=run.account_id,
        kind=run.kind,
        status=run.status,
        started_at=cast(datetime, run.started_at),
        finished_at=cast("datetime | None", run.finished_at),
        new_count=run.new_count,
        skipped_count=run.skipped_count,
        suspected_count=run.suspected_count,
        failed_count=run.failed_count,
        message=run.message,
        errors=[_error_dto(error) for error in run.errors],
    )


def _page(items: list[Item], limit: int, cursor: str | None) -> tuple[list[Item], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.post("/import/detect", response_model=ImportDetectionResponse)
async def detect_import_route(
    request: Request,
    encoding: str | None = None,
    delimiter: str | None = None,
    decimal_separator: str | None = None,
    date_format: str | None = None,
    has_header: bool | None = None,
    skip_rows: int | None = Query(default=None, ge=0),
    services: AppServices = Depends(get_services),
    upload_store: UploadStore = Depends(get_upload_store),
) -> ImportDetectionResponse:
    """Erkennt das Importformat, speichert den Upload und liefert Vorschlaege (F-IMP-01/02/04)."""

    data = await request.body()
    if not data:
        raise DomainError("Es wurde keine Datei hochgeladen.")
    check_upload_size(len(data), services.settings)

    options = CsvOptions(
        encoding=encoding,
        delimiter=delimiter,
        decimal_separator=decimal_separator,
        date_format=date_format,
        has_header=has_header,
        skip_rows=skip_rows,
    )
    try:
        detected = detect_import(data, options)
    except ValueError as exc:
        raise DomainError(str(exc)) from exc

    token = upload_store.put(data)
    suggestion = detected.csv
    return ImportDetectionResponse(
        kind=detected.kind,
        token=token,
        csv=(
            CsvSuggestionView(
                encoding=suggestion.encoding,
                delimiter=suggestion.delimiter,
                decimal_separator=suggestion.decimal_separator,
                date_format=suggestion.date_format,
                has_header=suggestion.has_header,
                skip_rows=suggestion.skip_rows,
            )
            if suggestion is not None
            else None
        ),
    )


@router.post("/import/preview", response_model=ImportPreviewResponse)
def preview_import_route(
    payload: ImportPreviewRequest,
    session: Session = Depends(get_session),
    upload_store: UploadStore = Depends(get_upload_store),
) -> ImportPreviewResponse:
    """Erzeugt eine Vorschau je Zeile gegen den Kontobestand (F-IMP-03/06/07)."""

    _require_account(session, payload.account_id)
    content = _load_upload(upload_store, payload.token)
    try:
        data, kind, mapping = _resolve_import(session, payload, content)
        preview = build_preview(payload.account_id, data, mapping, kind, session)
    except DomainError:
        raise
    except ValueError as exc:
        raise DomainError(str(exc)) from exc
    return _preview_dto(payload.account_id, kind, preview)


@router.post("/import/execute", response_model=ImportRunView)
def execute_import_route(
    payload: ImportExecuteRequest,
    session: Session = Depends(get_session),
    upload_store: UploadStore = Depends(get_upload_store),
    event_bus: EventBus = Depends(get_event_bus),
) -> ImportRunView:
    """Fuehrt den Datei-Import aus und protokolliert den Lauf (F-IMP-06, F-SYN-07)."""

    _require_account(session, payload.account_id)
    content = _load_upload(upload_store, payload.token)
    try:
        data, kind, mapping = _resolve_import(session, payload, content)
    except DomainError:
        raise
    except ValueError as exc:
        raise DomainError(str(exc)) from exc

    run = execute_import(
        payload.account_id,
        data,
        mapping,
        kind,
        session,
        restore_flags_for_existing=payload.restore_flags_for_existing,
    )
    event_bus.emit("import.finished", account=payload.account_id, run=run.id)
    event_bus.emit("transaction.changed", account=payload.account_id)
    return run_view(run)


@router.get("/import-runs", response_model=ImportRunListResponse)
def list_import_runs_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    account_id: int | None = None,
    kind: str | None = Query(default=None, pattern="^(sync|csv|json)$"),
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> ImportRunListResponse:
    """Listet Sync- und Datei-Laeufe mit Fehlerzeilen (F-SYN-07, F-IMP-06)."""

    params = resolve_page_params(request, services.settings)
    stmt = (
        select(ImportRun)
        .options(selectinload(ImportRun.errors))
        .order_by(ImportRun.started_at.desc(), ImportRun.id.desc())
    )
    if account_id is not None:
        stmt = stmt.where(ImportRun.account_id == account_id)
    if kind is not None:
        stmt = stmt.where(ImportRun.kind == kind)
    runs = [run_view(run) for run in session.scalars(stmt)]
    page_items, next_cursor = _page(runs, params.limit, params.cursor)
    return ImportRunListResponse(items=page_items, next_cursor=next_cursor)
