"""Modelle für Import-Läufe und deren Fehler.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-SYN-05 (Fehler-Mapping:
Bankcode -> Klartext), F-SYN-07 (Sync-Protokoll je Import-Lauf mit Zeitpunkt,
Konto, Status, Zählern und Fehlercode/Klartext), F-IMP-06 (Datei-Import als
Import-Lauf mit Protokoll) und F-CON-01 (Versionsspalte für optimistische
Sperre).

N-SEC-02: Die Meldungsfelder (message, code) dürfen niemals Geheimnisse wie
PIN, TAN, Schlüssel, Tokens oder Rohdaten von Zugangsdaten enthalten. Diese
Vorgabe wird nicht per Datenbank-Constraint, sondern in der Service-Schicht und
den Log-Filtern durchgesetzt (siehe AuditLog). Auch ImportRunError dient nur zur
Protokollierung des Klartexts aus dem Fehler-Mapping.
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, UtcDateTime, VersionedMixin
from finue.timeutil import utc_now

if TYPE_CHECKING:
    from finue.models.account import Account


class ImportRun(Base, VersionedMixin, TimestampMixin):
    """Ein Sync oder Datei-Import (CSV, JSON) für ein Konto (F-SYN-07, F-IMP-06).

    Implementiert N-DB-01 (kein rohes SQL), F-SYN-07 (Status und Zähler je
    Lauf), F-IMP-06 (Protokoll des Datei-Imports) und F-CON-01 (Versionsspalte
    für optimistische Sperre).

    `kind` unterscheidet FinTS-Sync ('sync') von Datei-Importen ('csv', 'json').
    `status` fuehrt die abschliessenden Werte nach F-SYN-07: 'erfolgreich',
    'teilweise', 'fehler', 'abgebrochen' und 'zeitueberschreitung'. Zusaetzlich
    sind die technischen Zwischenzustaende 'laeuft' (Lauf gestartet) und
    'wartet_auf_tan' (angehaltener Dialog, N-SEC-03) erlaubt; sie sind keine
    abschliessenden SPEC-Statuswerte. Die Zähler `new_count`, `skipped_count`,
    `suspected_count` und `failed_count` protokollieren das Ergebnis (F-IMP-06,
    F-SYN-07). `message` ist der Klartext-Status bzw. die Handlungsempfehlung
    nach F-SYN-05 und enthält keine Geheimnisse (N-SEC-02).

    Die Zeilen werden beim Sync bzw. Import angelegt und während des Laufs
    aktualisiert; jeder Einzel-Fehler hängt als ImportRunError am Lauf.
    `job_id` gruppiert die Läufe eines Batch-Anstosses (F-SYN-07): mehrere
    Konten desselben Batch teilen sich eine Kennung, Einzel-Sync und
    Datei-Import lassen sie NULL.
    """

    __tablename__ = "import_run"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Zugehöriges Konto (CASCADE entfernt Läufe beim Löschen des Kontos)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Gruppenkennung eines Batch-Laufs (F-SYN-07): mehrere ImportRun-Zeilen
    # desselben Batch teilen diese ID. NULL bei Einzel-Sync und Datei-Import.
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # Art des Laufs: sync (FinTS), csv oder json (Datei-Import)
    kind: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    # Status des Laufs (F-SYN-07)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="laeuft",
    )

    # Beginn und Ende des Laufs in UTC (N-DB-02); finished_at bleibt offen,
    # solange der Lauf läuft oder auf eine TAN wartet
    started_at: Mapped[UtcDateTime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
    )
    finished_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)

    # Ergebniszähler (F-SYN-07, F-IMP-06)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suspected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Klartext-Status bzw. Handlungsempfehlung (F-SYN-05, ohne Geheimnisse N-SEC-02)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Constraints zur Validierung
    __table_args__ = (
        CheckConstraint(
            "kind IN ('sync', 'csv', 'json')",
            name="ck_import_run_kind",
        ),
        CheckConstraint(
            # F-SYN-07: abschliessende Statuswerte plus technische Zwischenzustaende
            # 'laeuft' und 'wartet_auf_tan' (N-SEC-03).
            "status IN ('laeuft', 'wartet_auf_tan', 'erfolgreich', 'teilweise', "
            "'fehler', 'abgebrochen', 'zeitueberschreitung')",
            name="ck_import_run_status",
        ),
        # Läufe werden je Konto nach Startzeit gelistet (F-SYN-07)
        Index("ix_import_run_account_started", "account_id", "started_at"),
    )

    # Einzel-Fehler des Laufs (F-SYN-05, F-SYN-07); Löschen des Laufs entfernt sie
    errors: Mapped[list["ImportRunError"]] = relationship(
        "ImportRunError",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ImportRunError.row_no",
    )

    # Viewonly-Relation: nur lesende Abbildung, kein Schreiben über die Beziehung
    account: Mapped["Account"] = relationship(
        "Account",
        foreign_keys=[account_id],
        viewonly=True,
    )


class ImportRunError(Base):
    """Einzelner Fehler eines Import-Laufs (F-SYN-05, F-SYN-07).

    Implementiert N-DB-01 (kein rohes SQL) und F-SYN-05 (Bankcode -> Klartext).

    `code` ist der Bank- bzw. Import-Fehlercode (z. B. ING '9800' oder '9010'),
    `message` der zugehörige Klartext bzw. die Handlungsempfehlung. `row_no`
    verweist auf die betroffene Zeile eines Datei-Imports und ist bei Lauf- oder
    Sync-Fehlern ohne Zeilenbezug NULL.

    Append-only und ohne Versionsspalte: Fehlerzeilen werden nur angelegt und
    beim Löschen des Laufs CASCADE-gelöscht, nie aktualisiert (N-SEC-02: die
    Meldungen enthalten keine Geheimnisse).
    """

    __tablename__ = "import_run_error"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Zugehöriger Lauf (CASCADE entfernt Fehler beim Löschen des Laufs)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("import_run.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Betroffene Zeile bei Datei-Import; NULL bei Lauf-/Sync-Fehlern
    row_no: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Fehlercode (F-SYN-05) und Klartext ohne Geheimnisse (N-SEC-02)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Constraints: Index für die sortierte Auflistung der Fehler je Lauf
    __table_args__ = (Index("ix_import_run_error_run_row", "run_id", "row_no"),)

    # Bidirektionale Relation zum Lauf
    run: Mapped["ImportRun"] = relationship(
        "ImportRun",
        back_populates="errors",
        foreign_keys=[run_id],
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da account.py nur in TYPE_CHECKING ImportRun importiert (falls überhaupt).
# Der Import stellt sicher, dass configure_mappers() allein mit
# finue.models.import_run erfolgreich ist.
from finue.models import account  # noqa: E402,F401
