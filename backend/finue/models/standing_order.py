"""Modell für Termin- und Daueraufträge.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-AUF-02 (Anzeige von
Empfänger, IBAN, Verwendungszweck, Betrag, Art, Intervall und nächster
Ausführung), F-AUF-04 (Abruf beim Sync, jeder Abruf ersetzt den
Auftragsbestand des Kontos) und F-CON-01 (Versionsspalte für optimistische
Sperre).

Die Abbildung ist strikt lesend: Der Sync spiegelt den von der Bank
gelieferten Auftragsbestand je Konto und ersetzt ihn vollständig. Es werden
nie schreibende FinTS-Geschäftsvorfälle aufgerufen und Aufträge nie geändert
oder gelöscht (siehe F-AUF-03). `last_seen_at` hält den Zeitstempel des
letzten erfolgreichen Abrufs fest (F-AUF-04).
"""

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, UtcDateTime, VersionedMixin
from finue.timeutil import utc_now

if TYPE_CHECKING:
    from finue.models.account import Account


class StandingOrder(Base, VersionedMixin, TimestampMixin):
    """Aktiver Termin- oder Dauerauftrag eines Bankkontos (nur lesend).

    Implementiert N-DB-01 (kein rohes SQL), F-AUF-02 (Anzeigefelder),
    F-AUF-04 (Auftragsbestand je Abruf, Stand mit Zeitstempel) und F-CON-01
    (Versionsspalte für optimistische Sperre).

    `kind` unterscheidet Dauerauftrag ('dauerauftrag', wiederkehrend über
    `interval`) und Terminauftrag ('terminauftrag', einmalige Ausführung).
    Die Zeilen werden ausschließlich vom Sync erzeugt und ersetzt; dieses
    Modell bildet die Daten nur ab und ändert nie etwas bei der Bank.
    """

    __tablename__ = "standing_order"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Zugehöriges Konto (CASCADE entfernt Aufträge beim Löschen des Kontos)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Art des Auftrags: dauerauftrag oder terminauftrag (F-AUF-02)
    kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    # Empfänger, IBAN und Verwendungszweck (F-AUF-02, können fehlen)
    counterparty: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Betrag in ganzen Cent plus Währung (N-DB-02)
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # Intervall des Dauerauftrags (z. B. 'monatlich'); NULL beim Terminauftrag
    interval: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Nächste Ausführung (F-AUF-02)
    next_execution: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Zeitstempel des letzten erfolgreichen Abrufs (F-AUF-04)
    last_seen_at: Mapped[UtcDateTime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
    )

    # Constraints zur Validierung
    __table_args__ = (
        CheckConstraint(
            "kind IN ('dauerauftrag', 'terminauftrag')",
            name="ck_standing_order_kind",
        ),
        # Auftragsbestand wird je Konto nach nächster Ausführung sortiert (F-AUF-02)
        Index("ix_standing_order_account_next", "account_id", "next_execution"),
    )

    # Viewonly-Relation: nur lesende Abbildung, kein Schreiben über die Beziehung
    account: Mapped["Account"] = relationship(
        "Account",
        foreign_keys=[account_id],
        viewonly=True,
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da account.py nur in TYPE_CHECKING StandingOrder importiert (falls überhaupt).
# Der Import stellt sicher, dass configure_mappers() allein mit
# finue.models.standing_order erfolgreich ist.
from finue.models import account  # noqa: E402,F401
