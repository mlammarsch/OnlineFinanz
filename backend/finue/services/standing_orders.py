"""Auftragsbestand je Konto ersetzen und Anzeigezustand liefern.

Der Service setzt die Persistenzseite der Auftragsanzeige um:

- F-AUF-04: :func:`replace_standing_orders` ersetzt den Auftragsbestand eines
  Kontos vollstaendig. Jeder Abruf schreibt genau den Stand, den die Bank
  geliefert hat; ``last_seen_at`` haelt den Zeitstempel des letzten
  erfolgreichen Abrufs fest.
- F-AUF-05: :func:`get_orders_state` liefert ``available`` mit Zeitstempel,
  wenn Auftraege vorliegen, sonst ``unavailable`` mit dem festen Klartext
  "Keine Auftragsdaten von der Bank verfuegbar" und ohne Zeilen. Datei-Konten
  erhalten keinen Bereich und liefern ``None``.
- F-AUF-03: Es gibt ausschliesslich lesende Abbildung und Ersetzung. Auftraege
  werden bei der Bank nie angelegt, geaendert, ausgesetzt oder geloescht.
- F-CON-01: Die vorhandenen Zeilen werden als ORM-Objekte geladen und ueber
  ``Session.delete()`` entfernt. Damit wirkt die Versionsspalte auch beim
  Loeschen; ein zwischenzeitlich geaenderter Datensatz fuehrt zu
  ``sqlalchemy.orm.exc.StaleDataError`` statt zu stillem Datenverlust.
- N-DB-01: Ausschliesslich SQLAlchemy-Ausdruecke, kein rohes SQL.
- N-DB-02: Betraege bleiben ganze Cent, Zeitstempel sind aware UTC.
- N-SEC-02: Meldungen enthalten nur festen Klartext, keine Kontodaten.

Es finden keine Bankzugriffe statt. Der Aufrufer uebergibt die bereits lesend
abgerufenen Auftraege als
:class:`~finue.services.fints_types.FetchedStandingOrder`. Die Funktion
committet nicht; der Sync-Aufrufer speichert sie in derselben Transaktion wie
Saldo und Umsaetze (:func:`finue.services.sync_persist.persist_account_sync`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.standing_order import StandingOrder
from finue.services.fints_types import FetchedStandingOrder

__all__ = [
    "ORDERS_UNAVAILABLE_MESSAGE",
    "OrdersState",
    "OrdersStateStatus",
    "get_orders_state",
    "replace_standing_orders",
]

#: Fester Anzeigetext ohne Auftragsdaten (F-AUF-05).
ORDERS_UNAVAILABLE_MESSAGE: Final[str] = (
    "Keine Auftragsdaten von der Bank verfuegbar"
)

#: Anzeigezustand des Auftragsbereichs eines FinTS-Kontos (F-AUF-05).
OrdersStateStatus = Literal["available", "unavailable"]

#: Kontoquelle, fuer die der Auftragsbereich entfaellt (F-AUF-05).
_FINTS_SOURCE: Final[str] = "fints"


@dataclass(frozen=True, slots=True)
class OrdersState:
    """Anzeigezustand des Auftragsbereichs (F-AUF-04, F-AUF-05).

    Attributes:
        status: ``available`` mit mindestens einer Zeile oder ``unavailable``
            ohne Zeilen.
        fetched_at: Zeitstempel des letzten erfolgreichen Abrufs; nur bei
            ``available`` gesetzt.
        orders: Gespeicherte Auftraege in Anzeigereihenfolge; bei
            ``unavailable`` leer.
        message: Fester Klartext bei ``unavailable``, sonst ``None``.
    """

    status: OrdersStateStatus
    fetched_at: datetime | None = None
    orders: tuple[StandingOrder, ...] = ()
    message: str | None = None


def replace_standing_orders(
    session: Session,
    account_id: int,
    orders: Sequence[FetchedStandingOrder],
    fetched_at: datetime,
) -> tuple[StandingOrder, ...]:
    """Ersetzt den Auftragsbestand eines Kontos vollstaendig (F-AUF-04).

    Alle vorhandenen Auftraege des Kontos werden geloescht und die neu
    gelieferten angelegt. Eine leere Sequenz loescht den Bestand; das ist der
    regulaere Fall "Bank liefert keine Auftraege" (F-AUF-05). Die Funktion
    committet nicht und ruft ``flush()`` auf, damit die Constraints sofort
    greifen.

    Args:
        session: SQLAlchemy-Session des laufenden Kontos.
        account_id: Konto, dessen Auftragsbestand ersetzt wird.
        orders: Lesend abgerufene Auftraege. Die Reihenfolge bleibt erhalten.
        fetched_at: Zeitstempel des Abrufs als aware ``datetime``. Er wird als
            ``last_seen_at`` jeder neuen Zeile gespeichert.

    Returns:
        Die neu angelegten, bereits der Session hinzugefuegten Auftraege.

    Raises:
        LookupError: Wenn das Konto nicht existiert.
        ValueError: Wenn ``fetched_at`` keine Zeitzone traegt.
        sqlalchemy.orm.exc.StaleDataError: Wenn eine vorhandene Zeile
            zwischenzeitlich geaendert wurde (F-CON-01).
    """

    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Konto {account_id} nicht gefunden.")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at muss zeitzonenbehaftet (aware) sein")

    existing = session.scalars(
        select(StandingOrder).where(StandingOrder.account_id == account_id)
    ).all()
    for row in existing:
        session.delete(row)
    session.flush()

    inserted = tuple(
        _from_fetched(account_id, order, fetched_at) for order in orders
    )
    session.add_all(inserted)
    session.flush()
    return inserted


def get_orders_state(
    session: Session,
    account_id: int,
) -> OrdersState | None:
    """Liefert den Anzeigezustand des Auftragsbereichs (F-AUF-04, F-AUF-05).

    FinTS-Konten mit gespeicherten Auftraegen liefern ``available`` samt
    Zeitstempel des letzten Abrufs. Ohne Zeilen liefert der Bereich
    ``unavailable`` mit festem Klartext und ohne Auftraege. Datei-Konten haben
    keinen Auftragsbereich und liefern ``None``.

    Args:
        session: SQLAlchemy-Session (nur lesend).
        account_id: Konto, dessen Bereichszustand gelesen wird.

    Returns:
        :class:`OrdersState` fuer FinTS-Konten oder ``None`` fuer Datei-Konten.

    Raises:
        LookupError: Wenn das Konto nicht existiert.
    """

    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Konto {account_id} nicht gefunden.")
    if account.source != _FINTS_SOURCE:
        return None

    orders = tuple(
        session.scalars(
            select(StandingOrder)
            .where(StandingOrder.account_id == account_id)
            .order_by(StandingOrder.next_execution, StandingOrder.id)
        ).all()
    )
    if not orders:
        return OrdersState(
            status="unavailable",
            message=ORDERS_UNAVAILABLE_MESSAGE,
        )

    # Das Modell annotiert ``last_seen_at`` mit dem TypeDecorator; zur
    # Laufzeit liefert er ein aware ``datetime`` (N-DB-02).
    fetched_at = max(cast(datetime, order.last_seen_at) for order in orders)
    return OrdersState(
        status="available",
        fetched_at=fetched_at,
        orders=orders,
    )


def _from_fetched(
    account_id: int,
    order: FetchedStandingOrder,
    fetched_at: datetime,
) -> StandingOrder:
    """Bildet einen abgerufenen Auftrag auf das Persistenzmodell ab."""

    return StandingOrder(
        account_id=account_id,
        kind=order.kind,
        counterparty=order.counterparty,
        counterparty_iban=order.counterparty_iban,
        purpose=order.purpose,
        amount_cents=order.amount_cents,
        currency=order.currency,
        interval=order.interval,
        next_execution=order.next_execution,
        last_seen_at=fetched_at,
    )
