"""Modell für das pflegbare Bankfehler-Mapping.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-SYN-05 (Bankcode ->
Klartext und Handlungsempfehlung), F-SYN-09 (Pflege in der UI: Code, optional
nur für eine BLZ, Klartext, Handlungsempfehlung, wiederholbar ja/nein,
gekennzeichnete Standardeinträge) und F-CON-01 (Versionsspalte für
optimistische Sperre).

Ein Eintrag mit ``blz`` gilt nur für diese Bankleitzahl und hat Vorrang vor
dem allgemeinen Standardeintrag mit ``blz = NULL`` (F-SYN-09). Die
Eindeutigkeit ``(code, blz)`` verhindert doppelte Einträge für denselben
Geltungsbereich; wie bei den übrigen Unique-Constraints im Projekt erlaubt
SQL NULL mehrfach, die Service-Schicht hält die Standardeinträge eindeutig.
"""

from sqlalchemy import Boolean, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finue.db.base import Base
from finue.db.mixins import VersionedMixin


class BankErrorMapping(Base, VersionedMixin):
    """Klartext und Handlungsempfehlung zu einem Bank-Fehlercode (F-SYN-05).

    Implementiert N-DB-01 (kein rohes SQL), F-SYN-05 (Bankcode -> Klartext
    und Handlungsempfehlung), F-SYN-09 (BLZ-spezifische Einträge, wiederholbar,
    Standardkennzeichnung) und F-CON-01 (Versionsspalte).

    ``code`` ist der Fehlercode der Bank, ``blz`` die optionale Beschränkung
    auf eine Bankleitzahl. ``message`` ist der Klartext, ``recommendation`` die
    Handlungsempfehlung. ``retryable`` markiert wiederholbare Codes
    (F-SYN-06), ``is_default`` kennzeichnet mitgelieferte Standardeinträge,
    die sich auf den Standard zurücksetzen lassen (F-SYN-09). Die Meldungen
    enthalten keine Geheimnisse (N-SEC-02).
    """

    __tablename__ = "bank_error_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Fehlercode der Bank (F-SYN-05), z. B. '9800' oder '9010'
    code: Mapped[str] = mapped_column(String(20), nullable=False)

    # Optionale Bindung an eine Bankleitzahl; NULL gilt bankübergreifend
    blz: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Klartext und Handlungsempfehlung ohne Geheimnisse (N-SEC-02)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)

    # Wiederholbar (F-SYN-06) und mitgelieferter Standard (F-SYN-09)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("code", "blz", name="uq_bank_error_mapping_code_blz"),
        # Nachschlage-Index für die Auflösung über den Code (F-SYN-05)
        Index("ix_bank_error_mapping_code", "code"),
    )
