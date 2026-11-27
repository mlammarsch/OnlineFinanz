"""REST-Router fuer Import-Vorlagen (F-IMP-05, F-CON-02).

Vorlagen speichern das Mapping und die Formatoptionen eines Datei-Imports. Der
Router bietet CRUD unter ``/api/import-templates``; ``?account_id=`` schlaegt
die fuer ein Konto passenden Vorlagen vor (konto-spezifische zuerst, danach die
kontounabhaengigen, F-IMP-05). Aenderungen tragen ``version`` und liefern bei
veraltetem Stand 409 mit dem aktuellen Stand (F-CON-02). Die Fachlogik liegt in
:mod:`finue.services.import_templates`; der Router uebersetzt nur HTTP. Er
bringt ``prefix="/api"`` und die Auth-Naht ``require_user`` selbst mit
(T-088-Konvention).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.conventions import VersionedRequest
from finue.api.deps import get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.models.import_template import ImportTemplate
from finue.services.accounts import AccountNotFound, get_account
from finue.services.import_templates import (
    TemplateVersionConflict,
    create_template,
    delete_template,
    get_template,
    list_templates,
    suggest_templates,
    template_pre_filter,
    update_template,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["import-templates"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class ImportTemplateView(BaseModel):
    """Gespeicherte Import-Vorlage (F-IMP-05)."""

    id: int
    name: str
    kind: str
    account_id: int | None
    mapping: dict[str, Any]
    delimiter: str | None
    encoding: str | None
    date_format: str | None
    decimal_separator: str | None
    pre_filter: str | None
    version: int


class ImportTemplateListResponse(BaseModel):
    """Vorlagenliste mit Cursor (F-IMP-05, F-MCP-11)."""

    items: list[ImportTemplateView]
    next_cursor: str | None = None


class ImportTemplateCreateRequest(BaseModel):
    """Anlage einer Import-Vorlage (F-IMP-05)."""

    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(pattern="^(csv|json)$")
    mapping: dict[str, Any]
    account_id: int | None = None
    delimiter: str | None = Field(default=None, max_length=10)
    encoding: str | None = Field(default=None, max_length=20)
    date_format: str | None = Field(default=None, max_length=30)
    decimal_separator: str | None = Field(default=None, max_length=5)


class ImportTemplateUpdateRequest(VersionedRequest):
    """Aenderung einer Import-Vorlage mit Versionspruefung (F-IMP-05, F-CON-02)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    kind: str | None = Field(default=None, pattern="^(csv|json)$")
    mapping: dict[str, Any] | None = None
    account_id: int | None = None
    delimiter: str | None = Field(default=None, max_length=10)
    encoding: str | None = Field(default=None, max_length=20)
    date_format: str | None = Field(default=None, max_length=30)
    decimal_separator: str | None = Field(default=None, max_length=5)


# ---------------------------------------------------------------- Uebersetzung


def _template_dto(template: ImportTemplate) -> ImportTemplateView:
    """Uebersetzt eine Vorlage in das API-DTO (F-IMP-05)."""

    return ImportTemplateView(
        id=template.id,
        name=template.name,
        kind=template.kind,
        account_id=template.account_id,
        mapping=dict(template.mapping),
        delimiter=template.delimiter,
        encoding=template.encoding,
        date_format=template.date_format,
        decimal_separator=template.decimal_separator,
        pre_filter=template_pre_filter(template),
        version=template.version,
    )


def _require_template(session: Session, template_id: int) -> ImportTemplate:
    """Liefert eine Vorlage oder wirft ``NotFoundError``."""

    template = get_template(session, template_id)
    if template is None:
        raise NotFoundError(f"Import-Vorlage {template_id} wurde nicht gefunden.")
    return template


def _check_account_exists(session: Session, account_id: int | None) -> None:
    """Prueft eine optionale Konto-Zuordnung und meldet unbekannte Konten als 422."""

    if account_id is None:
        return
    try:
        get_account(session, account_id)
    except AccountNotFound as exc:
        raise DomainError(f"Das Konto {account_id} wurde nicht gefunden.") from exc


def _page(
    items: list[ImportTemplateView], limit: int, cursor: str | None
) -> tuple[list[ImportTemplateView], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.get("/import-templates", response_model=ImportTemplateListResponse)
def list_templates_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    account_id: int | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> ImportTemplateListResponse:
    """Listet Vorlagen; mit ``account_id`` als Vorschlag fuer dieses Konto (F-IMP-05)."""

    params = resolve_page_params(request, services.settings)
    templates = suggest_templates(session, account_id) if account_id is not None else list_templates(session)
    views = [_template_dto(template) for template in templates]
    page_items, next_cursor = _page(views, params.limit, params.cursor)
    return ImportTemplateListResponse(items=page_items, next_cursor=next_cursor)


@router.post("/import-templates", response_model=ImportTemplateView, status_code=status.HTTP_201_CREATED)
def create_template_route(
    payload: ImportTemplateCreateRequest,
    session: Session = Depends(get_session),
) -> ImportTemplateView:
    """Legt eine Import-Vorlage an (F-IMP-05)."""

    _check_account_exists(session, payload.account_id)
    try:
        template = create_template(
            session,
            name=payload.name,
            kind=payload.kind,
            mapping=payload.mapping,
            account_id=payload.account_id,
            delimiter=payload.delimiter,
            encoding=payload.encoding,
            date_format=payload.date_format,
            decimal_separator=payload.decimal_separator,
        )
    except ValueError as exc:
        raise DomainError(str(exc)) from exc
    return _template_dto(template)


@router.get("/import-templates/{template_id}", response_model=ImportTemplateView)
def get_template_route(
    template_id: int,
    session: Session = Depends(get_session),
) -> ImportTemplateView:
    """Liefert eine Import-Vorlage (F-IMP-05)."""

    return _template_dto(_require_template(session, template_id))


@router.patch("/import-templates/{template_id}", response_model=ImportTemplateView)
def update_template_route(
    template_id: int,
    payload: ImportTemplateUpdateRequest,
    session: Session = Depends(get_session),
) -> ImportTemplateView:
    """Aendert eine Vorlage mit Versionspruefung und 409 (F-IMP-05, F-CON-02)."""

    changes = payload.model_dump(exclude_unset=True)
    changes.pop("version", None)
    if "account_id" in changes:
        _check_account_exists(session, changes["account_id"])
    try:
        template = update_template(session, template_id, expected_version=payload.version, **changes)
    except LookupError as exc:
        raise NotFoundError(f"Import-Vorlage {template_id} wurde nicht gefunden.") from exc
    except TemplateVersionConflict as exc:
        raise ConflictError(current=_template_dto(_require_template(session, template_id))) from exc
    except ValueError as exc:
        raise DomainError(str(exc)) from exc
    return _template_dto(template)


@router.delete("/import-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template_route(
    template_id: int,
    version: int = Query(..., ge=1, description="Bekannte Versionsnummer der Vorlage"),
    session: Session = Depends(get_session),
) -> Response:
    """Loescht eine Import-Vorlage mit Versionspruefung (F-IMP-05, F-CON-02)."""

    try:
        deleted = delete_template(session, template_id, expected_version=version)
    except TemplateVersionConflict as exc:
        raise ConflictError(current=_template_dto(_require_template(session, template_id))) from exc
    if not deleted:
        raise NotFoundError(f"Import-Vorlage {template_id} wurde nicht gefunden.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
