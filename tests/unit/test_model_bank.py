"""Tests für die Bank-Modelle.

Referenziert N-DB-01 (kein rohes SQL), N-SEC-01 (PIN nur verschlüsselt,
kein Klartext-PIN-Feld), F-KTO-10 (Banken-Stammdaten mit Icon) und
F-CON-01/F-CON-02 (Versionsspalte für optimistische Sperre).
"""

import time

import pytest

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.bank import Bank, BankAccess


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_create_bank(factory) -> None:  # type: ignore[no-untyped-def]
    """Bank mit Basisdaten kann erstellt werden."""
    with factory() as session:
        bank = Bank(
            blz="12345678",
            name="Test Bank",
            website="https://www.testbank.de",
            fints_url="https://banking.testbank.de/fints",
        )
        session.add(bank)
        session.commit()
        assert bank.id is not None
        assert bank.blz == "12345678"
        assert bank.name == "Test Bank"
        assert bank.website == "https://www.testbank.de"
        assert bank.fints_url == "https://banking.testbank.de/fints"


def test_bank_website_and_fints_url_nullable(factory) -> None:  # type: ignore[no-untyped-def]
    """Bank.website und Bank.fints_url sind optional."""
    with factory() as session:
        bank = Bank(blz="87654321", name="Minimal Bank")
        session.add(bank)
        session.commit()
        assert bank.website is None
        assert bank.fints_url is None


def test_create_bank_access(factory) -> None:  # type: ignore[no-untyped-def]
    """BankAccess mit verschlüsselter PIN und TAN-Methode kann erstellt werden."""
    with factory() as session:
        bank = Bank(blz="11111111", name="Access Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user@example.com",
            pin_encrypted=b"encrypted_pin_bytes",
            tan_method="900",
        )
        session.add(access)
        session.commit()
        assert access.id is not None
        assert access.bank_id == bank_id
        assert access.login_name == "user@example.com"
        assert access.pin_encrypted == b"encrypted_pin_bytes"
        assert access.tan_method == "900"


def test_bank_access_pin_encrypted_nullable(factory) -> None:  # type: ignore[no-untyped-def]
    """N-SEC-01: BankAccess.pin_encrypted ist nullable."""
    with factory() as session:
        bank = Bank(blz="22222222", name="PIN Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user2@example.com",
            tan_method="901",
        )
        session.add(access)
        session.commit()
        assert access.pin_encrypted is None


def test_bank_access_tan_method_nullable(factory) -> None:  # type: ignore[no-untyped-def]
    """BankAccess.tan_method ist optional."""
    with factory() as session:
        bank = Bank(blz="33333333", name="TAN Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user3@example.com",
        )
        session.add(access)
        session.commit()
        assert access.tan_method is None


def test_no_plaintext_pin_attribute(factory) -> None:  # type: ignore[no-untyped-def]
    """N-SEC-01: BankAccess hat kein Klartext-PIN-Feld pin oder pin_plain."""
    with factory() as session:
        bank = Bank(blz="44444444", name="Plaintext Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user4@example.com",
        )
        session.add(access)
        session.commit()

        # Stelle sicher, dass es keine plain-text PIN Attribute gibt
        assert not hasattr(access, "pin")
        assert not hasattr(access, "pin_plain")


def test_bank_access_relation(factory) -> None:  # type: ignore[no-untyped-def]
    """Bank und BankAccess sind bidirektional verknüpft."""
    with factory() as session:
        bank = Bank(blz="55555555", name="Relation Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access1 = BankAccess(
            bank_id=bank_id,
            login_name="user5a@example.com",
        )
        access2 = BankAccess(
            bank_id=bank_id,
            login_name="user5b@example.com",
        )
        session.add(access1)
        session.add(access2)
        session.commit()

    with factory() as session:
        bank = session.get(Bank, bank_id)
        assert bank is not None
        assert len(bank.accesses) == 2
        assert bank.accesses[0].login_name == "user5a@example.com"
        assert bank.accesses[1].login_name == "user5b@example.com"


def test_bank_access_version_increments_on_update(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: BankAccess.version startet bei 1 und steigt bei Update."""
    with factory() as session:
        bank = Bank(blz="66666666", name="Version Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user6@example.com",
        )
        session.add(access)
        session.commit()
        assert access.version == 1
        access_id = access.id

    with factory() as session:
        access = session.get(BankAccess, access_id)
        assert access is not None
        assert access.version == 1
        access.login_name = "updated@example.com"
        session.commit()
        assert access.version == 2


def test_bank_access_timestamps(factory) -> None:  # type: ignore[no-untyped-def]
    """BankAccess hat created_at und updated_at Zeitstempel."""
    with factory() as session:
        bank = Bank(blz="77777777", name="Timestamp Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user7@example.com",
        )
        session.add(access)
        session.commit()
        assert access.created_at is not None
        assert access.updated_at is not None
        assert access.created_at == access.updated_at
        access_id = access.id

    with factory() as session:
        access = session.get(BankAccess, access_id)
        assert access is not None
        created = access.created_at
        time.sleep(0.01)
        access.login_name = "updated7@example.com"
        session.commit()
        assert access.updated_at > created


def test_delete_bank_cascades_to_bank_access(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-01: Löschen einer Bank entfernt auch ihre BankAccess-Einträge (Cascade)."""
    with factory() as session:
        bank = Bank(blz="88888888", name="Cascade Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access1 = BankAccess(
            bank_id=bank_id,
            login_name="user8a@example.com",
        )
        access2 = BankAccess(
            bank_id=bank_id,
            login_name="user8b@example.com",
        )
        session.add(access1)
        session.add(access2)
        session.commit()

    with factory() as session:
        bank = session.get(Bank, bank_id)
        assert bank is not None
        assert len(bank.accesses) == 2
        session.delete(bank)
        session.commit()

    with factory() as session:
        # Nach dem Löschen der Bank sollte es keine BankAccess-Einträge mehr geben
        accesses = session.query(BankAccess).filter_by(bank_id=bank_id).all()
        assert len(accesses) == 0


def test_bank_access_references_correct_bank(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-01: BankAccess.bank zeigt auf die richtige Bank (Gegenrichtung)."""
    with factory() as session:
        bank1 = Bank(blz="99999991", name="Bank A")
        bank2 = Bank(blz="99999992", name="Bank B")
        session.add(bank1)
        session.add(bank2)
        session.commit()
        bank1_id = bank1.id
        bank2_id = bank2.id

    with factory() as session:
        access1 = BankAccess(
            bank_id=bank1_id,
            login_name="user_a@example.com",
        )
        access2 = BankAccess(
            bank_id=bank2_id,
            login_name="user_b@example.com",
        )
        session.add(access1)
        session.add(access2)
        session.commit()
        access1_id = access1.id
        access2_id = access2.id

    with factory() as session:
        access1 = session.get(BankAccess, access1_id)
        access2 = session.get(BankAccess, access2_id)
        assert access1 is not None
        assert access2 is not None
        # Stelle sicher, dass jeder Zugang auf die richtige Bank zeigt
        assert access1.bank.id == bank1_id
        assert access1.bank.name == "Bank A"
        assert access2.bank.id == bank2_id
        assert access2.bank.name == "Bank B"


def test_bank_version_increments_on_update(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-02: Bank.version startet bei 1 und steigt bei Update."""
    with factory() as session:
        bank = Bank(blz="10101010", name="Version Test Bank")
        session.add(bank)
        session.commit()
        assert bank.version == 1
        bank_id = bank.id

    with factory() as session:
        bank = session.get(Bank, bank_id)
        assert bank is not None
        bank.name = "Umbenannte Bank"
        session.commit()
        assert bank.version == 2


def test_bank_icon_columns(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-10: Bank.icon und Bank.icon_mime sind optional und speicherbar."""
    with factory() as session:
        bank = Bank(blz="20202020", name="Icon Test Bank")
        session.add(bank)
        session.commit()
        assert bank.icon is None
        assert bank.icon_mime is None
        bank_id = bank.id

    with factory() as session:
        bank = session.get(Bank, bank_id)
        assert bank is not None
        bank.icon = b"\x89PNG\r\n\x1a\n"
        bank.icon_mime = "image/png"
        session.commit()

    with factory() as session:
        bank = session.get(Bank, bank_id)
        assert bank is not None
        assert bank.icon == b"\x89PNG\r\n\x1a\n"
        assert bank.icon_mime == "image/png"
