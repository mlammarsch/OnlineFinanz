"""Tests für das Auditprotokoll-Modell.

Referenziert N-DB-01 (kein rohes SQL), F-MCP-09 (Auditprotokoll für MCP),
F-KTO-07 (Konto-Löschung entfernt Protokolle) und N-SEC-02 (keine Geheimnisse
in Logs). Testet append-only Semantik (ORM-Events), JSON-Roundtrip, Cascade-
Löschung und die Unmöglichkeit von ORM-Updates. Core-Updates werden nicht
abgefangen (Schutz gegen Programmierfehler, nicht Datenbank-Garantie).
"""

import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import delete, event, inspect
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.audit import AuditLog
from finue.models.bank import Bank  # noqa: F401


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests mit aktivierten Foreign Keys."""
    engine = create_db_engine("sqlite://")

    # Foreign Keys aktivieren für SQLite
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def make_test_account(name: str = "Testkonto") -> Account:
    """Hilfsfunktion zum Erstellen eines Test-Kontos."""
    return Account(
        name=name,
        currency="EUR",
        source="fints",
    )


def test_audit_log_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur
    finue.models.audit importiert wird, ohne Zirkelimport-Fehler.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.audit
configure_mappers()
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Mapper configuration fehlgeschlagen: {result.stderr}"
    assert "ok" in result.stdout


def test_create_audit_log_with_full_json(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog kann mit verschachteltem JSON mit Umlauten und Listen erstellt werden."""
    with factory() as session:
        account = make_test_account("Prüfkonto")
        session.add(account)
        session.commit()
        account_id = account.id

    # Komplexes JSON mit Umlauten und Listen
    changes: dict[str, Any] = {
        "entity_name": "Transaktion",
        "field_name": "Bemerkung",
        "old_value": "Überweisungsauftrag",
        "new_value": "Überweisungsauftrag & Rückzahlung",
        "nested": {
            "größe": "Ü-äöäöä",
            "liste": [1, 2, {"ä": "ö"}],
            "null_wert": None,
        },
    }

    with factory() as session:
        log = AuditLog(
            account_id=account_id,
            entity="transaction",
            entity_id=123,
            action="updated",
            source="ui",
            changes=changes,
        )
        session.add(log)
        session.commit()
        log_id = log.id

    # Roundtrip-Test: JSON wird korrekt aus der DB gelesen
    with factory() as session:
        retrieved = session.query(AuditLog).filter_by(id=log_id).one()
        assert retrieved.changes == changes
        assert retrieved.changes["nested"]["größe"] == "Ü-äöäöä"
        assert retrieved.changes["nested"]["liste"] == [1, 2, {"ä": "ö"}]
        assert retrieved.changes["nested"]["null_wert"] is None


def test_create_audit_log_without_account(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog kann ohne Konto-Bezug erstellt werden (globale Ereignisse)."""
    with factory() as session:
        log = AuditLog(
            account_id=None,
            entity="system",
            entity_id=None,
            action="database_upgrade",
            source="system",
        )
        session.add(log)
        session.commit()
        log_id = log.id

    with factory() as session:
        retrieved = session.query(AuditLog).filter_by(id=log_id).one()
        assert retrieved.account_id is None
        assert retrieved.entity == "system"
        assert retrieved.action == "database_upgrade"
        assert retrieved.source == "system"


def test_create_audit_log_with_token_id(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog kann token_id enthalten (nullable, FK folgt in Migration)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        log = AuditLog(
            account_id=account_id,
            entity="transaction",
            entity_id=456,
            action="created",
            source="mcp",
            token_id=789,  # MCP-Token-ID, keine FK-Validierung (folgt später)
        )
        session.add(log)
        session.commit()
        log_id = log.id

    with factory() as session:
        retrieved = session.query(AuditLog).filter_by(id=log_id).one()
        assert retrieved.token_id == 789


def test_audit_log_ts_is_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """ts ist immer aware UTC."""
    with factory() as session:
        log = AuditLog(
            entity="test",
            action="created",
            source="system",
        )
        session.add(log)
        session.commit()
        log_id = log.id

    with factory() as session:
        retrieved = session.query(AuditLog).filter_by(id=log_id).one()
        assert retrieved.ts.tzinfo is not None
        assert retrieved.ts.tzinfo == timezone.utc
        assert isinstance(retrieved.ts, datetime)


def test_audit_log_accepts_valid_sources(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog akzeptiert alle erlaubten Quellen."""
    valid_sources = ["ui", "mcp", "sync", "import", "system"]
    with factory() as session:
        for source in valid_sources:
            log = AuditLog(
                entity="test",
                action="created",
                source=source,
            )
            session.add(log)
        session.commit()

    with factory() as session:
        count = session.query(AuditLog).count()
        assert count == len(valid_sources)


def test_audit_log_rejects_invalid_source(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog lehnt ungültige Quellen ab."""
    with factory() as session:
        log = AuditLog(
            entity="test",
            action="created",
            source="xyz",  # Ungültig
        )
        session.add(log)
        with pytest.raises(IntegrityError):
            session.commit()


def test_account_deletion_cascades_to_audit_logs(factory) -> None:  # type: ignore[no-untyped-def]
    """Konto per Core-Delete entfernt seine Auditlog-Einträge (CASCADE)."""
    with factory() as session:
        account1 = make_test_account("Konto 1")
        account2 = make_test_account("Konto 2")
        session.add(account1)
        session.add(account2)
        session.commit()
        account1_id = account1.id
        account2_id = account2.id

    with factory() as session:
        # Logs für beide Konten
        log1 = AuditLog(
            account_id=account1_id,
            entity="transaction",
            entity_id=1,
            action="created",
            source="sync",
        )
        log2 = AuditLog(
            account_id=account2_id,
            entity="transaction",
            entity_id=2,
            action="created",
            source="sync",
        )
        session.add(log1)
        session.add(log2)
        session.commit()
        log1_id = log1.id
        log2_id = log2.id

    # Konto 1 löschen
    with factory() as session:
        session.execute(delete(Account).where(Account.id == account1_id))
        session.commit()

    # Log1 sollte gelöscht sein, Log2 sollte noch da sein
    with factory() as session:
        log1_retrieved = session.query(AuditLog).filter_by(id=log1_id).one_or_none()
        log2_retrieved = session.query(AuditLog).filter_by(id=log2_id).one_or_none()
        assert log1_retrieved is None, "Log vom gelöschten Konto sollte auch gelöscht sein"
        assert log2_retrieved is not None, "Log von anderem Konto sollte bleiben"
        assert log2_retrieved.account_id == account2_id


def test_other_accounts_logs_unaffected(factory) -> None:  # type: ignore[no-untyped-def]
    """Logs anderer Konten bleiben beim Löschen eines Kontos unverändert."""
    with factory() as session:
        account1 = make_test_account("Konto 1")
        account2 = make_test_account("Konto 2")
        account3 = make_test_account("Konto 3")
        session.add(account1)
        session.add(account2)
        session.add(account3)
        session.commit()
        account1_id = account1.id
        account2_id = account2.id
        account3_id = account3.id

    with factory() as session:
        logs = [
            AuditLog(
                account_id=account1_id,
                entity="transaction",
                entity_id=i,
                action="created",
                source="sync",
            )
            for i in range(1, 4)
        ]
        logs.extend(
            [
                AuditLog(
                    account_id=account2_id,
                    entity="transaction",
                    entity_id=i,
                    action="created",
                    source="sync",
                )
                for i in range(4, 7)
            ]
        )
        logs.extend(
            [
                AuditLog(
                    account_id=account3_id,
                    entity="transaction",
                    entity_id=i,
                    action="created",
                    source="sync",
                )
                for i in range(7, 10)
            ]
        )
        session.add_all(logs)
        session.commit()

    # Konto 1 löschen
    with factory() as session:
        session.execute(delete(Account).where(Account.id == account1_id))
        session.commit()

    # Prüfe, dass nur Logs von Konto 1 gelöscht wurden
    with factory() as session:
        remaining_logs = session.query(AuditLog).all()
        assert len(remaining_logs) == 6  # 3 (K2) + 3 (K3)
        for log in remaining_logs:
            assert log.account_id in [account2_id, account3_id]
            assert log.account_id != account1_id


def test_update_audit_log_raises_runtime_error(factory) -> None:  # type: ignore[no-untyped-def]
    """Update eines AuditLog-Eintrags wirft RuntimeError (append-only)."""
    with factory() as session:
        log = AuditLog(
            entity="transaction",
            entity_id=1,
            action="created",
            source="ui",
        )
        session.add(log)
        session.commit()
        log_id = log.id

    with factory() as session:
        retrieved = session.query(AuditLog).filter_by(id=log_id).one()
        retrieved.action = "updated"  # Versuch zu ändern
        with pytest.raises(RuntimeError) as exc_info:
            session.commit()
        assert "Auditprotokolle können nicht aktualisiert werden" in str(exc_info.value)


def test_audit_log_has_required_indexes(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog hat die erforderlichen Indizes."""
    with factory() as session:
        inspector = inspect(session.get_bind())
        indexes = inspector.get_indexes("audit_log")
        index_names = {idx["name"] for idx in indexes}

        # Prüfe, dass die erforderlichen Indizes existieren
        assert "ix_audit_log_account_ts" in index_names
        assert "ix_audit_log_entity_id" in index_names


def test_audit_log_has_no_version_attribute(factory) -> None:  # type: ignore[no-untyped-def]
    """AuditLog hat KEIN version-Attribut (append-only, kein VersionedMixin)."""
    with factory() as session:
        log = AuditLog(
            entity="test",
            action="created",
            source="system",
        )
        session.add(log)
        session.commit()

        # Prüfe, dass kein version-Attribut existiert
        assert not hasattr(log, "version"), "AuditLog sollte kein version-Attribut haben"


def test_core_update_audit_log_bypasses_orm_protection(factory) -> None:  # type: ignore[no-untyped-def]
    """SQLAlchemy Core-Update auf AuditLog wird NICHT abgefangen (N-DB-01, kein ORM-Event).

    Dies dokumentiert das bekannte Verhalten: ORM-Events (before_update) werden
    nur bei ORM-basierten Updates ausgelöst. Core-Level Updates via execute(update(...))
    umgehen den before_update-Handler völlig.

    Das ist ein Schutz gegen Programmierfehler in der Service-Schicht (N-DB-01),
    keine Datenbank-Garantie. Die Service-Schicht muss AuditLog-Updates vermeiden.
    """
    from sqlalchemy import update

    with factory() as session:
        log = AuditLog(
            entity="test",
            action="created",
            source="system",
        )
        session.add(log)
        session.commit()
        log_id = log.id

    with factory() as session:
        # Core-Update umgeht den before_update-Handler
        session.execute(update(AuditLog).where(AuditLog.id == log_id).values(action="updated"))
        session.commit()

    # Verifiziere, dass das Core-Update erfolgreich war (Handler wurde nicht ausgelöst)
    with factory() as session:
        retrieved = session.query(AuditLog).filter_by(id=log_id).one()
        assert retrieved.action == "updated", "Core-Update sollte erfolgreich sein (Handler wurde umgangen)"
