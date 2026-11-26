"""Modell für gespeicherte Exportprofile.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-EXP-03 (CSV-Exportoptionen
je Profil: Trennzeichen, Dezimalformat, Datumsformat, Kodierung, Spaltenauswahl
und -reihenfolge; Profile speicherbar) und F-CON-02 (Versionsspalte für
optimistisches Sperren).

Ein Profil hält den Namen (eindeutig), ein Flag für das Standardprofil, die
Formatoptionen (``options``) und die Spaltenauswahl/-reihenfolge (``columns``)
je als JSON. Der Wertebereich der Optionswerte wird im Export-Service (T-098)
validiert; dieses Modell beschränkt sich auf die Ablage.
"""

from typing import Any

from sqlalchemy import JSON, Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, VersionedMixin


class ExportProfile(Base, VersionedMixin, TimestampMixin):
    """Gespeichertes CSV-Exportprofil (F-EXP-03).

    Implementiert N-DB-01 (kein rohes SQL), F-EXP-03 (speicherbare Profile mit
    Spaltenauswahl und -reihenfolge) und F-CON-02 (Versionsspalte).

    ``options`` traegt die Formatoptionen Trennzeichen, Dezimalformat,
    Datumsformat und Kodierung; ``columns`` die Auswahl und Reihenfolge der
    exportierten Spalten. ``is_default`` kennzeichnet genau das Standardprofil
    (die Eindeutigkeit dieses Flags erzwingt der Export-Service, weil SQLite,
    PostgreSQL und MySQL mehrere ``false`` zulassen).
    """

    __tablename__ = "export_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Formatoptionen (Trennzeichen, Dezimalformat, Datumsformat, Kodierung).
    options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Spaltenauswahl und -reihenfolge des CSV-Exports.
    columns: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (UniqueConstraint("name", name="uq_export_profile_name"),)
