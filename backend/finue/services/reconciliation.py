"""Abstimmungsuebersicht je Konto und Gruppe (F-ABS-03).

Bündelt die Fachlogik der Abstimmungsübersicht, damit REST (M4) und MCP (M9)
dieselbe Schicht nutzen (D10) und die API-Router nur noch HTTP uebersetzen.

Kernregeln (F-ABS-03, Herleitung in ``projects/finanzuebersicht/decisions.md``):

- **Abgestimmter Saldo** = massgeblicher Anfangssaldo plus Summe der
  abgestimmten, gebuchten und nicht zusammengefuehrten Buchungen.
- **Nicht abgestimmt**: Anzahl und Summe der gebuchten, nicht
  zusammengefuehrten, nicht abgestimmten Buchungen.
- **Vorgemerkte Buchungen** (``status='vorgemerkt'``) zaehlen nicht mit
  (F-ABS-03); sie lassen sich nicht abstimmen (F-ABS-01).
- **Zusammengefuehrte Buchungen** (``merged=True``, F-DUP-04) sind nur eine
  Markierung einer Dublette und zaehlen weder als abgestimmt noch als nicht
  abgestimmt.
- **Differenz** = Banksaldo minus abgestimmter Saldo; fehlt der Banksaldo
  (``balance_cents IS NULL``), ist die Differenz ``None``.
- **Gemischte Waehrung** in der Gruppe: Es wird nur in der Buchwaehrung
  (Standard EUR) addiert; andere Waehrungen werden je Waehrung **getrennt**
  ausgewiesen und nicht addiert (Regel wie in :mod:`finue.services.accounts`,
  T-088).

Die Herleitung des Anfangssaldos ist in ``decisions.md`` festgehalten:
Datei-Konten fuehren den Ankersaldo in ``opening_balance_cents``. FinTS-Konten
haben dort nie einen Wert (0); fuer sie wird der Anfangssaldo aus dem Banksaldo
abgeleitet (``balance_cents`` minus Summe der gebuchten, nicht
zusammengefuehrten Buchungen), sofern ein Banksaldo vorliegt.

Referenzierte Anforderungen: F-ABS-01, F-ABS-03, N-DB-01 (kein rohes Daten-SQL),
N-DB-02 (Betraege in ganzen Cent).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.accounts import (
    DEFAULT_CURRENCY,
    AccountNotFound,
    GroupNotFound,
    list_accounts,
)

__all__ = [
    "AccountReconciliation",
    "CurrencyReconciliation",
    "GroupReconciliation",
    "account_reconciliation",
    "group_reconciliation",
]

#: Status einer gebuchten Buchung; ``vorgemerkt`` zaehlt nicht (F-ABS-03).
BOOKED_STATUS: Final = "gebucht"

#: Konten dieser Herkunft fuehren keinen Ankersaldo; der Anfangssaldo wird
#: aus dem Banksaldo abgeleitet (Regel in ``decisions.md``).
FINTS_SOURCE: Final = "fints"


@dataclass(frozen=True, slots=True)
class CurrencyReconciliation:
    """Abstimmungsaggregat einer einzelnen Waehrung (F-ABS-03)."""

    currency: str
    opening_balance_cents: int
    reconciled_balance_cents: int
    unreconciled_count: int
    unreconciled_sum_cents: int
    balance_cents: int | None
    difference_cents: int | None


@dataclass(frozen=True, slots=True)
class AccountReconciliation:
    """Abstimmungsuebersicht eines Kontos (F-ABS-03)."""

    account_id: int
    opening_balance_derived: bool
    totals: CurrencyReconciliation


@dataclass(frozen=True, slots=True)
class GroupReconciliation:
    """Abstimmungsuebersicht einer Gruppe (F-ABS-03).

    ``totals`` fasst die Mitglieder in der Buchwaehrung zusammen; ``foreign``
    weist je anderer Waehrung ein eigenes Aggregat aus, das nicht addiert wird
    (Regel in ``decisions.md``).
    """

    group_id: int
    member_count: int
    totals: CurrencyReconciliation
    foreign: tuple[CurrencyReconciliation, ...]


@dataclass(frozen=True, slots=True)
class _Aggregate:
    """Rohe Summen einer Kontos aus einer einzigen Aggregatabfrage."""

    reconciled_sum_cents: int
    unreconciled_sum_cents: int
    unreconciled_count: int
    booked_sum_cents: int


def _aggregate(session: Session, account_id: int) -> _Aggregate:
    """Summiert die gebuchten, nicht zusammengefuehrten Buchungen eines Kontos.

    Eine einzige Aggregatabfrage in der Datenbank (keine unbegrenzte
    Zeilenuebertragung, N-DB-01); vorgemerkte und zusammengefuehrte Buchungen
    bleiben ausgeschlossen (F-ABS-03).
    """

    reconciled = func.coalesce(
        func.sum(case((Transaction.reconciled.is_(True), Transaction.amount_cents), else_=0)),
        0,
    )
    unreconciled_sum = func.coalesce(
        func.sum(case((Transaction.reconciled.is_(False), Transaction.amount_cents), else_=0)),
        0,
    )
    unreconciled_count = func.coalesce(
        func.sum(case((Transaction.reconciled.is_(False), 1), else_=0)),
        0,
    )
    booked_sum = func.coalesce(func.sum(Transaction.amount_cents), 0)
    stmt = select(reconciled, unreconciled_sum, unreconciled_count, booked_sum).where(
        Transaction.account_id == account_id,
        Transaction.status == BOOKED_STATUS,
        Transaction.merged.is_(False),
    )
    row = session.execute(stmt).one()
    return _Aggregate(
        reconciled_sum_cents=int(row[0] or 0),
        unreconciled_sum_cents=int(row[1] or 0),
        unreconciled_count=int(row[2] or 0),
        booked_sum_cents=int(row[3] or 0),
    )


def _effective_opening(account: Account, booked_sum_cents: int) -> tuple[int, bool]:
    """Liefert den massgeblichen Anfangssaldo und ob er abgeleitet wurde.

    Datei-Konten nutzen den gespeicherten Ankersaldo. FinTS-Konten ohne
    gesetzten Ankersaldo (0) leiten ihn aus dem Banksaldo ab, sofern ein
    Banksaldo vorliegt (Regel in ``decisions.md``).
    """

    if account.source == FINTS_SOURCE and account.opening_balance_cents == 0 and account.balance_cents is not None:
        return account.balance_cents - booked_sum_cents, True
    return account.opening_balance_cents, False


def _build_account(account: Account, aggregate: _Aggregate) -> AccountReconciliation:
    """Baut die Kontouehersicht aus Konto und Rohtsummen (F-ABS-03)."""

    opening, derived = _effective_opening(account, aggregate.booked_sum_cents)
    reconciled_balance = opening + aggregate.reconciled_sum_cents
    balance = account.balance_cents
    difference = None if balance is None else balance - reconciled_balance
    totals = CurrencyReconciliation(
        currency=account.currency,
        opening_balance_cents=opening,
        reconciled_balance_cents=reconciled_balance,
        unreconciled_count=aggregate.unreconciled_count,
        unreconciled_sum_cents=aggregate.unreconciled_sum_cents,
        balance_cents=balance,
        difference_cents=difference,
    )
    return AccountReconciliation(account_id=account.id, opening_balance_derived=derived, totals=totals)


def account_reconciliation(session: Session, account_id: int) -> AccountReconciliation:
    """Liefert die Abstimmungsuebersicht eines Kontos (F-ABS-03).

    Auch fuer archivierte Konten abrufbar (kein Archivfilter).

    Raises:
        AccountNotFound: Das Konto existiert nicht.
    """

    account = session.get(Account, account_id)
    if account is None:
        raise AccountNotFound(f"Konto {account_id} nicht gefunden.")
    return _build_account(account, _aggregate(session, account_id))


def _combine(items: list[CurrencyReconciliation], currency: str) -> CurrencyReconciliation:
    """Fasst die Aggregate der Mitglieder einer Waehrung zusammen (F-ABS-03).

    Der Banksaldo wird nur summiert, wenn ihn **jedes** Mitglied fuehrt; sonst
    bleibt er ``None`` und damit auch die Differenz.
    """

    opening = sum(item.opening_balance_cents for item in items)
    reconciled = sum(item.reconciled_balance_cents for item in items)
    count = sum(item.unreconciled_count for item in items)
    unreconciled_sum = sum(item.unreconciled_sum_cents for item in items)

    balances = [item.balance_cents for item in items]
    if all(balance is not None for balance in balances):
        balance = sum(balance for balance in balances if balance is not None)
        difference: int | None = balance - reconciled
    else:
        balance = None
        difference = None

    return CurrencyReconciliation(
        currency=currency,
        opening_balance_cents=opening,
        reconciled_balance_cents=reconciled,
        unreconciled_count=count,
        unreconciled_sum_cents=unreconciled_sum,
        balance_cents=balance,
        difference_cents=difference,
    )


def group_reconciliation(
    session: Session,
    group_id: int,
    *,
    default_currency: str = DEFAULT_CURRENCY,
) -> GroupReconciliation:
    """Liefert die Abstimmungsuebersicht einer Gruppe (F-ABS-03).

    Es zaehlen die aktiven Mitglieder (wie die Gruppensumme, F-KTO-03). Die
    Summe fuehrt nur die Buchwaehrung; andere Waehrungen werden je Waehrung
    getrennt ausgewiesen und nicht addiert (Regel in ``decisions.md``).

    Raises:
        GroupNotFound: Die Gruppe existiert nicht.
    """

    group = session.get(AccountGroup, group_id)
    if group is None:
        raise GroupNotFound(f"Gruppe {group_id} nicht gefunden.")

    members = [account for account in list_accounts(session, archived=False) if account.group_id == group_id]

    buckets: dict[str, list[CurrencyReconciliation]] = {}
    for member in members:
        view = _build_account(member, _aggregate(session, member.id))
        buckets.setdefault(member.currency, []).append(view.totals)

    book_currency_items = buckets.pop(default_currency, [])
    totals = _combine(book_currency_items, default_currency)
    foreign = tuple(_combine(items, currency) for currency, items in sorted(buckets.items()))
    return GroupReconciliation(
        group_id=group_id,
        member_count=len(members),
        totals=totals,
        foreign=foreign,
    )
