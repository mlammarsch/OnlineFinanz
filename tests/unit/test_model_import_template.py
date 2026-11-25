"""Tests für das Modell der Import-Vorlage.

Referenziert N-DB-01 (kein rohes SQL), F-IMP-01 (Assistent: Format, Mapping,
Vorschau), F-IMP-02 (Formatoptionen), F-IMP-03 (Mapping Quellfeld ->
Buchungsfeld), F-IMP-05 (Mapping-Vorlagen speichern) und F-CON-01
(Versionsspalte für optimistische Sperre).
"""

import subprocess
import sys

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import Bank, BankAccess  # noqa: F401
from finue.models.import_template import ImportTemplate


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


def make_csv_template(account_id: int | None = None) -> ImportTemplate:
    """Hilfsfunktion zum Erstellen einer CSV-Vorlage."""
    return ImportTemplate(
        name="N26 CSV",
        kind="csv",
        account_id=account_id,
        mapping={"date": "Buchungstag", "amount.value": "Betrag"},
        delimiter=";",
        encoding="utf-8",
        date_format="TT.MM.JJJJ",
        decimal_separator=",",
    )


def test_import_template_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Testet, dass configure_mappers() erfolgreich ist, wenn nur
    finue.models.import_template importiert wird, ohne Zirkelimport-Fehler.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.import_template
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


def test_import_template_mapping_json_round_trip(factory) -> None:  # type: ignore[no-untyped-def]
    """mapping übersteht den echten Persistenz-Roundtrip unverändert (F-IMP-03).

    Anlegen mit einem nicht-trivialen, verschachtelten Mapping, aus der
    Datenbank in einer frischen Session neu laden und auf Gleichheit prüfen.
    """
    mapping = {
        "date": "Buchungstag",
        "amount": {"value": "Betrag (EUR)", "currency": "Währung"},
        "counterparty": "Empfänger",
        "purpose": "Verwendungszweck",
        "sign_flip": True,
        "nullable_field": None,
        "column_order": [3, 1, 2],
        "meta": {"source": "N26", "ungeprueft": False, "skipped_rows": 4},
        "größe": "groß-äöü",
    }

    with factory() as session:
        template = ImportTemplate(
            name="N26 CSV",
            kind="csv",
            mapping=mapping,
            delimiter=";",
            encoding="utf-8",
            date_format="TT.MM.JJJJ",
            decimal_separator=",",
        )
        session.add(template)
        session.commit()
        template_id = template.id

    with factory() as session:
        retrieved = session.query(ImportTemplate).filter_by(id=template_id).one()
        assert retrieved.mapping == mapping


def test_import_template_account_id_nullable(factory) -> None:  # type: ignore[no-untyped-def]
    """Eine kontounabhängige Vorlage hat account_id NULL (F-IMP-05)."""
    with factory() as session:
        template = make_csv_template(account_id=None)
        session.add(template)
        session.commit()
        template_id = template.id

    with factory() as session:
        retrieved = session.query(ImportTemplate).filter_by(id=template_id).one()
        assert retrieved.account_id is None
        assert retrieved.kind == "csv"
        assert retrieved.delimiter == ";"
        assert retrieved.encoding == "utf-8"
        assert retrieved.date_format == "TT.MM.JJJJ"
        assert retrieved.decimal_separator == ","
        assert retrieved.version == 1


def test_import_template_json_kind_without_csv_options(factory) -> None:  # type: ignore[no-untyped-def]
    """Eine JSON-Vorlage erlaubt NULL-Formatoptionen (F-IMP-01)."""
    with factory() as session:
        template = ImportTemplate(
            name="Eigener JSON-Export",
            kind="json",
            mapping={"date": "bookingDate", "amount": "amount"},
        )
        session.add(template)
        session.commit()
        template_id = template.id

    with factory() as session:
        retrieved = session.query(ImportTemplate).filter_by(id=template_id).one()
        assert retrieved.kind == "json"
        assert retrieved.delimiter is None
        assert retrieved.encoding is None
        assert retrieved.date_format is None
        assert retrieved.decimal_separator is None
        assert retrieved.account_id is None


def test_import_template_rejects_invalid_kind(factory) -> None:  # type: ignore[no-untyped-def]
    """kind-Check lehnt andere Werte als csv|json ab (F-IMP-01)."""
    with factory() as session:
        template = make_csv_template()
        template.kind = "excel"  # Nicht erlaubt
        session.add(template)
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_template_rejects_unknown_account(factory) -> None:  # type: ignore[no-untyped-def]
    """FK-Verletzung (unbekannte account_id) wird abgelehnt."""
    with factory() as session:
        template = make_csv_template(account_id=9999)  # Nicht existierende Konto-ID
        session.add(template)
        with pytest.raises(IntegrityError):
            session.commit()


def test_account_delete_sets_template_account_id_null(factory) -> None:  # type: ignore[no-untyped-def]
    """Konto löschen setzt account_id der Vorlage auf NULL (ON DELETE SET NULL)."""
    with factory() as session:
        account = make_test_account()
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        template = make_csv_template(account_id=account_id)
        session.add(template)
        session.commit()
        template_id = template.id

    with factory() as session:
        session.execute(delete(Account).where(Account.id == account_id))
        session.commit()

    with factory() as session:
        retrieved = session.query(ImportTemplate).filter_by(id=template_id).one()
        assert retrieved.mapping == {"date": "Buchungstag", "amount.value": "Betrag"}
        assert retrieved.account_id is None


def test_import_template_version_increments(factory) -> None:  # type: ignore[no-untyped-def]
    """Version steigt bei Update der Vorlage (F-CON-01)."""
    with factory() as session:
        template = make_csv_template()
        session.add(template)
        session.commit()
        template_id = template.id
        assert template.version == 1

    with factory() as session:
        retrieved = session.query(ImportTemplate).filter_by(id=template_id).one()
        retrieved.name = "N26 CSV (korrigiert)"
        session.commit()

    with factory() as session:
        retrieved = session.query(ImportTemplate).filter_by(id=template_id).one()
        assert retrieved.version == 2
