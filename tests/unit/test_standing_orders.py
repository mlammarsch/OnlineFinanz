"""Tests fuer den Auftragsbestand je Konto (F-AUF-04, F-AUF-05).

Abgedeckt:

- F-AUF-04: Ein Abruf ersetzt den Auftragsbestand des Kontos vollstaendig;
  ``last_seen_at`` haelt den Zeitstempel des letzten Abrufs fest.
- F-AUF-05: Ohne Auftraege liefert der Bereich ``unavailable`` mit festem
  Klartext und ohne Zeilen; Datei-Konten haben keinen Bereich.
- F-AUF-03: Die Persistenz bildet nur ab und schreibt nie zurueck zur Bank.
- F-CON-01: Vorhandene Zeilen werden als ORM-Objekte geloescht, damit die
  Versionsspalte wirkt.
- N-DB-01/N-DB-02: SQLAlchemy-Ausdruecke, ganze Cent und aware UTC.

Es werden ausschliesslich erfundene Testdaten und keine echten Bankzugaenge
verwendet (D8).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models import Account, StandingOrder
from finue.services.fints_types import FetchedStandingOrder
from finue.services.standing_orders import (
    ORDERS_UNAVAILABLE_MESSAGE,
    get_orders_state,
    replace_standing_orders,
)

UTC = timezone.utc


@pytest.fixture()
def factory(tmp_path: Path) -> sessionmaker[Session]:
    """Datei-SQLite je Test mit allen registrierten Tabellen."""

    db_path = (tmp_path / "orders.db").as_posix()
    engine = create_db_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _account(factory: sessionmaker[Session], source: str = "fints") -> int:
    """Legt ein Konto an und liefert seine ID."""

    with factory() as session:
        account = Account(name="Erfundenes Konto", currency="EUR", source=source)
        session.add(account)
        session.commit()
        return account.id


def _dauerauftrag() -> FetchedStandingOrder:
    return FetchedStandingOrder(
        kind="dauerauftrag",
        counterparty="Erfundene Vermietung GmbH",
        counterparty_iban="DE02120300000000202051",
        purpose="Erfundene Miete",
        amount_cents=75000,
        currency="EUR",
        interval="monatlich",
        next_execution=date(2026, 12, 1),
        first_execution=date(2026, 1, 1),
        last_execution=None,
    )


def _terminauftrag() -> FetchedStandingOrder:
    return FetchedStandingOrder(
        kind="terminauftrag",
        counterparty="Erfundene Versicherung AG",
        counterparty_iban="DE00777700001111222233",
        purpose="Erfundene Jahrespraemie",
        amount_cents=12000,
        currency="EUR",
        interval=None,
        next_execution=date(2027, 1, 15),
        first_execution=date(2027, 1, 15),
        last_execution=date(2027, 1, 15),
    )


def _stored(factory: sessionmaker[Session], account_id: int) -> list[StandingOrder]:
    with factory() as session:
        return list(
            session.scalars(
                select(StandingOrder)
                .where(StandingOrder.account_id == account_id)
                .order_by(StandingOrder.id)
            ).all()
        )


def test_replace_replaces_existing_stock_f_auf_04(
    factory: sessionmaker[Session],
) -> None:
    """F-AUF-04: Ein Abruf ersetzt alte Auftraege vollstaendig."""

    account_id = _account(factory)
    first_seen = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    second_seen = datetime(2026, 11, 26, 17, 0, tzinfo=UTC)

    with factory() as session:
        old = replace_standing_orders(
            session, account_id, (_dauerauftrag(), _terminauftrag()), first_seen
        )
        session.commit()
        assert len(old) == 2

    with factory() as session:
        new = replace_standing_orders(
            session, account_id, (_terminauftrag(),), second_seen
        )
        session.commit()
        assert len(new) == 1

    rows = _stored(factory, account_id)
    assert len(rows) == 1
    assert rows[0].kind == "terminauftrag"
    assert rows[0].counterparty == "Erfundene Versicherung AG"
    assert rows[0].counterparty_iban == "DE00777700001111222233"
    assert rows[0].purpose == "Erfundene Jahrespraemie"
    assert rows[0].amount_cents == 12000
    assert rows[0].currency == "EUR"
    assert rows[0].interval is None
    assert rows[0].next_execution == date(2027, 1, 15)
    assert rows[0].last_seen_at == second_seen


def test_replace_maps_dauerauftrag_f_auf_02_f_auf_04(
    factory: sessionmaker[Session],
) -> None:
    """F-AUF-02/F-AUF-04: Dauerauftragsfelder und Abrufzeitpunkt werden gespeichert."""

    account_id = _account(factory)
    fetched_at = datetime(2026, 11, 26, 9, 30, tzinfo=UTC)

    with factory() as session:
        replace_standing_orders(session, account_id, (_dauerauftrag(),), fetched_at)
        session.commit()

    rows = _stored(factory, account_id)
    assert len(rows) == 1
    stored = rows[0]
    assert stored.kind == "dauerauftrag"
    assert stored.counterparty == "Erfundene Vermietung GmbH"
    assert stored.counterparty_iban == "DE02120300000000202051"
    assert stored.purpose == "Erfundene Miete"
    assert stored.amount_cents == 75000
    assert stored.currency == "EUR"
    assert stored.interval == "monatlich"
    assert stored.next_execution == date(2026, 12, 1)
    assert stored.last_seen_at == fetched_at


def test_replace_empty_deletes_stock_f_auf_04_f_auf_05(
    factory: sessionmaker[Session],
) -> None:
    """F-AUF-04/05: Eine leere Bankantwort loescht den Bestand."""

    account_id = _account(factory)
    with factory() as session:
        replace_standing_orders(
            session,
            account_id,
            (_dauerauftrag(), _terminauftrag()),
            datetime(2026, 11, 20, 8, 0, tzinfo=UTC),
        )
        session.commit()

    with factory() as session:
        assert replace_standing_orders(
            session,
            account_id,
            (),
            datetime(2026, 11, 26, 17, 0, tzinfo=UTC),
        ) == ()
        session.commit()

    assert _stored(factory, account_id) == []

    with factory() as session:
        state = get_orders_state(session, account_id)

    assert state is not None
    assert state.status == "unavailable"
    assert state.fetched_at is None
    assert state.orders == ()
    assert state.message == ORDERS_UNAVAILABLE_MESSAGE
    assert state.message == "Keine Auftragsdaten von der Bank verfuegbar"


def test_state_available_with_timestamp_and_rows_f_auf_04(
    factory: sessionmaker[Session],
) -> None:
    """F-AUF-04: available liefert Zeitstempel und die gespeicherten Zeilen."""

    account_id = _account(factory)
    fetched_at = datetime(2026, 11, 26, 10, 15, tzinfo=UTC)
    with factory() as session:
        replace_standing_orders(
            session,
            account_id,
            (_dauerauftrag(), _terminauftrag()),
            fetched_at,
        )
        session.commit()

    with factory() as session:
        state = get_orders_state(session, account_id)

    assert state is not None
    assert state.status == "available"
    assert state.fetched_at == fetched_at
    assert state.message is None
    assert [row.kind for row in state.orders] == ["dauerauftrag", "terminauftrag"]
    assert all(row.account_id == account_id for row in state.orders)


def test_state_unavailable_without_rows_f_auf_05(
    factory: sessionmaker[Session],
) -> None:
    """F-AUF-05: FinTS-Konto ohne Auftraege zeigt den festen Klartext."""

    account_id = _account(factory)

    with factory() as session:
        state = get_orders_state(session, account_id)

    assert state is not None
    assert state.status == "unavailable"
    assert state.fetched_at is None
    assert state.orders == ()
    assert state.message == ORDERS_UNAVAILABLE_MESSAGE


def test_state_none_for_file_account_f_auf_05(
    factory: sessionmaker[Session],
) -> None:
    """F-AUF-05: Datei-Konten haben keinen Auftragsbereich."""

    account_id = _account(factory, source="datei")

    with factory() as session:
        assert get_orders_state(session, account_id) is None


def test_replace_rejects_naive_timestamp_n_db_02(
    factory: sessionmaker[Session],
) -> None:
    """N-DB-02: Zeitstempel muessen aware UTC sein."""

    account_id = _account(factory)

    with factory() as session, pytest.raises(ValueError, match="zeitzonenbehaftet"):
        replace_standing_orders(
            session,
            account_id,
            (_dauerauftrag(),),
            datetime(2026, 11, 26, 10, 15),
        )


def test_unknown_account_raises_lookup_error(factory: sessionmaker[Session]) -> None:
    """Ein unbekanntes Konto liefert einen klaren Fehler."""

    with factory() as session:
        with pytest.raises(LookupError):
            replace_standing_orders(
                session, 9999, (), datetime(2026, 11, 26, 10, 15, tzinfo=UTC)
            )
        with pytest.raises(LookupError):
            get_orders_state(session, 9999)
