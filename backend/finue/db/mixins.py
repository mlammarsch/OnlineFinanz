"""Wiederverwendbare ORM-Mixins für Finanzübersicht.

Implementiert N-DB-02 (Zeitstempel in UTC, Beträge als ganze Cent) und F-CON-01
(optimistische Sperre über eine Versionsspalte, bei Konflikt ``StaleDataError``).
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator

from finue.timeutil import utc_now


class UtcDateTime(TypeDecorator[datetime]):
    """Zeitstempel, der immer als aware UTC aus der Datenbank kommt.

    SQLite speichert keine Zeitzone. Beim Schreiben wird deshalb nach UTC
    umgerechnet (naive Werte werden abgelehnt), beim Lesen wird UTC angehängt.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Rechnet vor dem Schreiben nach UTC um; naive Werte sind ein Fehler."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Zeitstempel muss zeitzonenbehaftet (aware) sein")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Hängt nach dem Lesen UTC an, falls die Datenbank keine Zone liefert."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class VersionedMixin:
    """Versionsspalte für optimistische Sperre (F-CON-01).

    Die Version startet bei 1 und steigt bei jedem Update um 1. Ein Update auf
    einem veralteten Stand wirft ``sqlalchemy.orm.exc.StaleDataError``.
    """

    version: Mapped[int] = mapped_column(Integer, nullable=False)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        """Aktiviert ``version_id_col`` für die erbende Klasse."""
        return {"version_id_col": cls.version}


class TimestampMixin:
    """Zeitstempel ``created_at`` und ``updated_at`` in aware UTC (N-DB-02)."""

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class AmountMixin:
    """Betragskonvention (N-DB-02): ganze Cent als Integer plus Währung ISO 4217.

    Keine Gleitkommazahlen für Geldbeträge.
    """

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
