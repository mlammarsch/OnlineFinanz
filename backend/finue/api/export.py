"""REST-Router fuer den Export (F-EXP-01, F-EXP-02, F-EXP-04, F-EXP-05).

Setzt den Service aus :mod:`finue.services.export` als ``POST /api/export`` um.
Der Endpunkt aendert keinen Datenbestand; er waehlt Buchungen nach Umfang und
Filtern, rendert JSON oder Markdown und liefert die Datei mit
``Content-Disposition``. Markdown kann zusaetzlich als ``text/plain`` fuer die
Zwischenablage abgerufen werden.

Die Auth-Naht traegt der Router selbst (``require_user``); er bringt sein
``prefix="/api"`` mit (FastAPI 0.165 setzt das Elternpraefix nicht in
``APIRoute.path``, siehe ``decisions.md`` T-088).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.deps import get_clock, get_services, get_session, require_user
from finue.api.errors import PayloadTooLargeError, ValidationApiError
from finue.services.export import (
    CLIPBOARD_MEDIA_TYPE,
    ExportFormat,
    ExportLimitExceeded,
    ExportRequest,
    ExportScope,
    ExportValidationError,
    run_export,
)
from finue.services.search import SearchPeriod, SortDirection, SortField, local_today

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["export"], dependencies=[Depends(require_user)])

ScopeParam = Literal["all", "account", "group", "search", "selection"]
FormatParam = Literal["json", "md"]
PeriodParam = Literal["30", "90", "365", "current_year", "previous_year", "all", "custom"]
StatusParam = Literal["gebucht", "vorgemerkt"]
SortParam = Literal["date", "amount", "counterparty"]
DirectionParam = Literal["asc", "desc"]


class ExportRequestModel(BaseModel):
    """Anfragekoerper des Exports (F-EXP-01, F-EXP-02).

    Die Filterfelder entsprechen ``GET /api/search``; ``scope`` erweitert sie um
    ``search`` (freie Suchtreffer) und ``selection`` (explizite ID-Liste).
    """

    model_config = ConfigDict(populate_by_name=True)

    format: FormatParam = "json"
    scope: ScopeParam = "all"
    account_id: int | None = None
    group_id: int | None = None
    q: str | None = None
    period: PeriodParam = "365"
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")
    checked: bool | None = None
    reconciled: bool | None = None
    with_note: bool = False
    status: StatusParam | None = None
    sort: SortParam = "date"
    direction: DirectionParam = "asc"
    transaction_ids: list[int] | None = None


def _to_export_request(payload: ExportRequestModel) -> ExportRequest:
    """Uebersetzt den Anfragekoerper in die Service-Beschreibung."""

    return ExportRequest(
        format=ExportFormat(payload.format),
        scope=ExportScope(payload.scope),
        account_id=payload.account_id,
        group_id=payload.group_id,
        search=payload.q,
        period=SearchPeriod(payload.period),
        from_date=payload.from_date,
        to_date=payload.to_date,
        checked=payload.checked,
        reconciled=payload.reconciled,
        with_note=payload.with_note,
        status=payload.status,
        sort=SortField(payload.sort),
        direction=SortDirection(payload.direction),
        transaction_ids=payload.transaction_ids,
    )


@router.post("/export")
def export_route(
    payload: ExportRequestModel,
    download: bool = Query(
        default=True,
        description=(
            "true: Datei mit Content-Disposition; false: Text fuer die Zwischenablage (Markdown als text/plain)."
        ),
    ),
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
    clock: Callable[[], datetime] = Depends(get_clock),
) -> Response:
    """Exportiert Buchungen als JSON oder Markdown (F-EXP-01/02/04/05).

    Der Zeitraum und die Filter der Ansicht gelten auch hier (F-EXP-02). Bei
    ``download=false`` liefert Markdown ``text/plain`` ohne Dateikopf, damit die
    Oberflaeche den Inhalt in die Zwischenablage kopieren kann. Eine zu grosse
    Buchungsmenge wird als 413 mit Klartext abgewiesen (Grenze, F-EXP-01).
    """

    request = _to_export_request(payload)
    now = clock()
    today = local_today(lambda: now)
    try:
        result = run_export(session, request, now=now, today=today, max_rows=services.settings.export_max_rows)
    except ExportLimitExceeded as exc:
        raise PayloadTooLargeError(
            str(exc),
            details={"limit": services.settings.export_max_rows},
        ) from exc
    except ExportValidationError as exc:
        raise ValidationApiError(str(exc)) from exc

    media_type = result.media_type
    headers: dict[str, str] = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{result.filename}"'
    elif request.format is ExportFormat.MARKDOWN:
        media_type = CLIPBOARD_MEDIA_TYPE

    return Response(content=result.content.encode("utf-8"), media_type=media_type, headers=headers)
