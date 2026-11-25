"""Modelle für Backup-Jobs und deren Lauf-Protokoll.

Implementiert N-DB-01 (kein rohes SQL für Daten), N-DB-02 (Zeitstempel und
Beträge in ganzen Einheiten) und F-BAK-05 (Backup-Jobs: Name, Ziel, Zeitplan,
Aufbewahrung, Aktiv-Flag; Ziele lokal oder SMB) sowie F-BAK-06 (jeder Lauf wird
protokolliert: Zeitpunkt, Größe, Dateiname, Status, Fehler im Klartext).

Sicherheitsentscheidung (kein Klartext-Passwort, N-SEC-01):
    Das SMB-Passwort wird ausschliesslich verschlüsselt persistiert. Das Feld
    ``smb_password_encrypted`` ist vom Typ ``LargeBinary`` und enthält die
    bereits verschlüsselten Bytes (Fernet o. Ä.), niemals das Passwort im
    Klartext. Es gibt bewusst kein Feld ``smb_password``, ``password`` oder
    ``smb_password_plain``. Verschlüsselung und Entschlüsselung selbst liegen
    in der Service-Schicht; dieses Modell speichert nur die verschlüsselten
    Bytes und enthält keine Methode, die ein Klartext-Passwort erzeugt oder
    zurückgibt. Das Feld ist nullable, damit Testdaten ohne
    Verschlüsselungsschlüssel erzeugt werden können (analog zu
    ``BankAccess.pin_encrypted``).

Sicherheitsentscheidung (N-SEC-02, keine Geheimnisse in Meldungen):
    ``BackupRun.message`` ist der Klartext-Status bzw. die Fehlermeldung und
    darf niemals das SMB-Passwort, den Fernet-Schlüssel oder andere Geheimnisse
    enthalten. Diese Vorgabe wird nicht per Datenbank-Constraint, sondern in
    der Service-Schicht und den Log-Filtern durchgesetzt.
"""

from datetime import time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, UtcDateTime, VersionedMixin
from finue.timeutil import utc_now


class BackupJob(Base, VersionedMixin, TimestampMixin):
    """Ein geplanter Backup-Job mit lokalem oder SMB-Ziel (F-BAK-05).

    Implementiert N-DB-01 (kein rohes SQL), N-SEC-01 (SMB-Passwort nur
    verschlüsselt) und F-CON-01 (Versionsspalte für optimistische Sperre, da
    der Nutzer den Job in den Einstellungen bearbeitet).

    ``target_kind`` ist exakt ``lokal`` oder ``smb``. ``target_path`` ist bei
    beiden Zielen der Pfad: bei ``lokal`` das Verzeichnis im Daten-Volume, bei
    ``smb`` der Pfad innerhalb der Freigabe. Die SMB-Felder ``smb_host``,
    ``smb_share`` und ``smb_user`` sind nur für ``target_kind = 'smb'`` gesetzt
    und sonst NULL.

    ``schedule_time`` ist die Uhrzeit des täglichen Laufs (F-BAK-05, Typ
    ``Time``). ``weekdays`` schränkt den Lauf optional auf bestimmte Wochentage
    ein (F-BAK-05): ein String mit kommagetrennten Kürzeln ``mo,di,mi,do,fr,sa,so``
    (Reihenfolge und Gross-/Kleinschreibung werden in der Service-Schicht
    normalisiert); NULL oder leer bedeutet täglich. Die einfachste portable
    Lösung ist ein String ohne Datenbank-spezifisches Bitfeld. ``retention_count``
    ist die Anzahl der am Ziel aufzubewahrenden Backups (Vorgabe 14, F-BAK-05);
    ``enabled`` steuert, ob der Zeitplaner den Job ausführt.
    """

    __tablename__ = "backup_job"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Anzeigename des Jobs (F-BAK-05)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Zielart: lokal oder smb (F-BAK-05)
    target_kind: Mapped[str] = mapped_column(String(10), nullable=False)

    # Pfad des Ziels (lokal: Verzeichnis; smb: Pfad innerhalb der Freigabe)
    target_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # SMB-Zugang (nur bei target_kind = 'smb' gesetzt, F-BAK-05).
    # Das Passwort liegt ausschliesslich verschlüsselt vor (N-SEC-01).
    smb_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_share: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Zeitplan: Uhrzeit des täglichen Laufs und optionale Wochentage (F-BAK-05)
    schedule_time: Mapped[time] = mapped_column(Time, nullable=False)
    weekdays: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Aufbewahrung: Anzahl der am Ziel zu behaltenden Backups (F-BAK-05)
    retention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=14)

    # Aktives Flag: nur aktive Jobs führt der Zeitplaner aus (F-BAK-05)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Constraints zur Validierung der Zielart
    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('lokal', 'smb')",
            name="ck_backup_job_target_kind",
        ),
    )

    # Läufe des Jobs. Beim Löschen des Jobs bleibt der Lauf als Historie
    # erhalten; die Datenbank setzt ``job_id`` auf NULL (ON DELETE SET NULL).
    # ``passive_deletes`` überlässt das Setzen der Datenbank.
    runs: Mapped[list["BackupRun"]] = relationship(
        "BackupRun",
        back_populates="job",
        passive_deletes=True,
        order_by="BackupRun.started_at",
    )


class BackupRun(Base):
    """Protokolleintrag eines Backup-Laufs (F-BAK-06).

    Implementiert N-DB-01 (kein rohes SQL), F-BAK-06 (Protokoll je Lauf mit
    Zeitpunkt, Größe, Dateiname, Status und Klartext-Fehler) und N-SEC-02
    (``message`` enthält keine Geheimnisse).

    ``status`` beschreibt den Lauf und ist exakt auf ``laeuft``, ``erfolgreich``
    oder ``fehlgeschlagen`` beschränkt. Ein neuer Lauf startet mit ``laeuft``
    und ``finished_at = NULL``; beim Abschluss werden ``finished_at``, ``status``,
    ``file_name``, ``size_bytes`` und ``message`` gesetzt. ``size_bytes`` ist die
    Grösse der erzeugten Backup-Datei.

    Bewusst KEIN ``VersionedMixin``: Läufe werden nicht durch gleichzeitige
    Nutzer-Bearbeitung geändert, sondern vom Zeitplaner bzw. Joblauf angelegt und
    abgeschlossen (ein Schreiber). Die Register-Festlegung nimmt ``BackupRun``
    deshalb zusammen mit ``AuditLog`` und ``ImportRunError`` von der
    Versionspflicht aus (F-CON-02 nennt Läufe nicht unter den versionierten
    Entitäten).

    ``job_id`` ist nullable mit ON DELETE SET NULL: Wird der Job gelöscht,
    bleibt der Lauf als Historie erhalten und verliert nur die Zuordnung
    (konsistent zur Vorlagen-Entscheidung, Historie soll nicht verloren gehen).
    """

    __tablename__ = "backup_run"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Zugehöriger Job; NULL, wenn der Job später gelöscht wurde (SET NULL)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("backup_job.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Beginn und Ende des Laufs in aware UTC (N-DB-02); finished_at bleibt offen,
    # solange der Lauf läuft
    started_at: Mapped[UtcDateTime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
    )
    finished_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)

    # Status des Laufs (F-BAK-06)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="laeuft",
    )

    # Name und Grösse der erzeugten Backup-Datei (F-BAK-06)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Klartext-Status bzw. Fehlermeldung, ohne Geheimnisse (F-BAK-06, N-SEC-02)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Constraints zur Validierung des Status
    __table_args__ = (
        CheckConstraint(
            "status IN ('laeuft', 'erfolgreich', 'fehlgeschlagen')",
            name="ck_backup_run_status",
        ),
        # Läufe werden je Job nach Startzeit gelistet (F-BAK-06)
        Index("ix_backup_run_job_started", "job_id", "started_at"),
    )

    # Bidirektionale Relation zum Job (optional, SET NULL beim Job-Löschen)
    job: Mapped["BackupJob | None"] = relationship(
        "BackupJob",
        back_populates="runs",
        foreign_keys=[job_id],
    )


# Kein Zirkelimport-Workaround nötig: BackupJob und BackupRun haben weder einen
# Foreign Key noch eine Beziehung zu account.py oder anderen Modellen (die
# einzige Beziehung zeigt innerhalb dieses Moduls auf backup_job).
# configure_mappers() ist daher bereits mit finue.models.backup allein
# erfolgreich (siehe Tests).
