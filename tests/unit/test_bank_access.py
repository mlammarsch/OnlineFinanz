"""Tests fuer den Bankzugang-Service mit PIN-Option (T-071).

Referenziert F-SYN-08 (PIN je Bankzugang verschluesselt speichern oder bei
jedem Sync abfragen), N-SEC-01 (PINs nur verschluesselt in der DB), N-SEC-02
(keine PIN in DB, Logs oder Fehlern) und F-CON-01/F-CON-02 (Versionspruefung
bei Aenderungen). Alle Werte sind erfundene Fantasiedaten, keine echten
Secrets und keine echten Bankzugriffe (D8).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.bank import Bank, BankAccess
from finue.services.bank_access import (
    BankAccessVersionConflict,
    PinRequiredError,
    create_access,
    delete_access,
    get_access,
    list_accesses,
    resolve_pin,
    update_access,
)
from finue.services.pin_crypto import (
    PinCipher,
    PinDecryptionError,
    create_dev_key_file,
)

FANTASIE_PIN = "FANTASIE-PIN-4711"
FANTASIE_PIN_NEU = "FANTASIE-PIN-815"
BLZ = "12340000"
BLZ_ZWEIT = "99990000"
LOGIN = "fantasie@example.invalid"


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    """Isolierte In-Memory-SQLite-Datenbank mit allen ORM-Tabellen."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def key_file(tmp_path: Path) -> Path:
    """Erzeuge eine gueltige Entwicklungsschluesseldatei."""
    path = tmp_path / "dev.key"
    create_dev_key_file(path)
    return path


@pytest.fixture()
def cipher(key_file: Path) -> PinCipher:
    """PinCipher mit einer isolierten Test-Schluesseldatei."""
    return PinCipher(settings=Settings(key_file=key_file))


@pytest.fixture()
def other_cipher(tmp_path: Path) -> PinCipher:
    """PinCipher mit einem anderen Schluessel (fuer Fehlerfaelle)."""
    path = tmp_path / "other.key"
    create_dev_key_file(path)
    return PinCipher(settings=Settings(key_file=path))


def _create(
    session: Session,
    cipher: PinCipher | None,
    **overrides: object,
) -> BankAccess:
    """Legt einen Bankzugang mit sinnvollen Fantasiewerten an."""
    params: dict[str, object] = {
        "blz": BLZ,
        "login_name": LOGIN,
        "cipher": cipher,
        "tan_method": "900",
        "store_pin": True,
        "pin": FANTASIE_PIN,
        "bank_name": "Fantasiebank",
    }
    params.update(overrides)
    return create_access(session, **params)


def _pin_is_stored(access: BankAccess, pin: str, cipher: PinCipher) -> bool:
    """Prueft, ob der Chiffretext die PIN ergibt und nicht im Klartext enthaelt."""
    token = access.pin_encrypted
    assert token is not None
    assert pin.encode("utf-8") not in token
    return cipher.decrypt_pin(token) == pin


class TestCreateUndLesen:
    """F-SYN-08, N-SEC-01: Anlegen, Lesen, Auflisten."""

    def test_create_with_stored_pin(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: store_pin=True speichert die PIN nur verschluesselt."""
        with factory() as session:
            access = _create(session, cipher)

            assert access.id >= 1
            assert access.version == 1
            assert access.login_name == LOGIN
            assert access.tan_method == "900"
            assert access.store_pin is True
            assert _pin_is_stored(access, FANTASIE_PIN, cipher)
            assert resolve_pin(access, None, cipher=cipher) == FANTASIE_PIN

            bank = session.get(Bank, access.bank_id)
            assert bank is not None
            assert bank.blz == BLZ
            assert bank.name == "Fantasiebank"

    def test_create_without_stored_pin_keeps_supplied_pin_out_of_db(self, factory: sessionmaker[Session]) -> None:
        """F-SYN-08, N-SEC-01: store_pin=False speichert auch eine uebergebene PIN nicht."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=FANTASIE_PIN)

            assert access.store_pin is False
            assert access.pin_encrypted is None
            stored = session.execute(select(BankAccess.pin_encrypted)).scalar_one()
            assert stored is None

            assert resolve_pin(access, FANTASIE_PIN) == FANTASIE_PIN

    def test_create_with_stored_pin_requires_pin(self, factory: sessionmaker[Session]) -> None:
        """F-SYN-08: store_pin=True ohne PIN fordert eine PIN an."""
        with factory() as session:
            with pytest.raises(PinRequiredError) as info:
                _create(session, None, pin=None)

            assert str(info.value) == "PIN bitte eingeben."

    def test_create_with_empty_stored_pin_requires_pin(self, factory: sessionmaker[Session]) -> None:
        """F-SYN-08: Eine leere PIN zaehlt als fehlend."""
        with factory() as session:
            with pytest.raises(PinRequiredError):
                _create(session, None, pin="")

    def test_create_reuses_bank_by_blz(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: Eine vorhandene Bank wird ueber die BLZ wiederverwendet."""
        with factory() as session:
            first = _create(session, cipher, login_name="erste@example.invalid")
            second = _create(session, cipher, login_name="zweite@example.invalid")

            assert first.bank_id == second.bank_id
            assert len(list(session.scalars(select(Bank)))) == 1

    def test_create_normalizes_text_fields(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: Leerraum wird entfernt, leeres TAN-Verfahren wird NULL."""
        with factory() as session:
            access = _create(
                session,
                cipher,
                blz=f"  {BLZ}  ",
                login_name=f"  {LOGIN}  ",
                tan_method="   ",
                bank_name="  Fantasiebank  ",
            )

            assert access.login_name == LOGIN
            assert access.tan_method is None
            bank = session.get(Bank, access.bank_id)
            assert bank is not None
            assert bank.blz == BLZ
            assert bank.name == "Fantasiebank"

    def test_get_and_list_accesses(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: Lesen und Auflisten funktionieren stabil nach ID."""
        with factory() as session:
            first = _create(session, cipher, login_name="erste@example.invalid")
            second = _create(session, cipher, blz=BLZ_ZWEIT, login_name="zweite@example.invalid")

            assert get_access(session, first.id) is first
            assert get_access(session, 999_999) is None
            assert [entry.id for entry in list_accesses(session)] == [first.id, second.id]
            assert [entry.id for entry in list_accesses(session, bank_id=second.bank_id)] == [second.id]


class TestUpdateUndVersion:
    """F-CON-01/F-CON-02: Versionspruefung und Feldaenderungen."""

    def test_update_fields_raises_version(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-CON-02: Ein Update erhoeht die Version und aendert nur die Felder."""
        with factory() as session:
            access = _create(session, cipher)

            updated = update_access(
                session,
                access.id,
                access.version,
                login_name="neu@example.invalid",
                tan_method="901",
            )

            assert updated.version == 2
            assert updated.login_name == "neu@example.invalid"
            assert updated.tan_method == "901"
            assert _pin_is_stored(updated, FANTASIE_PIN, cipher)

    def test_update_rejects_stale_version(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-CON-02: Ein Update mit alter Version wird abgewiesen, ohne Aenderung."""
        with factory() as session:
            access = _create(session, cipher)
            update_access(session, access.id, access.version, login_name="neu@example.invalid")

            with pytest.raises(BankAccessVersionConflict) as info:
                update_access(session, access.id, 1, login_name="veraltet@example.invalid")

            assert info.value.access_id == access.id
            assert info.value.expected_version == 1
            assert info.value.current_version == 2

            current = get_access(session, access.id)
            assert current is not None
            assert current.login_name == "neu@example.invalid"

    def test_update_detects_concurrent_change(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-CON-01/02: Wettlauf zwischen Pruefung und Commit liefert Konflikt."""
        with factory() as seed:
            access_id = _create(seed, cipher).id

        with factory() as first, factory() as second:
            loaded_first = get_access(first, access_id)
            loaded_second = get_access(second, access_id)
            assert loaded_first is not None
            assert loaded_second is not None

            update_access(first, access_id, loaded_first.version, login_name="erste@example.invalid")
            with pytest.raises(BankAccessVersionConflict):
                update_access(second, access_id, loaded_second.version, login_name="zweite@example.invalid")

    def test_update_moves_access_to_other_bank(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: Eine BLZ-Aenderung haengt den Zugang an die andere Bank."""
        with factory() as session:
            access = _create(session, cipher)

            updated = update_access(
                session,
                access.id,
                access.version,
                blz=BLZ_ZWEIT,
                bank_name="Zweite Fantasiebank",
            )

            assert updated.bank.blz == BLZ_ZWEIT
            assert updated.bank.name == "Zweite Fantasiebank"
            assert len(list(session.scalars(select(Bank)))) == 2

    def test_delete_requires_current_version(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-CON-02: Loeschen prueft die Version ebenfalls."""
        with factory() as session:
            access = _create(session, cipher)
            update_access(session, access.id, access.version, login_name="neu@example.invalid")

            with pytest.raises(BankAccessVersionConflict):
                delete_access(session, access.id, 1)

            delete_access(session, access.id, 2)
            assert get_access(session, access.id) is None


class TestPinOptionWechsel:
    """F-SYN-08: Wechsel zwischen gespeicherter und je Sync gelieferter PIN."""

    def test_switch_true_to_false_deletes_ciphertext(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: true -> false loescht den Chiffretext."""
        with factory() as session:
            access = _create(session, cipher)
            assert access.pin_encrypted is not None

            updated = update_access(session, access.id, access.version, store_pin=False)

            assert updated.store_pin is False
            assert updated.pin_encrypted is None
            stored = session.execute(select(BankAccess.pin_encrypted)).scalar_one()
            assert stored is None
            assert resolve_pin(updated, FANTASIE_PIN_NEU) == FANTASIE_PIN_NEU

    def test_switch_true_to_false_does_not_store_supplied_pin(
        self, factory: sessionmaker[Session], cipher: PinCipher
    ) -> None:
        """F-SYN-08, N-SEC-01: Beim Abschalten wird eine uebergebene PIN verworfen."""
        with factory() as session:
            access = _create(session, cipher)

            updated = update_access(
                session,
                access.id,
                access.version,
                store_pin=False,
                pin=FANTASIE_PIN_NEU,
            )

            assert updated.pin_encrypted is None
            assert updated.store_pin is False
            with pytest.raises(PinRequiredError):
                resolve_pin(updated, None)

    def test_switch_false_to_true_stores_pin(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: false -> true verlangt und speichert die PIN."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)

            updated = update_access(
                session,
                access.id,
                access.version,
                cipher=cipher,
                store_pin=True,
                pin=FANTASIE_PIN_NEU,
            )

            assert updated.store_pin is True
            assert updated.version == 2
            assert _pin_is_stored(updated, FANTASIE_PIN_NEU, cipher)
            assert resolve_pin(updated, None, cipher=cipher) == FANTASIE_PIN_NEU

    def test_switch_false_to_true_without_pin_raises(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: false -> true ohne PIN laesst den Datensatz unveraendert."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)

            with pytest.raises(PinRequiredError):
                update_access(session, access.id, access.version, store_pin=True, cipher=cipher)

            session.rollback()
            current = get_access(session, access.id)
            assert current is not None
            assert current.store_pin is False
            assert current.version == 1

    def test_true_with_new_pin_reencrypts(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: Eine neue PIN bei store_pin=True ersetzt den Chiffretext."""
        with factory() as session:
            access = _create(session, cipher)
            old_token = access.pin_encrypted

            updated = update_access(
                session,
                access.id,
                access.version,
                cipher=cipher,
                pin=FANTASIE_PIN_NEU,
            )

            assert updated.pin_encrypted is not None
            assert updated.pin_encrypted != old_token
            assert _pin_is_stored(updated, FANTASIE_PIN_NEU, cipher)

    def test_false_with_supplied_pin_never_stores(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08, N-SEC-01: Bei store_pin=False bleibt ein PIN-Aufruf fluechtig."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)

            updated = update_access(
                session,
                access.id,
                access.version,
                cipher=cipher,
                pin=FANTASIE_PIN_NEU,
            )

            assert updated.store_pin is False
            assert updated.pin_encrypted is None

    def test_true_without_ciphertext_and_without_pin_raises(
        self, factory: sessionmaker[Session], cipher: PinCipher
    ) -> None:
        """F-SYN-08: store_pin=True ohne Chiffretext und ohne PIN ist ungueltig."""
        with factory() as session:
            bank = Bank(blz=BLZ, name="Fantasiebank")
            session.add(bank)
            session.flush()
            access = BankAccess(
                bank_id=bank.id,
                login_name=LOGIN,
                pin_encrypted=None,
                store_pin=True,
            )
            session.add(access)
            session.commit()
            session.refresh(access)

            with pytest.raises(PinRequiredError):
                update_access(session, access.id, access.version, cipher=cipher)


class TestResolvePin:
    """F-SYN-08, N-SEC-01: PIN-Aufloesung ohne Speicherung im Klartext."""

    def test_resolve_uses_supplied_pin_when_not_stored(self, factory: sessionmaker[Session]) -> None:
        """F-SYN-08: Bei store_pin=False kommt die PIN aus dem Aufruf."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)

            assert resolve_pin(access, FANTASIE_PIN) == FANTASIE_PIN

    def test_resolve_requires_pin_when_not_stored(self, factory: sessionmaker[Session]) -> None:
        """F-SYN-08: Ohne gespeicherte und ohne gelieferte PIN folgt PinRequiredError."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)

            with pytest.raises(PinRequiredError) as info:
                resolve_pin(access, None)
            assert str(info.value) == "PIN bitte eingeben."

    def test_resolve_uses_stored_pin_and_ignores_supplied(
        self, factory: sessionmaker[Session], cipher: PinCipher
    ) -> None:
        """F-SYN-08: Bei store_pin=True gewinnt der gespeicherte Chiffretext."""
        with factory() as session:
            access = _create(session, cipher)

            assert resolve_pin(access, FANTASIE_PIN_NEU, cipher=cipher) == FANTASIE_PIN

    def test_resolve_with_wrong_key_raises_without_secret(
        self,
        factory: sessionmaker[Session],
        cipher: PinCipher,
        other_cipher: PinCipher,
        key_file: Path,
    ) -> None:
        """N-SEC-01/02: Falscher Schluessel scheitert ohne PIN oder Schluessel im Text."""
        with factory() as session:
            access = _create(session, cipher)
            token = access.pin_encrypted
            assert token is not None

            with pytest.raises(PinDecryptionError) as info:
                resolve_pin(access, None, cipher=other_cipher)

            message = str(info.value)
            assert FANTASIE_PIN not in message
            assert token.decode("ascii") not in message
            assert key_file.read_text(encoding="ascii").strip() not in message


class TestSicherheit:
    """N-SEC-01, N-SEC-02: Keine PIN in DB, Logs, repr oder Fehlern."""

    def test_no_plaintext_pin_in_db_dump(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """N-SEC-01: Ein vollstaendiger Spalten-Dump enthaelt die PIN nicht."""
        with factory() as session:
            access = _create(session, cipher)
            update_access(
                session,
                access.id,
                access.version,
                cipher=cipher,
                pin=FANTASIE_PIN_NEU,
            )

            rows = list(session.scalars(select(BankAccess)))
            assert rows
            for row in rows:
                for column in BankAccess.__table__.columns:
                    value = getattr(row, column.name)
                    assert FANTASIE_PIN not in str(value)
                    assert FANTASIE_PIN_NEU not in str(value)
                    if isinstance(value, bytes):
                        assert FANTASIE_PIN.encode("utf-8") not in value
                        assert FANTASIE_PIN_NEU.encode("utf-8") not in value

    def test_no_pin_in_logs(
        self,
        factory: sessionmaker[Session],
        cipher: PinCipher,
        key_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """N-SEC-02: Kein Log-Eintrag enthaelt PIN oder Schluessel."""
        caplog.set_level(logging.DEBUG)
        secret_key = key_file.read_text(encoding="ascii").strip()

        with factory() as session:
            access = _create(session, cipher)
            update_access(session, access.id, access.version, cipher=cipher, pin=FANTASIE_PIN_NEU)
            resolve_pin(access, None, cipher=cipher)
            updated = update_access(session, access.id, access.version, store_pin=False, pin=FANTASIE_PIN)
            resolve_pin(updated, FANTASIE_PIN)

        assert FANTASIE_PIN not in caplog.text
        assert FANTASIE_PIN_NEU not in caplog.text
        assert secret_key not in caplog.text

    def test_error_messages_are_secret_free(self, factory: sessionmaker[Session]) -> None:
        """N-SEC-02: Fehler enthalten nie eine PIN."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)
            errors = []

            try:
                resolve_pin(access, None)
            except PinRequiredError as exc:
                errors.append(exc)

            try:
                update_access(session, access.id, 99, store_pin=True)
            except BankAccessVersionConflict as exc:
                errors.append(exc)

            assert errors
            for error in errors:
                assert FANTASIE_PIN not in str(error)
                assert FANTASIE_PIN not in repr(error)

    def test_model_and_repr_contain_no_plaintext_pin(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """N-SEC-01/02: Kein Klartextfeld und kein PIN in repr."""
        with factory() as session:
            access = _create(session, cipher)
            columns = {column.name for column in BankAccess.__table__.columns}

            assert "pin_encrypted" in columns
            assert not {"pin", "pin_plain", "pin_clear"} & columns
            assert FANTASIE_PIN not in repr(access)
            assert FANTASIE_PIN not in str(access)


class TestValidierung:
    """F-SYN-08: Ungueltige Eingaben werden vor dem Schreiben abgewiesen."""

    def test_invalid_fields_are_rejected(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-SYN-08: BLZ, Login und TAN-Verfahren werden validiert."""
        with factory() as session:
            with pytest.raises(ValueError):
                _create(session, cipher, blz="")
            with pytest.raises(ValueError):
                _create(session, cipher, blz="123456789")
            with pytest.raises(ValueError):
                _create(session, cipher, login_name="   ")
            with pytest.raises(ValueError):
                _create(session, cipher, tan_method=123)
            with pytest.raises(ValueError):
                _create(session, cipher, store_pin="ja")

    def test_invalid_ids_and_versions_are_rejected(self, factory: sessionmaker[Session], cipher: PinCipher) -> None:
        """F-CON-01: IDs und Versionen muessen positive Ganzzahlen sein."""
        with factory() as session:
            access = _create(session, cipher)

            with pytest.raises(ValueError):
                get_access(session, 0)
            with pytest.raises(ValueError):
                update_access(session, 0, 1, login_name="x")
            with pytest.raises(ValueError):
                update_access(session, access.id, 0, login_name="x")
            with pytest.raises(ValueError):
                delete_access(session, access.id, 0)

    def test_missing_access_is_not_found(self, factory: sessionmaker[Session]) -> None:
        """F-CON-02: Unbekannte IDs fuehren zu LookupError."""
        with factory() as session:
            with pytest.raises(LookupError):
                update_access(session, 999_999, 1, login_name="x")
            with pytest.raises(LookupError):
                delete_access(session, 999_999, 1)

    def test_resolve_rejects_non_text_pin(self, factory: sessionmaker[Session]) -> None:
        """F-SYN-08: Bei store_pin=False muss die gelieferte PIN ein Text sein."""
        with factory() as session:
            access = _create(session, None, store_pin=False, pin=None)

            with pytest.raises(ValueError):
                resolve_pin(access, 123)
