"""Tests für die Konto-Modelle.

Referenziert N-DB-01 (kein rohes SQL), N-DB-02 (Beträge in Cent,
Zeitstempel in UTC), F-KTO-01 (Konten-Basisdaten), F-KTO-02
(include_in_total), F-KTO-03 (Gruppe optional, Löschen setzt auf NULL),
F-KTO-06 (Archivierung über active-Flag) und F-CON-01 (Versionsspalte für
optimistische Sperre).
"""

import subprocess
import sys
import time

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account, AccountGroup
from finue.models.bank import Bank, BankAccess


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_create_account_group(factory) -> None:  # type: ignore[no-untyped-def]
    """Kontengruppe kann mit Namen und sort_order erstellt werden."""
    with factory() as session:
        group = AccountGroup(
            name="Girokonten",
            sort_order=1,
        )
        session.add(group)
        session.commit()
        assert group.id is not None
        assert group.name == "Girokonten"
        assert group.sort_order == 1


def test_account_group_default_sort_order(factory) -> None:  # type: ignore[no-untyped-def]
    """AccountGroup.sort_order hat Standardwert 0."""
    with factory() as session:
        group = AccountGroup(name="Sparbücher")
        session.add(group)
        session.commit()
        assert group.sort_order == 0


def test_create_account_without_group(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-01: Konto ohne Gruppe kann erstellt werden."""
    with factory() as session:
        account = Account(
            name="Mein Sparkonto",
            iban="DE89370400440532013000",
            currency="EUR",
            source="fints",
            account_type="Savings",
            opening_balance_cents=50000,
        )
        session.add(account)
        session.commit()
        assert account.id is not None
        assert account.name == "Mein Sparkonto"
        assert account.iban == "DE89370400440532013000"
        assert account.currency == "EUR"
        assert account.source == "fints"
        assert account.account_type == "Savings"
        assert account.opening_balance_cents == 50000
        assert account.group_id is None
        assert account.group is None


def test_create_account_with_group(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-01, F-KTO-03: Konto kann einer Gruppe zugeordnet werden."""
    with factory() as session:
        group = AccountGroup(name="Meine Konten")
        session.add(group)
        session.commit()
        group_id = group.id

    with factory() as session:
        account = Account(
            name="Girokonto",
            iban="DE00120300000000202030",
            currency="EUR",
            source="fints",
            group_id=group_id,
            opening_balance_cents=100000,
        )
        session.add(account)
        session.commit()
        assert account.group_id == group_id
        account_id = account.id

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.group is not None
        assert account.group.name == "Meine Konten"


def test_account_group_accounts_relation(factory) -> None:  # type: ignore[no-untyped-def]
    """Bidirektionale Relation zwischen AccountGroup und Account."""
    with factory() as session:
        group = AccountGroup(name="Testgruppe")
        session.add(group)
        session.commit()
        group_id = group.id

    with factory() as session:
        account1 = Account(
            name="Konto 1",
            currency="EUR",
            source="datei",
            group_id=group_id,
        )
        account2 = Account(
            name="Konto 2",
            currency="EUR",
            source="fints",
            group_id=group_id,
        )
        session.add(account1)
        session.add(account2)
        session.commit()

    with factory() as session:
        group = session.get(AccountGroup, group_id)
        assert group is not None
        assert len(group.accounts) == 2
        assert group.accounts[0].name == "Konto 1"
        assert group.accounts[1].name == "Konto 2"


def test_account_source_check_valid_values(factory) -> None:  # type: ignore[no-untyped-def]
    """Kontenquelle muss 'fints' oder 'datei' sein."""
    with factory() as session:
        account_fints = Account(
            name="FinTS Konto",
            currency="EUR",
            source="fints",
        )
        account_datei = Account(
            name="Datei Konto",
            currency="EUR",
            source="datei",
        )
        session.add(account_fints)
        session.add(account_datei)
        session.commit()
        assert account_fints.source == "fints"
        assert account_datei.source == "datei"


def test_account_source_check_invalid_value(factory) -> None:  # type: ignore[no-untyped-def]
    """Ungültige source-Werte werden abgelehnt (N-DB-01)."""
    with factory() as session:
        account = Account(
            name="Ungültiges Konto",
            currency="EUR",
            source="xyz",
        )
        session.add(account)
        with pytest.raises(IntegrityError):
            session.commit()


def test_account_delete_group_sets_group_id_null(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-03: Gruppe löschen setzt group_id auf NULL."""
    with factory() as session:
        group = AccountGroup(name="Gruppe zum Löschen")
        session.add(group)
        session.commit()
        group_id = group.id

    with factory() as session:
        account = Account(
            name="Konto mit Gruppe",
            currency="EUR",
            source="fints",
            group_id=group_id,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        group = session.get(AccountGroup, group_id)
        assert group is not None
        session.delete(group)
        session.commit()

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.group_id is None


def test_account_with_bank_access(factory) -> None:  # type: ignore[no-untyped-def]
    """Konto kann mit BankAccess verknüpft sein."""
    with factory() as session:
        bank = Bank(blz="12345678", name="Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="user@example.com",
        )
        session.add(access)
        session.commit()
        access_id = access.id

    with factory() as session:
        account = Account(
            name="FinTS Konto",
            currency="EUR",
            source="fints",
            bank_access_id=access_id,
        )
        session.add(account)
        session.commit()
        assert account.bank_access_id == access_id
        account_id = account.id

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.bank_access is not None
        assert account.bank_access.login_name == "user@example.com"


def test_delete_bank_access_with_account_fails(factory) -> None:  # type: ignore[no-untyped-def]
    """Löschen eines BankAccess mit verknüpftem Konto schlägt fehl (N-DB-01)."""
    with factory() as session:
        bank = Bank(blz="87654321", name="Delete Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="delete_user@example.com",
        )
        session.add(access)
        session.commit()
        access_id = access.id

    with factory() as session:
        account = Account(
            name="Geschütztes Konto",
            currency="EUR",
            source="fints",
            bank_access_id=access_id,
        )
        session.add(account)
        session.commit()

    with factory() as session:
        access = session.get(BankAccess, access_id)
        assert access is not None
        session.delete(access)
        with pytest.raises(IntegrityError):
            session.commit()


def test_account_group_version_increments_on_update(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: AccountGroup.version startet bei 1 und steigt bei Update."""
    with factory() as session:
        group = AccountGroup(name="Versionstest")
        session.add(group)
        session.commit()
        assert group.version == 1
        group_id = group.id

    with factory() as session:
        group = session.get(AccountGroup, group_id)
        assert group is not None
        assert group.version == 1
        group.name = "Versionstest 2"
        session.commit()
        assert group.version == 2


def test_account_version_increments_on_update(factory) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: Account.version startet bei 1 und steigt bei Update."""
    with factory() as session:
        account = Account(
            name="Versionstest Konto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        assert account.version == 1
        account_id = account.id

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.version == 1
        account.name = "Versionstest Konto 2"
        session.commit()
        assert account.version == 2


def test_account_icon_roundtrip(factory) -> None:  # type: ignore[no-untyped-def]
    """Icon-Bytes werden korrekt gespeichert und gelesen (N-OPS-02)."""
    icon_data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    with factory() as session:
        account = Account(
            name="Konto mit Icon",
            currency="EUR",
            source="fints",
            icon=icon_data,
            icon_mime="image/png",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.icon == icon_data
        assert account.icon_mime == "image/png"


def test_account_default_values(factory) -> None:  # type: ignore[no-untyped-def]
    """Standardwerte sind korrekt gesetzt (F-KTO-02, F-KTO-06)."""
    with factory() as session:
        account = Account(
            name="Minimales Konto",
            source="fints",
        )
        session.add(account)
        session.commit()
        assert account.currency == "EUR"
        assert account.include_in_total is True
        assert account.active is True
        assert account.opening_balance_cents == 0
        assert account.balance_cents is None
        assert account.balance_at is None
        assert account.iban is None
        assert account.account_type is None
        assert account.icon is None
        assert account.icon_mime is None
        assert account.bank_access_id is None
        assert account.group_id is None


def test_account_include_in_total(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-02: include_in_total kann explizit gesetzt werden."""
    with factory() as session:
        account_included = Account(
            name="Eingeschlossenes Konto",
            currency="EUR",
            source="fints",
            include_in_total=True,
        )
        account_excluded = Account(
            name="Ausgeschlossenes Konto",
            currency="EUR",
            source="datei",
            include_in_total=False,
        )
        session.add(account_included)
        session.add(account_excluded)
        session.commit()
        assert account_included.include_in_total is True
        assert account_excluded.include_in_total is False


def test_account_active_archiving(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-06: Konten können archiviert werden über active=False."""
    with factory() as session:
        account = Account(
            name="Archiviertes Konto",
            currency="EUR",
            source="fints",
            active=True,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.active is True
        account.active = False
        session.commit()

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.active is False


def test_account_balance_in_cents(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-02: Beträge werden als ganze Cent gespeichert."""
    with factory() as session:
        account = Account(
            name="Konto mit Saldo",
            currency="EUR",
            source="fints",
            opening_balance_cents=123456,
            balance_cents=234567,
        )
        session.add(account)
        session.commit()
        assert account.opening_balance_cents == 123456
        assert account.balance_cents == 234567


def test_account_timestamps(factory) -> None:  # type: ignore[no-untyped-def]
    """Account hat created_at und updated_at Zeitstempel (N-DB-02)."""
    with factory() as session:
        account = Account(
            name="Zeitstempel-Konto",
            currency="EUR",
            source="fints",
        )
        session.add(account)
        session.commit()
        assert account.created_at is not None
        assert account.updated_at is not None
        assert account.created_at == account.updated_at
        account_id = account.id

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        created = account.created_at
        time.sleep(0.01)
        account.name = "Aktualisiertes Konto"
        session.commit()
        assert account.updated_at > created


def test_account_group_timestamps(factory) -> None:  # type: ignore[no-untyped-def]
    """AccountGroup hat created_at und updated_at Zeitstempel (N-DB-02)."""
    with factory() as session:
        group = AccountGroup(name="Gruppe mit Zeitstempel")
        session.add(group)
        session.commit()
        assert group.created_at is not None
        assert group.updated_at is not None
        assert group.created_at == group.updated_at
        group_id = group.id

    with factory() as session:
        group = session.get(AccountGroup, group_id)
        assert group is not None
        created = group.created_at
        time.sleep(0.01)
        group.name = "Aktualisierte Gruppe"
        session.commit()
        assert group.updated_at > created


def test_bank_access_accounts_relation(factory) -> None:  # type: ignore[no-untyped-def]
    """Bidirektionale Relation zwischen BankAccess und Account."""
    with factory() as session:
        bank = Bank(blz="99999999", name="Relation Test Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="relation_user@example.com",
        )
        session.add(access)
        session.commit()
        access_id = access.id

    with factory() as session:
        account1 = Account(
            name="Konto A",
            currency="EUR",
            source="fints",
            bank_access_id=access_id,
        )
        account2 = Account(
            name="Konto B",
            currency="EUR",
            source="fints",
            bank_access_id=access_id,
        )
        session.add(account1)
        session.add(account2)
        session.commit()

    with factory() as session:
        access = session.get(BankAccess, access_id)
        assert access is not None
        assert len(access.accounts) == 2
        assert access.accounts[0].name == "Konto A"
        assert access.accounts[1].name == "Konto B"


def test_bank_access_import_alone_configures_mappers() -> None:
    """N-DB-01: Bank-Import allein registriert alle ORM-Mappers."""
    # Dieser Test läuft in einem isolierten Subprocess, um sicherzustellen,
    # dass `from finue.models.bank import BankAccess` allein ausreicht,
    # um configure_mappers() erfolgreich zu starten.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from finue.models.bank import BankAccess; "
            "from sqlalchemy.orm import configure_mappers; "
            "configure_mappers()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_account_group_delete_via_core_sets_group_id_null(factory) -> None:  # type: ignore[no-untyped-def]
    """F-KTO-03: Gruppe löschen mit Core-Delete setzt group_id auf NULL."""
    with factory() as session:
        group = AccountGroup(name="Gruppe zum Core-Löschen")
        session.add(group)
        session.commit()
        group_id = group.id

    with factory() as session:
        account = Account(
            name="Konto mit Gruppe",
            currency="EUR",
            source="fints",
            group_id=group_id,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    with factory() as session:
        # Core-Delete: SQLAlchemy Core statt ORM delete
        session.execute(delete(AccountGroup).where(AccountGroup.id == group_id))
        session.commit()
        session.expire_all()

    with factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        assert account.group_id is None


def test_delete_bank_with_access_and_account_fails(factory) -> None:  # type: ignore[no-untyped-def]
    """N-DB-01: Bank-Löschen mit BankAccess und Konto schlägt fehl (IntegrityError)."""
    with factory() as session:
        bank = Bank(blz="11112222", name="Delete Fail Bank")
        session.add(bank)
        session.commit()
        bank_id = bank.id

    with factory() as session:
        access = BankAccess(
            bank_id=bank_id,
            login_name="fail_user@example.com",
        )
        session.add(access)
        session.commit()
        access_id = access.id

    with factory() as session:
        account = Account(
            name="Geschütztes Konto",
            currency="EUR",
            source="fints",
            bank_access_id=access_id,
        )
        session.add(account)
        session.commit()

    with factory() as session:
        bank = session.get(Bank, bank_id)
        assert bank is not None
        session.delete(bank)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
