"""Modelle für Banken und Bankzugänge.

Implementiert N-DB-01 (kein rohes SQL für Daten), N-SEC-01 (PINs nur
verschlüsselt, kein Klartext-PIN-Feld) und F-CON-01 (Versionsspalte für
optimistische Sperre).
"""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, LargeBinary, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, VersionedMixin

if TYPE_CHECKING:
    from finue.models.account import Account


class Bank(Base):
    """Bankverbindung mit Basisdaten und FinTS-Konfiguration.

    Implementiert N-DB-01 (kein rohes SQL).
    """

    __tablename__ = "bank"

    id: Mapped[int] = mapped_column(primary_key=True)
    blz: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fints_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Bidirektionale Relation zu BankAccess
    accesses: Mapped[list["BankAccess"]] = relationship(
        "BankAccess",
        back_populates="bank",
        cascade="all, delete-orphan",
    )


class BankAccess(Base, VersionedMixin, TimestampMixin):
    """Zugangsdaten zu einem Bankzugang.

    Implementiert N-DB-01 (kein rohes SQL), N-SEC-01 (PIN nur verschlüsselt,
    kein Klartext-PIN-Feld pin oder pin_plain), F-SYN-08 (PIN verschlüsselt
    speichern oder bei jedem Sync abfragen) und F-CON-01 (Versionsspalte).

    Die Verschlüsselung der PIN wird später implementiert; das Feld
    pin_encrypted ist vom Typ LargeBinary und nullable, damit Testdaten
    ohne Verschlüsselungsschlüssel erzeugt werden können.

    `store_pin` steuert die Option aus F-SYN-08: Bei ``True`` bleibt die PIN
    verschlüsselt gespeichert, bei ``False`` wird sie bei jedem Sync erneut
    abgefragt. Der Datenbankvorgabewert ``true`` sorgt dafür, dass vorhandene
    Zugänge beim Upgrade auf Migration 0002 die Option aktiviert bekommen.
    """

    __tablename__ = "bank_access"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_id: Mapped[int] = mapped_column(
        ForeignKey("bank.id"),
        nullable=False,
    )
    login_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pin_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    tan_method: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # F-SYN-08: PIN verschlüsselt speichern (True) oder je Sync abfragen (False).
    # server_default erhält Bestandsdaten beim Upgrade (Migration 0002).
    store_pin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )

    # Bidirektionale Relation zu Bank
    bank: Mapped[Bank] = relationship(
        "Bank",
        back_populates="accesses",
        foreign_keys=[bank_id],
    )

    # Bidirektionale Relation zu Account
    # passive_deletes=True verhindert, dass SQLAlchemy den FK auf NULL setzt
    # und lässt stattdessen die Datenbank-Constraint (RESTRICT) durchsetzen
    accounts: Mapped[list["Account"]] = relationship(
        "Account",
        back_populates="bank_access",
        passive_deletes=True,
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da account.py und transaction.py nur in TYPE_CHECKING diese Module importieren.
from finue.models import account  # noqa: E402,F401
from finue.models import transaction as _transaction  # noqa: E402,F401
