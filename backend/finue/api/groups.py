"""REST-Router fuer Kontengruppen (F-KTO-03, F-CON-02).

Gruppen anlegen, umbenennen, sortieren (Neuordnung als ID-Liste) und loeschen;
die Gruppe liefert Mitglieder-IDs, Kontenzahl und Summe. Loeschen setzt bei den
Konten ``group_id`` auf NULL, die Konten bleiben erhalten. Umbenennen und
Loeschen pruefen die Version (F-CON-02). Die Summe folgt der Fremdwaehrungsregel
aus ``decisions.md`` (Fremdwaehrung getrennt, nicht addiert). Ereignisse:
``group.changed`` (F-CON-04).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from finue.api.accounts import ForeignTotal
from finue.api.container import AppServices
from finue.api.conventions import VersionedRequest
from finue.api.deps import get_event_bus, get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.api.pagination import decode_cursor, encode_cursor, resolve_page_params
from finue.services.accounts import (
    AccountValidationError,
    GroupNotFound,
    GroupSummary,
    GroupVersionConflict,
    create_group,
    delete_group,
    list_groups,
    rename_group,
    reorder_groups,
)
from finue.services.events import EventBus

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["groups"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class GroupMember(BaseModel):
    """Mitglied einer Gruppe mit Icon-Verweis (F-KTO-03)."""

    id: int
    has_icon: bool
    icon_url: str | None


class GroupView(BaseModel):
    """Gruppe mit Mitgliedern, Kontenzahl und Summe (F-KTO-03)."""

    id: int
    name: str
    sort_order: int
    version: int
    member_ids: list[int]
    account_count: int
    currency: str
    amount_cents: int
    foreign: list[ForeignTotal]
    members: list[GroupMember]


class GroupListResponse(BaseModel):
    """Gruppenliste mit Cursor (F-KTO-03, F-MCP-11)."""

    items: list[GroupView]
    next_cursor: str | None = None


class GroupCreateRequest(BaseModel):
    """Anlage einer Gruppe (F-KTO-03)."""

    name: str = Field(min_length=1, max_length=255)
    sort_order: int = 0


class GroupUpdateRequest(VersionedRequest):
    """Umbenennen einer Gruppe mit Versionspruefung (F-KTO-03, F-CON-02)."""

    name: str = Field(min_length=1, max_length=255)


class GroupOrderRequest(BaseModel):
    """Neuordnung als ID-Liste (F-KTO-03)."""

    ids: list[int]


# ---------------------------------------------------------------- Uebersetzung


def _icon_url(has_icon: bool, account_id: int) -> str | None:
    """Vorgesehener Icon-Verweis eines Mitglieds (F-KTO-03)."""

    return f"/api/accounts/{account_id}/icon" if has_icon else None


def _group_dto(summary: GroupSummary) -> GroupView:
    """Uebersetzt eine Gruppenzusammenfassung in das API-DTO."""

    return GroupView(
        id=summary.id,
        name=summary.name,
        sort_order=summary.sort_order,
        version=summary.version,
        member_ids=list(summary.member_ids),
        account_count=summary.account_count,
        currency=summary.currency,
        amount_cents=summary.amount_cents,
        foreign=[
            ForeignTotal(
                currency=bucket.currency,
                amount_cents=bucket.amount_cents,
                account_ids=list(bucket.account_ids),
            )
            for bucket in summary.foreign
        ],
        members=[
            GroupMember(id=member.id, has_icon=member.has_icon, icon_url=_icon_url(member.has_icon, member.id))
            for member in summary.members
        ],
    )


def _find_group(session: Session, group_id: int) -> GroupView:
    """Liefert die aktuelle Gruppenansicht oder wirft ``NotFoundError``."""

    for summary in list_groups(session):
        if summary.id == group_id:
            return _group_dto(summary)
    raise NotFoundError(f"Gruppe {group_id} wurde nicht gefunden.")


@contextmanager
def _group_errors(session: Session, group_id: int) -> Iterator[None]:
    """Uebersetzt Service-Fehler einer Gruppe in HTTP-Fehler (F-CON-02)."""

    try:
        yield
    except GroupNotFound as exc:
        raise NotFoundError(f"Gruppe {group_id} wurde nicht gefunden.") from exc
    except GroupVersionConflict as exc:
        raise ConflictError(current=_find_group(session, group_id)) from exc
    except AccountValidationError as exc:
        raise DomainError(str(exc)) from exc


def _page(items: list[GroupView], limit: int, cursor: str | None) -> tuple[list[GroupView], str | None]:
    """Schneidet eine Seite heraus und liefert den naechsten Cursor (F-MCP-11)."""

    offset = decode_cursor(cursor) if cursor else 0
    window = items[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(items) else None
    return window, next_cursor


# ------------------------------------------------------------------- Routen


@router.get("/groups", response_model=GroupListResponse)
def list_groups_route(
    request: Request,
    limit: int | None = None,
    cursor: str | None = None,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> GroupListResponse:
    """Listet Gruppen mit Mitgliedern, Kontenzahl und Summe (F-KTO-03)."""

    params = resolve_page_params(request, services.settings)
    views = [_group_dto(summary) for summary in list_groups(session)]
    page_items, next_cursor = _page(views, params.limit, params.cursor)
    return GroupListResponse(items=page_items, next_cursor=next_cursor)


@router.post("/groups", response_model=GroupView, status_code=status.HTTP_201_CREATED)
def create_group_route(
    payload: GroupCreateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> GroupView:
    """Legt eine Gruppe an (F-KTO-03)."""

    try:
        group = create_group(session, name=payload.name, sort_order=payload.sort_order)
    except AccountValidationError as exc:
        raise DomainError(str(exc)) from exc
    event_bus.emit("group.changed", group=group.id)
    return _find_group(session, group.id)


@router.put("/groups/order", response_model=list[GroupView])
def reorder_groups_route(
    payload: GroupOrderRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> list[GroupView]:
    """Sortiert Gruppen nach einer ID-Liste (Setz-Semantik, F-KTO-03)."""

    try:
        reorder_groups(session, payload.ids)
    except GroupNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except AccountValidationError as exc:
        raise DomainError(str(exc)) from exc
    event_bus.emit("group.changed")
    return [_group_dto(summary) for summary in list_groups(session)]


@router.patch("/groups/{group_id}", response_model=GroupView)
def rename_group_route(
    group_id: int,
    payload: GroupUpdateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> GroupView:
    """Benennt eine Gruppe um mit Versionspruefung und 409 (F-KTO-03, F-CON-02)."""

    with _group_errors(session, group_id):
        rename_group(session, group_id, expected_version=payload.version, name=payload.name)
    event_bus.emit("group.changed", group=group_id)
    return _find_group(session, group_id)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_route(
    group_id: int,
    version: int,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> Response:
    """Loescht eine Gruppe; die Konten bleiben mit ``group_id`` NULL (F-KTO-03)."""

    with _group_errors(session, group_id):
        delete_group(session, group_id, expected_version=version)
    event_bus.emit("group.changed", group=group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
