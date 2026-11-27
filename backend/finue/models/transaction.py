"""Modelle für Buchungen und Transaktionen.

Implementiert N-DB-01 (kein rohes SQL für Daten), N-DB-02 (Beträge in Cent,
Zeitstempel in UTC), F-KTO-07 (Löschen des Kontos entfernt Buchungen),
F-SUC-06 (normalisierte Suchspalte), F-ABS-01 (abgestimmt-Flag), F-DUP-01
(Fingerprint für Dublettenkennung), F-DUP-02 (Vorkommen-Index) und F-CON-01
(Versionsspalte für optimistische Sperre).
"""

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import AmountMixin, TimestampMixin, UtcDateTime, VersionedMixin

if TYPE_CHECKING:
    from finue.models.account import Account


class Transaction(Base, VersionedMixin, TimestampMixin, AmountMixin):
    """Einzelne Buchung auf einem Bankkonto.

    Implementiert N-DB-01 (kein rohes SQL), N-DB-02 (Beträge in Cent,
    Zeitstempel in UTC), F-KTO-07 (Konto löschen entfernt Buchungen),
    F-SUC-06 (normalisierte Suchspalte), F-ABS-01 (abgestimmt-Flag mit
    Zeitstempel), F-DUP-01 (Fingerprint für Dublettenkennung), F-DUP-02
    (Vorkommen-Index), F-DUP-04 (Zusammenführung nur als Markierung) und
    F-CON-01 (Versionsspalte).

    Die search_text-Spalte wird für normalisierte Suche verwendet (F-SUC-06).
    Der Fingerprint ist ein SHA-256-Hex für Dublettenkennung (F-DUP-01).
    """

    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="gebucht",
    )
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    counterparty: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Abgestimmungs-Flags (F-ABS-01): checked für geprüft, reconciled für abgestimmt
    checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checked_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)
    # Herkunft der letzten Änderung des Kennzeichens (F-ABS-01). Wertebereich ist
    # identisch mit FlagOrigin aus services/export_schema.py (ui/mcp/import); NULL,
    # solange kein Kennzeichen gesetzt wurde.
    checked_origin: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reconciled_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)
    reconciled_origin: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Herkunft der Buchung: FinTS, CSV oder JSON (F-DUP-01)
    origin: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    # Externe Referenz (z.B. Datensatz-ID aus Import)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Fingerprint für Dublettenkennung (SHA-256 Hex, F-DUP-01)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # Vorkommen-Index für Duplikate (F-DUP-02)
    occurrence_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Markierung für zusammengeführte Buchungen (F-DUP-04)
    merged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Constraints zur Validierung
    __table_args__ = (
        CheckConstraint(
            "status IN ('gebucht', 'vorgemerkt')",
            name="ck_transaction_status",
        ),
        CheckConstraint(
            "origin IN ('fints', 'csv', 'json')",
            name="ck_transaction_origin",
        ),
        # F-ABS-01: Herkunft der Kennzeichen nur als NULL oder ui/mcp/import
        # (Wertebereich wie FlagOrigin in services/export_schema.py).
        CheckConstraint(
            "checked_origin IS NULL OR checked_origin IN ('ui', 'mcp', 'import')",
            name="ck_transaction_checked_origin",
        ),
        CheckConstraint(
            "reconciled_origin IS NULL OR reconciled_origin IN ('ui', 'mcp', 'import')",
            name="ck_transaction_reconciled_origin",
        ),
        # Eindeutigkeit: (account_id, fingerprint, occurrence_index)
        UniqueConstraint(
            "account_id",
            "fingerprint",
            "occurrence_index",
            name="uq_transaction_account_fingerprint_index",
        ),
        # Indizes für Suche und Duplicate-Detection
        Index("ix_transaction_account_booking", "account_id", "booking_date"),
        Index("ix_transaction_fingerprint", "fingerprint"),
        # Indizes für Liste/Suche (F-SUC-01, F-BUC-01); Feintuning in T-103.
        Index("ix_transaction_account_status_booking_id", "account_id", "status", "booking_date", "id"),
        Index("ix_transaction_booking_id", "booking_date", "id"),
        Index("ix_transaction_amount_cents", "amount_cents"),
        # Abdeckende Performance-Indizes aus T-103 (N-PERF-01, F-BUC-06), angelegt
        # in Migration 0005. Der Suchindex deckt Trefferzahl/-summe ohne
        # Zeilen-Nachschlag ab; der Listenindex deckt die Saldo-/Tagessummen der
        # Buchungsliste und Gruppenansicht ab. Begruendung: EXPLAIN QUERY PLAN im
        # Performance-Block (Suche ~480 ms -> ~200 ms, Gruppenansicht ~800 ms ->
        # ~70 ms bei 100.000 Buchungen).
        Index(
            "ix_transaction_account_booking_search",
            "account_id",
            "booking_date",
            "merged",
            "search_text",
            "amount_cents",
        ),
        Index(
            "ix_transaction_account_status_booking_cover",
            "account_id",
            "status",
            "merged",
            "booking_date",
            "id",
            "amount_cents",
        ),
    )

    # Bidirektionale Relation zu Account
    # passive_deletes=True lässt die Datenbank-Constraint (CASCADE) durchsetzen
    account: Mapped["Account"] = relationship(
        "Account",
        back_populates="transactions",
        foreign_keys=[account_id],
        passive_deletes=True,
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da bank.py und account.py nur in TYPE_CHECKING Transaction importieren.
from finue.models import account as _account  # noqa: E402,F401
from finue.models import bank as _bank  # noqa: E402,F401
