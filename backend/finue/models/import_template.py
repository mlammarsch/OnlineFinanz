"""Modell für gespeicherte Import-Vorlagen.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-IMP-01 (Assistent:
Formaterkennung CSV oder JSON, Mapping und Vorschau) und F-IMP-05 (Mapping-
Vorlagen speichern und pro Konto vorschlagen). Zusätzlich F-CON-01
(Versionsspalte für optimistische Sperre).

Eine Vorlage hält das Mapping Quellfeld -> Buchungsfeld (`mapping`) sowie die
Formatoptionen Codierung, Trennzeichen, Datumsformat und Dezimaltrennzeichen
(F-IMP-02, F-IMP-03). `kind` unterscheidet CSV- von JSON-Vorlagen. `account_id`
ist optional: eine Vorlage kann kontounabhängig sein oder einem bestimmten
Konto zugeordnet werden (F-IMP-05, "pro Konto vorschlagen").
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, VersionedMixin

if TYPE_CHECKING:
    from finue.models.account import Account


class ImportTemplate(Base, VersionedMixin, TimestampMixin):
    """Gespeicherte Mapping-Vorlage für den Datei-Import (F-IMP-05).

    Implementiert N-DB-01 (kein rohes SQL), F-IMP-01 (Mapping und Vorschau im
    Import-Assistenten), F-IMP-02 (Formatoptionen Codierung, Trennzeichen,
    Datums- und Dezimalformat), F-IMP-03 (Mapping Quellfeld -> Buchungsfeld)
    und F-CON-01 (Versionsspalte für optimistische Sperre).

    `kind` ist 'csv' oder 'json'. `mapping` hält die Zuordnung der Quellfelder
    zu den Buchungsfeldern als JSON. Die Formatfelder `delimiter`, `encoding`,
    `date_format` und `decimal_separator` gelten für CSV-Vorlagen und können
    bei JSON-Vorlagen NULL sein.

    `account_id` ist optional (nullable). On-Delete-Verhalten ist SET NULL: Wird
    das zugeordnete Konto gelöscht, bleibt die Vorlage erhalten und wird
    kontounabhängig, statt mit dem Konto zu verschwinden. Eine einmal gepflegte
    Vorlage (F-IMP-05) soll nicht durch das Löschen eines Kontos verloren gehen
    und lässt sich später einem anderen Konto zuordnen. Die DB-Constraint
    (SET NULL) wird per ``passive_deletes`` von der Datenbank durchgesetzt.
    """

    __tablename__ = "import_template"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Anzeigename der Vorlage (F-IMP-05)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Format der Quelldatei: csv oder json (F-IMP-01)
    kind: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    # Optionale Konto-Zuordnung (NULL = kontounabhängig, SET NULL beim Konto-Löschen)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Mapping Quellfeld -> Buchungsfeld als JSON (F-IMP-01, F-IMP-03)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # CSV-Formatoptionen (F-IMP-02); bei JSON-Vorlagen NULL
    delimiter: Mapped[str | None] = mapped_column(String(10), nullable=True)
    encoding: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_format: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decimal_separator: Mapped[str | None] = mapped_column(String(5), nullable=True)

    # Constraints zur Validierung
    __table_args__ = (
        CheckConstraint(
            "kind IN ('csv', 'json')",
            name="ck_import_template_kind",
        ),
        # Vorlagen werden je Konto vorgeschlagen (F-IMP-05)
        Index("ix_import_template_account", "account_id"),
    )

    # Viewonly-Relation: nur lesende Abbildung, kein Schreiben über die Beziehung
    account: Mapped["Account | None"] = relationship(
        "Account",
        foreign_keys=[account_id],
        viewonly=True,
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da account.py nur in TYPE_CHECKING ImportTemplate importiert (falls überhaupt).
# Der Import stellt sicher, dass configure_mappers() allein mit
# finue.models.import_template erfolgreich ist.
from finue.models import account  # noqa: E402,F401
