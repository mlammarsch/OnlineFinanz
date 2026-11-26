"""Tests fuer die PIN-Verschluesselung mit Fernet.

Referenziert N-SEC-01 (PIN nur verschluesselt, Schluesseldatei getrennt),
N-SEC-02 (keine Geheimnisse in Fehlern, repr oder Logs) und F-SYN-08
(PIN je Bankzugang optional verschluesselt speichern). Alle Werte sind
erfundene Fantasiedaten, keine echten Secrets.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from finue.config import Settings, get_settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.logging_setup import configure_logging
from finue.models.bank import Bank, BankAccess
from finue.services.pin_crypto import (
    InvalidKeyError,
    KeyFileExistsError,
    KeyFileMissingError,
    PinCipher,
    PinDecryptionError,
    create_dev_key_file,
)

FANTASIE_PIN = "FANTASIE-PIN-4711"


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
def restore_root_logger() -> Iterator[None]:
    """Stelle Handler und Level des Root-Loggers nach dem Test wieder her."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


class TestRoundtrip:
    """N-SEC-01, F-SYN-08: Verschluesselung und Entschluesselung."""

    def test_encrypt_decrypt_roundtrip(self, cipher: PinCipher) -> None:
        """N-SEC-01: PIN kommt nach dem Roundtrip unveraendert zurueck."""
        token = cipher.encrypt_pin(FANTASIE_PIN)

        assert isinstance(token, bytes)
        assert cipher.decrypt_pin(token) == FANTASIE_PIN

    def test_token_contains_no_plaintext(self, cipher: PinCipher) -> None:
        """N-SEC-01: Das Token enthaelt die PIN nicht im Klartext."""
        token = cipher.encrypt_pin(FANTASIE_PIN)

        assert token != FANTASIE_PIN.encode("utf-8")
        assert FANTASIE_PIN.encode("utf-8") not in token

    def test_unicode_pin_roundtrip(self, cipher: PinCipher) -> None:
        """N-SEC-01: UTF-8-PINs (Fantasiewert) bleiben erhalten."""
        pin = "Fantasie-PIN-\u00e4\u00f6\u00fc"
        assert cipher.decrypt_pin(cipher.encrypt_pin(pin)) == pin

    def test_uses_settings_key_file_from_environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """N-SEC-01: Der Pfad kommt aus Settings.key_file (Umgebungsvariable)."""
        path = tmp_path / "env.key"
        create_dev_key_file(path)
        monkeypatch.setenv("KEY_FILE", str(path))
        get_settings.cache_clear()
        try:
            cipher = PinCipher()
            token = cipher.encrypt_pin(FANTASIE_PIN)
            assert cipher.decrypt_pin(token) == FANTASIE_PIN
        finally:
            get_settings.cache_clear()


class TestFehler:
    """N-SEC-01, N-SEC-02: Eigene Fehler ohne Geheimnisinhalt."""

    def test_wrong_key_raises_decryption_error(self, tmp_path: Path, key_file: Path) -> None:
        """N-SEC-01: Ein anderer Schluessel kann das Token nicht entschluesseln."""
        other_key_file = tmp_path / "other.key"
        create_dev_key_file(other_key_file)
        token = PinCipher(settings=Settings(key_file=key_file)).encrypt_pin(FANTASIE_PIN)

        with pytest.raises(PinDecryptionError) as excinfo:
            PinCipher(settings=Settings(key_file=other_key_file)).decrypt_pin(token)

        message = str(excinfo.value)
        assert FANTASIE_PIN not in message
        assert key_file.read_text(encoding="ascii").strip() not in message
        assert other_key_file.read_text(encoding="ascii").strip() not in message

    def test_manipulated_token_raises_decryption_error(self, cipher: PinCipher) -> None:
        """N-SEC-01: Ein manipuliertes Token wird abgelehnt."""
        token = bytearray(cipher.encrypt_pin(FANTASIE_PIN))
        token[-1] = ord("A") if token[-1] != ord("A") else ord("B")

        with pytest.raises(PinDecryptionError) as excinfo:
            cipher.decrypt_pin(bytes(token))

        assert FANTASIE_PIN not in str(excinfo.value)

    def test_missing_key_file_raises(self, tmp_path: Path) -> None:
        """N-SEC-01: Eine fehlende Schluesseldatei bricht mit klarer Meldung ab."""
        missing = tmp_path / "fehlt.key"

        with pytest.raises(KeyFileMissingError) as excinfo:
            PinCipher(settings=Settings(key_file=missing))

        assert "fehlt.key" in str(excinfo.value)

    def test_invalid_key_file_raises(self, tmp_path: Path) -> None:
        """N-SEC-01: Ungueltiger Schluesselinhalt bricht mit klarer Meldung ab."""
        invalid = tmp_path / "kaputt.key"
        invalid.write_bytes(b"kein-gueltiger-schluessel")
        raw = invalid.read_bytes()

        with pytest.raises(InvalidKeyError) as excinfo:
            PinCipher(settings=Settings(key_file=invalid))

        message = str(excinfo.value)
        assert "kaputt.key" in message
        assert raw.decode("ascii") not in message

    def test_empty_key_file_raises(self, tmp_path: Path) -> None:
        """N-SEC-01: Eine leere Schluesseldatei ist ungueltig."""
        empty = tmp_path / "leer.key"
        empty.write_bytes(b"   \n")

        with pytest.raises(InvalidKeyError):
            PinCipher(settings=Settings(key_file=empty))

    def test_garbage_token_raises_decryption_error(self, cipher: PinCipher) -> None:
        """N-SEC-01: Kein gueltiges Fernet-Token fuehrt zu PinDecryptionError."""
        with pytest.raises(PinDecryptionError):
            cipher.decrypt_pin(b"kein-token")

    def test_repr_and_str_are_secret_free(self, cipher: PinCipher, key_file: Path) -> None:
        """N-SEC-02: repr/str enthalten weder PIN noch Schluessel."""
        key_text = key_file.read_text(encoding="ascii").strip()
        token_text = cipher.encrypt_pin(FANTASIE_PIN).decode("ascii")

        for text in (repr(cipher), str(cipher)):
            assert FANTASIE_PIN not in text
            assert key_text not in text
            assert token_text not in text

    def test_error_repr_is_secret_free(self, tmp_path: Path, key_file: Path) -> None:
        """N-SEC-02: Auch die Fehler-Repraesentation enthaelt kein Geheimnis."""
        key_text = key_file.read_text(encoding="ascii").strip()
        with pytest.raises(KeyFileMissingError) as excinfo:
            PinCipher(settings=Settings(key_file=tmp_path / "fehlt.key"))

        assert FANTASIE_PIN not in repr(excinfo.value)
        assert key_text not in repr(excinfo.value)


class TestDevKeyFile:
    """N-SEC-01, N-OPS-07: Entwicklungsschluessel nie ueberschreiben."""

    def test_create_key_file(self, tmp_path: Path) -> None:
        """N-SEC-01: Die Hilfsfunktion legt einen gueltigen Fernet-Schluessel an."""
        path = tmp_path / "unterordner" / "dev.key"

        created = create_dev_key_file(path)

        assert created == path
        key = path.read_bytes().strip()
        assert key
        Fernet(key)  # Konstruktion validiert Laenge und Base64

    def test_existing_key_file_is_never_overwritten(self, tmp_path: Path) -> None:
        """N-SEC-01: Ein zweiter Aufruf laesst die vorhandene Datei unveraendert."""
        path = tmp_path / "dev.key"
        create_dev_key_file(path)
        first = path.read_bytes()

        with pytest.raises(KeyFileExistsError):
            create_dev_key_file(path)

        assert path.read_bytes() == first

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-Dateirechte; Windows kennt sie nicht")
    def test_key_file_has_restricted_permissions(self, tmp_path: Path) -> None:
        """N-OPS-07: Die Schluesseldatei ist nur fuer den Eigentuemer lesbar."""
        path = tmp_path / "dev.key"
        create_dev_key_file(path)

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0


class TestKeineKlartextPins:
    """N-SEC-01, N-SEC-02: Keine PIN in DB-Zeile, Modell oder Log."""

    def test_no_plaintext_pin_in_db_row(self, cipher: PinCipher) -> None:
        """N-SEC-01: Die DB-Zeile enthaelt nur das Token, nie die PIN."""
        engine = create_db_engine("sqlite://")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        try:
            with factory() as session:
                bank = Bank(blz="12340000", name="Krypto-Testbank")
                session.add(bank)
                session.flush()
                access = BankAccess(
                    bank_id=bank.id,
                    login_name="fantasie@example.invalid",
                    pin_encrypted=cipher.encrypt_pin(FANTASIE_PIN),
                    tan_method="900",
                )
                session.add(access)
                session.commit()

                stored = session.execute(select(BankAccess.pin_encrypted)).scalar_one()
                assert isinstance(stored, bytes)
                assert FANTASIE_PIN.encode("utf-8") not in stored
                assert FANTASIE_PIN not in repr(access)
                assert not hasattr(access, "pin")
        finally:
            engine.dispose()

    def test_model_has_no_plaintext_pin_column(self) -> None:
        """N-SEC-01: Das Modell kennt nur pin_encrypted, kein Klartextfeld."""
        columns = {column.name for column in BankAccess.__table__.columns}

        assert "pin_encrypted" in columns
        assert not {"pin", "pin_plain", "pin_clear"} & columns

    def test_log_filter_masks_pin(self, capsys: pytest.CaptureFixture[str], restore_root_logger: None) -> None:
        """N-SEC-02: Der Log-Filter maskiert PINs in Nachricht und extra."""
        configure_logging("INFO")
        logger = logging.getLogger("test_pin_crypto")
        logger.info("Verschluesseln pin=%s", FANTASIE_PIN)
        logger.info("Vorgang", extra={"pin": FANTASIE_PIN, "konto": "Fantasie"})

        out = capsys.readouterr().out
        assert FANTASIE_PIN not in out

        entries = [json.loads(line) for line in out.strip().splitlines()]
        assert any("***" in entry["message"] for entry in entries)
        assert all(entry.get("extra", {}).get("pin") != FANTASIE_PIN for entry in entries)
