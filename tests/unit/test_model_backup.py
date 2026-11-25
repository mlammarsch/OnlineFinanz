"""Tests für die Modelle Backup-Job und Backup-Lauf.

Referenziert N-DB-01 (kein rohes SQL), N-SEC-01 (SMB-Passwort nur
verschlüsselt, kein Klartext-Passwortfeld), N-SEC-02 (keine Geheimnisse in
Meldungen), F-BAK-05 (Backup-Job: Ziel, Zeitplan, Aufbewahrung, aktiv) und
F-BAK-06 (Protokoll je Lauf) sowie F-CON-01 (Versionsspalte nur für den Job).
"""

import subprocess
import sys
from datetime import time

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.backup import BackupJob, BackupRun


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests mit aktivierten Foreign Keys."""
    engine = create_db_engine("sqlite://")

    # Foreign Keys aktivieren für SQLite (nötig für ON DELETE SET NULL)
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def make_test_job(name: str = "Naechtliches Backup") -> BackupJob:
    """Hilfsfunktion zum Erstellen eines lokalen Backup-Jobs."""
    return BackupJob(
        name=name,
        target_kind="lokal",
        target_path="/data/backups",
        schedule_time=time(3, 30),
        retention_count=14,
    )


def test_backup_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur
    finue.models.backup importiert wird, ohne Zirkelimport-Fehler.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.backup
configure_mappers()
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Mapper configuration fehlgeschlagen: {result.stderr}"
    assert "OK" in result.stdout


def test_backup_job_with_run_round_trip(factory) -> None:  # type: ignore[no-untyped-def]
    """Job und zugehöriger Lauf werden angelegt und gelesen (F-BAK-05, F-BAK-06)."""
    with factory() as session:
        job = make_test_job()
        run = BackupRun(status="erfolgreich", file_name="finanzuebersicht_2026-11-25_033000.zip")
        run.size_bytes = 4096
        run.message = "Backup erfolgreich erstellt."
        job.runs.append(run)
        session.add(job)
        session.commit()
        job_id = job.id
        run_id = run.id

    with factory() as session:
        retrieved_job = session.query(BackupJob).filter_by(id=job_id).one()
        assert retrieved_job.name == "Naechtliches Backup"
        assert retrieved_job.target_kind == "lokal"
        assert retrieved_job.target_path == "/data/backups"
        assert retrieved_job.retention_count == 14
        assert retrieved_job.schedule_time == time(3, 30)
        assert retrieved_job.enabled is True
        assert len(retrieved_job.runs) == 1

        retrieved_run = session.query(BackupRun).filter_by(id=run_id).one()
        assert retrieved_run.job_id == job_id
        assert retrieved_run.status == "erfolgreich"
        assert retrieved_run.file_name == "finanzuebersicht_2026-11-25_033000.zip"
        assert retrieved_run.size_bytes == 4096
        assert retrieved_run.message == "Backup erfolgreich erstellt."
        # Gegenrichtung der Beziehung
        assert retrieved_run.job is not None
        assert retrieved_run.job.id == job_id


def test_backup_job_defaults(factory) -> None:  # type: ignore[no-untyped-def]
    """Neuer Job: enabled True, retention 14; neuer Lauf: Status 'laeuft' (F-BAK-05/06)."""
    with factory() as session:
        job = BackupJob(
            name="Standard",
            target_kind="lokal",
            target_path="/data/backups",
            schedule_time=time(0, 0),
        )
        run = BackupRun()
        job.runs.append(run)
        session.add(job)
        session.commit()
        job_id = job.id
        run_id = run.id

    with factory() as session:
        retrieved_job = session.query(BackupJob).filter_by(id=job_id).one()
        assert retrieved_job.enabled is True
        assert retrieved_job.retention_count == 14
        assert retrieved_job.weekdays is None
        assert retrieved_job.version == 1

        retrieved_run = session.query(BackupRun).filter_by(id=run_id).one()
        assert retrieved_run.status == "laeuft"
        assert retrieved_run.finished_at is None
        assert retrieved_run.size_bytes is None


def test_backup_job_smb_target(factory) -> None:  # type: ignore[no-untyped-def]
    """SMB-Ziel speichert Host, Freigabe und Benutzer (F-BAK-05)."""
    with factory() as session:
        job = BackupJob(
            name="SMB-Backup",
            target_kind="smb",
            target_path="unterordner/backups",
            smb_host="nas.example.invalid",
            smb_share="sicherungen",
            smb_user="backupuser",
            schedule_time=time(2, 15),
            weekdays="mo,mi,fr",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with factory() as session:
        retrieved = session.query(BackupJob).filter_by(id=job_id).one()
        assert retrieved.target_kind == "smb"
        assert retrieved.smb_host == "nas.example.invalid"
        assert retrieved.smb_share == "sicherungen"
        assert retrieved.smb_user == "backupuser"
        assert retrieved.weekdays == "mo,mi,fr"


def test_no_plaintext_password_attribute(factory) -> None:  # type: ignore[no-untyped-def]
    """N-SEC-01: BackupJob hat kein Klartext-Passwortfeld."""
    with factory() as session:
        job = make_test_job()
        session.add(job)
        session.commit()

        # Es gibt kein Klartext-Passwortfeld, nur die verschlüsselten Bytes.
        assert not hasattr(job, "smb_password")
        assert not hasattr(job, "password")
        assert not hasattr(job, "smb_password_plain")


def test_smb_password_encrypted_nullable_and_round_trip(factory) -> None:  # type: ignore[no-untyped-def]
    """N-SEC-01: smb_password_encrypted ist LargeBinary, nullable und lesbar."""
    with factory() as session:
        job = make_test_job("Mit Passwort")
        job.smb_password_encrypted = b"encrypted_password_bytes"
        session.add(job)
        session.commit()
        job_id = job.id

    with factory() as session:
        retrieved = session.query(BackupJob).filter_by(id=job_id).one()
        assert retrieved.smb_password_encrypted == b"encrypted_password_bytes"

    with factory() as session:
        job = make_test_job("Ohne Passwort")
        session.add(job)
        session.commit()
        job_id = job.id

    with factory() as session:
        retrieved = session.query(BackupJob).filter_by(id=job_id).one()
        assert retrieved.smb_password_encrypted is None


def test_backup_job_rejects_invalid_target_kind(factory) -> None:  # type: ignore[no-untyped-def]
    """target_kind-Check lehnt andere Werte als lokal|smb ab (F-BAK-05)."""
    with factory() as session:
        job = make_test_job()
        job.target_kind = "ftp"  # Nicht erlaubt
        session.add(job)
        with pytest.raises(IntegrityError):
            session.commit()


def test_backup_run_rejects_invalid_status(factory) -> None:  # type: ignore[no-untyped-def]
    """status-Check lehnt unbekannte Werte ab (F-BAK-06)."""
    with factory() as session:
        job = make_test_job()
        run = BackupRun(status="halbfertig")  # Nicht erlaubt
        job.runs.append(run)
        session.add(job)
        with pytest.raises(IntegrityError):
            session.commit()


def test_job_delete_sets_run_job_id_null(factory) -> None:  # type: ignore[no-untyped-def]
    """Job löschen setzt die Zuordnung des Laufs auf NULL, Historie bleibt (F-BAK-06)."""
    with factory() as session:
        job = make_test_job()
        run = BackupRun(status="erfolgreich", file_name="finanzuebersicht_2026-11-25.zip")
        job.runs.append(run)
        session.add(job)
        session.commit()
        job_id = job.id
        run_id = run.id

    with factory() as session:
        session.execute(delete(BackupJob).where(BackupJob.id == job_id))
        session.commit()

    with factory() as session:
        # Der Job ist weg, der Lauf bleibt mit job_id NULL erhalten.
        assert session.query(BackupJob).filter_by(id=job_id).one_or_none() is None
        retrieved_run = session.query(BackupRun).filter_by(id=run_id).one()
        assert retrieved_run.job_id is None
        assert retrieved_run.file_name == "finanzuebersicht_2026-11-25.zip"


def test_backup_job_version_increments(factory) -> None:  # type: ignore[no-untyped-def]
    """Version steigt bei Update des Jobs (F-CON-01)."""
    with factory() as session:
        job = make_test_job()
        session.add(job)
        session.commit()
        job_id = job.id
        assert job.version == 1

    with factory() as session:
        retrieved = session.query(BackupJob).filter_by(id=job_id).one()
        retrieved.name = "Naechtliches Backup (korrigiert)"
        session.commit()

    with factory() as session:
        retrieved = session.query(BackupJob).filter_by(id=job_id).one()
        assert retrieved.version == 2


def test_backup_run_has_no_version(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-02: BackupRun hat bewusst keine Versionsspalte (nur der Job)."""
    with factory() as session:
        job = make_test_job()
        run = BackupRun()
        job.runs.append(run)
        session.add(job)
        session.commit()

        assert not hasattr(run, "version")
