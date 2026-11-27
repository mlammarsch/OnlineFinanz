"""REST-Router fuer die Abstimmungsuebersicht (F-ABS-03).

Liefert je Konto und je Gruppe den abgestimmten Saldo, Anzahl und Summe der
nicht abgestimmten Buchungen und die Differenz zum Banksaldo. Die Fachlogik
liegt in :mod:`finue.services.reconciliation`; dieser Router uebersetzt nur
HTTP. Der Router bringt ``prefix="/api"`` und die Auth-Naht ``require_user``
selbst mit (T-088-Konvention).

Referenzierte Anforderungen: F-ABS-03, F-CON-02 (einheitliches Fehlerformat).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from finue.api.deps import get_session, require_user
from finue.api.errors import NotFoundError
from finue.services.accounts import AccountNotFound, GroupNotFound
from finue.services.reconciliation import (
    AccountReconciliation,
    CurrencyReconciliation,
    GroupReconciliation,
    account_reconciliation,
    group_reconciliation,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["reconciliation"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class ReconciliationTotals(BaseModel):
    """Abstimmungsaggregat einer Waehrung (F-ABS-03)."""

    currency: str
    opening_balance_cents: int
    reconciled_balance_cents: int
    unreconciled_count: int
    unreconciled_sum_cents: int
    balance_cents: int | None
    difference_cents: int | None


class AccountReconciliationView(ReconciliationTotals):
    """Abstimmungsuebersicht eines Kontos (F-ABS-03)."""

    account_id: int
    opening_balance_derived: bool


class GroupReconciliationView(ReconciliationTotals):
    """Abstimmungsuebersicht einer Gruppe mit getrennten Fremdwaehrungen (F-ABS-03)."""

    group_id: int
    member_count: int
    foreign: list[ReconciliationTotals]


# ---------------------------------------------------------------- Uebersetzung


def _totals_dto(totals: CurrencyReconciliation) -> ReconciliationTotals:
    """Uebersetzt ein Waehrungsaggregat in das API-DTO."""

    return ReconciliationTotals(
        currency=totals.currency,
        opening_balance_cents=totals.opening_balance_cents,
        reconciled_balance_cents=totals.reconciled_balance_cents,
        unreconciled_count=totals.unreconciled_count,
        unreconciled_sum_cents=totals.unreconciled_sum_cents,
        balance_cents=totals.balance_cents,
        difference_cents=totals.difference_cents,
    )


def _account_dto(result: AccountReconciliation) -> AccountReconciliationView:
    """Baut die Kontouehersicht aus dem Service-Ergebnis (F-ABS-03)."""

    return AccountReconciliationView(
        **_totals_dto(result.totals).model_dump(),
        account_id=result.account_id,
        opening_balance_derived=result.opening_balance_derived,
    )


def _group_dto(result: GroupReconciliation) -> GroupReconciliationView:
    """Baut die Gruppenuebersicht aus dem Service-Ergebnis (F-ABS-03)."""

    return GroupReconciliationView(
        **_totals_dto(result.totals).model_dump(),
        group_id=result.group_id,
        member_count=result.member_count,
        foreign=[_totals_dto(bucket) for bucket in result.foreign],
    )


# ------------------------------------------------------------------- Routen


@router.get("/accounts/{account_id}/reconciliation", response_model=AccountReconciliationView)
def get_account_reconciliation(
    account_id: int,
    session: Session = Depends(get_session),
) -> AccountReconciliationView:
    """Abstimmungsuebersicht eines Kontos (F-ABS-03)."""

    try:
        result = account_reconciliation(session, account_id)
    except AccountNotFound as exc:
        raise NotFoundError(f"Konto {account_id} wurde nicht gefunden.") from exc
    return _account_dto(result)


@router.get("/groups/{group_id}/reconciliation", response_model=GroupReconciliationView)
def get_group_reconciliation(
    group_id: int,
    session: Session = Depends(get_session),
) -> GroupReconciliationView:
    """Abstimmungsuebersicht einer Gruppe mit getrennten Fremdwaehrungen (F-ABS-03)."""

    try:
        result = group_reconciliation(session, group_id)
    except GroupNotFound as exc:
        raise NotFoundError(f"Gruppe {group_id} wurde nicht gefunden.") from exc
    return _group_dto(result)
