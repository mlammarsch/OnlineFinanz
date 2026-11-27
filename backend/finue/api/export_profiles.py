"""REST-Router fuer Exportprofile (F-EXP-03, F-CON-02).

Ein Exportprofil buendelt die CSV-Optionen (Trennzeichen, Dezimalformat,
Datumsformat, Kodierung, Formel-Schutz) und die Spaltenauswahl/-reihenfolge
(F-EXP-03). Der Router bietet CRUD unter ``/api/export-profiles``. Aenderungen
tragen ``version`` und liefern bei veraltetem Stand 409 mit dem aktuellen Stand
(F-CON-02). Die Fachlogik liegt in :mod:`finue.services.export_profiles`; der
Router uebersetzt nur HTTP. Er bringt ``prefix="/api"`` und die Auth-Naht
``require_user`` selbst mit (T-088-Konvention).
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
from finue.models.export_profile import ExportProfile
from finue.services.export_profiles import (
    ExportProfileVersionConflict,
    create_profile,
    delete_profile,
    get_profile,
    list_profiles,
    update_profile,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["export-profiles"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class ExportProfileView(BaseModel):
    """Gespeichertes Exportprofil (F-EXP-03)."""

    id: int
    name: str
    is_default: bool
    options: dict[str, Any]
    columns: list[str]
    version: int


class ExportProfileListResponse(BaseModel):
    """Profilliste mit Cursor (F-EXP-03, F-MCP-11)."""

    items: list[ExportProfileView]
    next_cursor: str | None = None


class ExportProfileCreateRequest(BaseModel):
    """Anlage eines Exportprofils (F-EXP-03)."""

    name: str = Field(min_length=1, max_length=255)
    options: dict[str, Any] | None = None
    columns: list[str] | None = None
    is_default: bool = False


class ExportProfileUpdateRequest(VersionedRequest):
    """Aenderung eines Exportprofils mit Versionspruefung (F-EXP-03, F-CON-02)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    options: dict[str, Any] | None = None
    columns: list[str] | None = None
    is_default: bool | None = None


# ---------------------------------------------------------------- Uebersetzung


def _profile_dto(profile: ExportProfile) -> ExportProfileView:
    """Uebersetzt ein Profil in das API-DTO (F-EXP-03)."""

    return ExportProfileView(
        id=profile.id,
        name=profile.name,
        is_default=profile.is_default,
        options=dict(profile.options),
        columns=[str(column) for column in profile.columns],
        version=profile.version,
    )


def _require_profile(session: Session, profile_id: int) -> ExportProfile:
    """Liefert ein Profil oder wirft ``NotFoundError``."""

    profile = get_profile(session, profile_id)
    if profile is None:
        raise NotFoundError(f"Exportprofil {profile_id} wurde nicht gefunden.")
    return profile


def _page(items: list[ExportProfileView], limit: int, cursor: str | None) -> tuple[list[ExportProfileView], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.get("/export-profiles", response_model=ExportProfileListResponse)
def list_profiles_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> ExportProfileListResponse:
    """Listet Exportprofile, Standard zuerst (F-EXP-03)."""

    params = resolve_page_params(request, services.settings)
    views = [_profile_dto(profile) for profile in list_profiles(session)]
    page_items, next_cursor = _page(views, params.limit, params.cursor)
    return ExportProfileListResponse(items=page_items, next_cursor=next_cursor)


@router.post("/export-profiles", response_model=ExportProfileView, status_code=status.HTTP_201_CREATED)
def create_profile_route(
    payload: ExportProfileCreateRequest,
    session: Session = Depends(get_session),
) -> ExportProfileView:
    """Legt ein Exportprofil an (F-EXP-03)."""

    try:
        profile = create_profile(
            session,
            name=payload.name,
            options=payload.options,
            columns=payload.columns,
            is_default=payload.is_default,
        )
    except ValueError as exc:
        raise DomainError(str(exc)) from exc
    return _profile_dto(profile)


@router.get("/export-profiles/{profile_id}", response_model=ExportProfileView)
def get_profile_route(
    profile_id: int,
    session: Session = Depends(get_session),
) -> ExportProfileView:
    """Liefert ein Exportprofil (F-EXP-03)."""

    return _profile_dto(_require_profile(session, profile_id))


@router.patch("/export-profiles/{profile_id}", response_model=ExportProfileView)
def update_profile_route(
    profile_id: int,
    payload: ExportProfileUpdateRequest,
    session: Session = Depends(get_session),
) -> ExportProfileView:
    """Aendert ein Exportprofil mit Versionspruefung und 409 (F-EXP-03, F-CON-02)."""

    changes = payload.model_dump(exclude_unset=True)
    changes.pop("version", None)
    try:
        profile = update_profile(session, profile_id, expected_version=payload.version, **changes)
    except LookupError as exc:
        raise NotFoundError(f"Exportprofil {profile_id} wurde nicht gefunden.") from exc
    except ExportProfileVersionConflict as exc:
        raise ConflictError(current=_profile_dto(_require_profile(session, profile_id))) from exc
    except ValueError as exc:
        raise DomainError(str(exc)) from exc
    return _profile_dto(profile)


@router.delete("/export-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_route(
    profile_id: int,
    version: int = Query(..., ge=1, description="Bekannte Versionsnummer des Profils"),
    session: Session = Depends(get_session),
) -> Response:
    """Loescht ein Exportprofil mit Versionspruefung (F-EXP-03, F-CON-02)."""

    try:
        deleted = delete_profile(session, profile_id, expected_version=version)
    except ExportProfileVersionConflict as exc:
        raise ConflictError(current=_profile_dto(_require_profile(session, profile_id))) from exc
    if not deleted:
        raise NotFoundError(f"Exportprofil {profile_id} wurde nicht gefunden.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
