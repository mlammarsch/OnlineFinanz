"""Modelle für Konten und Kontengruppen.

Implementiert N-DB-01 (kein rohes SQL für Daten), N-DB-02 (Beträge in Cent,
Zeitstempel in UTC), F-KTO-01 (Konten mit oder ohne Gruppe), F-KTO-02
(include_in_total), F-KTO-03 (Gruppe löschen setzt group_id auf NULL),
F-KTO-06 (Archivieren über active-Flag) und F-CON-01 (Versionsspalte für
optimistische Sperre).
"""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, VersionedMixin, UtcDateTime

if TYPE_CHECKING:
    from finue.models.bank import BankAccess
    from finue.models.transaction import Transaction  # noqa: F401


class AccountGroup(Base, VersionedMixin, TimestampMixin):
    """Gruppe zur Kategorisierung von Konten.

    Implementiert N-DB-01 (kein rohes SQL), F-CON-01 (Versionsspalte).
    """

    __tablename__ = "account_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Bidirektionale Relation zu Account
    # passive_deletes=True lässt die Datenbank-Constraint (SET NULL) durchsetzen
    accounts: Mapped[list["Account"]] = relationship(
        "Account",
        back_populates="group",
        passive_deletes=True,
    )


class Account(Base, VersionedMixin, TimestampMixin):
    """Bankkonto mit Metadaten, Saldo und optionaler Gruppenzugehörigkeit.

    Implementiert N-DB-01 (kein rohes SQL), N-DB-02 (Beträge in Cent,
    Zeitstempel in UTC), F-KTO-01 (Konto-Basisdaten), F-KTO-02
    (include_in_total), F-KTO-03 (Gruppe optional, Löschen setzt auf NULL),
    F-KTO-06 (Archivierung) und F-CON-01 (Versionsspalte).

    Icons werden in der Datenbank gespeichert (N-OPS-02).
    """

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    source: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="fints",
    )
    account_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    include_in_total: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    bank_access_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_access.id", ondelete="RESTRICT"),
        nullable=True,
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("account_group.id", ondelete="SET NULL"),
        nullable=True,
    )
    opening_balance_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    balance_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)
    icon: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    icon_mime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Constraint zur Validierung des source-Feldes
    __table_args__ = (CheckConstraint("source IN ('fints', 'datei')", name="ck_account_source"),)

    # Bidirektionale Relation zu AccountGroup
    group: Mapped[AccountGroup | None] = relationship(
        "AccountGroup",
        back_populates="accounts",
        foreign_keys=[group_id],
    )

    # Bidirektionale Relation zu BankAccess
    bank_access: Mapped["BankAccess | None"] = relationship(
        "BankAccess",
        back_populates="accounts",
        foreign_keys=[bank_access_id],
    )

    # Bidirektionale Relation zu Transaction
    # passive_deletes=True lässt die Datenbank-Constraint (CASCADE) durchsetzen
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        back_populates="account",
        passive_deletes=True,
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da transaction.py und bank.py nur in TYPE_CHECKING diese Module importieren.
from finue.models import transaction  # noqa: E402,F401
from finue.models import bank as _bank  # noqa: E402,F401
