"""Tests für die ORM-Mixins.

Referenziert N-DB-02 (UTC-Zeitstempel, Beträge in Cent) und F-CON-01
(Versionsspalte, konkurrierende Änderung wirft StaleDataError).
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import String
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from finue.db.engine import create_db_engine, make_session_factory
from finue.db.mixins import AmountMixin, TimestampMixin, VersionedMixin


class _TestBase(DeclarativeBase):
    """Eigene Basis, damit das Testmodell die Projekt-Metadaten nicht berührt."""


class _Item(_TestBase, VersionedMixin, TimestampMixin, AmountMixin):
    """Lokales Testmodell."""

    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    engine = create_db_engine("sqlite://")
    _TestBase.metadata.create_all(engine)
    return make_session_factory(engine)


def _new_item(session: Session) -> _Item:
    item = _Item(name="a", amount_cents=1234)
    session.add(item)
    session.commit()
    return item


def test_version_starts_at_one_and_increments(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: Version startet bei 1 und steigt bei jedem Update."""
    with factory() as s:
        item = _new_item(s)
        assert item.version == 1
        item.name = "b"
        s.commit()
        assert item.version == 2


def test_concurrent_update_raises_stale_data(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: Zweite Sitzung mit altem Stand scheitert mit StaleDataError."""
    with factory() as s1:
        item_id = _new_item(s1).id
    with factory() as s1, factory() as s2:
        a = s1.get(_Item, item_id)
        b = s2.get(_Item, item_id)
        assert a is not None and b is not None
        a.name = "von-a"
        s1.commit()
        b.name = "von-b"
        with pytest.raises(StaleDataError):
            s2.commit()


def test_timestamps_are_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-02: Zeitstempel kommen als aware UTC zurück, updated_at wandert."""
    with factory() as s:
        item = _new_item(s)
        item_id = item.id
        created = item.created_at
    with factory() as s:
        loaded = s.get(_Item, item_id)
        assert loaded is not None
        assert loaded.created_at.utcoffset() == timedelta(0)
        assert loaded.created_at == created
        updated_before = loaded.updated_at
        time.sleep(0.01)
        loaded.name = "neu"
        s.commit()
        assert loaded.updated_at > updated_before


def test_non_utc_aware_value_is_normalized(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-02: Aware Werte in anderer Zone werden nach UTC umgerechnet."""
    cest = timezone(timedelta(hours=2))
    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=cest)
    with factory() as s:
        item = _Item(name="z", amount_cents=1, created_at=stamp)
        s.add(item)
        s.commit()
        item_id = item.id
    with factory() as s:
        loaded = s.get(_Item, item_id)
        assert loaded is not None
        assert loaded.created_at == stamp
        assert loaded.created_at.hour == 10


def test_naive_datetime_is_rejected(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-02: Naive Zeitstempel werden abgelehnt."""
    with factory() as s:
        s.add(_Item(name="n", amount_cents=1, created_at=datetime(2026, 1, 1)))
        with pytest.raises(StatementError, match="aware"):
            s.commit()


def test_amount_is_integer_cents_with_currency(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-02: Betrag als ganze Cent, Währung dreistellig, Standard EUR."""
    with factory() as s:
        item = _new_item(s)
        item_id = item.id
    with factory() as s:
        loaded = s.get(_Item, item_id)
        assert loaded is not None
        assert loaded.amount_cents == 1234
        assert isinstance(loaded.amount_cents, int)
        assert loaded.currency == "EUR"
