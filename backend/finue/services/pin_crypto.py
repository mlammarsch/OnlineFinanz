"""PIN-Verschluesselung mit Fernet.

Implementiert N-SEC-01 (PINs nur Fernet-verschluesselt in der DB; der Schluessel
liegt in einer Datei, deren Pfad ueber ``Settings.key_file`` kommt und nie in
DB, Repo oder Image), N-SEC-02 (keine PIN und kein Schluessel in Fehlern,
``repr`` oder Logs) und F-SYN-08 (PIN je Bankzugang optional verschluesselt
speichern).

Der Schluessel wird einmal beim Erzeugen von :class:`PinCipher` aus der Datei
gelesen. Fehlt die Datei oder ist sie ungueltig, bricht die Konstruktion mit
einer eigenen Fehlerklasse ab. Die Datei wird nicht automatisch angelegt:
Fuer die Entwicklung gibt es :func:`create_dev_key_file`, das eine vorhandene
Datei nie ueberschreibt und restriktive Rechte setzt, soweit die Plattform es
unterstuetzt.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from finue.config import Settings, get_settings

__all__ = [
    "InvalidKeyError",
    "KeyFileExistsError",
    "KeyFileMissingError",
    "KeyFileWriteError",
    "PinCipher",
    "PinCryptoError",
    "PinDecryptionError",
    "create_dev_key_file",
]


class PinCryptoError(Exception):
    """Basisklasse fuer PIN-Krypto-Fehler.

    Die Meldungen enthalten nie eine PIN, ein Token oder Schluesselmaterial
    (N-SEC-02). Der Pfad zur Schluesseldatei ist kein Geheimnis.
    """


class KeyFileMissingError(PinCryptoError):
    """Die konfigurierte Schluesseldatei fehlt oder ist nicht lesbar."""


class InvalidKeyError(PinCryptoError):
    """Die Schluesseldatei enthaelt keinen gueltigen Fernet-Schluessel."""


class PinDecryptionError(PinCryptoError):
    """Das Token ist manipuliert, ungueltig oder passt nicht zum Schluessel."""


class KeyFileExistsError(PinCryptoError):
    """Die Zieldatei existiert bereits; eine Schluesseldatei wird nie ueberschrieben."""


class KeyFileWriteError(PinCryptoError):
    """Die Schluesseldatei konnte nicht angelegt oder geschrieben werden."""


def _read_fernet_key(key_file: Path) -> bytes:
    """Lies und validiere den Fernet-Schluessel aus ``key_file``.

    Args:
        key_file: Pfad zur Schluesseldatei.

    Returns:
        Der validierte Schluessel als Bytes (ohne umgebende Leerraumzeichen).

    Raises:
        KeyFileMissingError: Datei fehlt oder ist nicht lesbar.
        InvalidKeyError: Datei ist leer oder enthaelt keinen gueltigen Schluessel.
    """
    try:
        raw = key_file.read_bytes()
    except FileNotFoundError:
        raise KeyFileMissingError(f"Schluesseldatei fehlt: {key_file}") from None
    except OSError:
        raise KeyFileMissingError(f"Schluesseldatei nicht lesbar: {key_file}") from None

    key = raw.strip()
    if not key:
        raise InvalidKeyError(f"Schluesseldatei ist leer: {key_file}")

    try:
        Fernet(key)
    except (TypeError, ValueError):
        # Die Originalmeldung kann Schluesselinhalt andeuten; daher ersetzen.
        raise InvalidKeyError(f"Schluesseldatei enthaelt keinen gueltigen Fernet-Schluessel: {key_file}") from None
    return key


def _restrict_permissions(path: Path) -> None:
    """Setze Dateirechte auf 0600, soweit die Plattform das unterstuetzt.

    Unter Windows und auf Dateisystemen ohne POSIX-Rechte bleibt der Aufruf
    wirkungslos, ohne den Ablauf zu brechen (N-OPS-07 "soweit moeglich").
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _unlink_quietly(path: Path) -> None:
    """Entferne eine halb geschriebene Datei, ohne den urspruenglichen Fehler zu verdecken."""
    try:
        path.unlink()
    except OSError:
        pass


def create_dev_key_file(key_file: Path) -> Path:
    """Erzeuge eine neue Fernet-Schluesseldatei fuer die Entwicklung.

    Die Datei wird ausschliesslich neu angelegt (``O_EXCL``): Eine vorhandene
    Datei wird nie ueberschrieben, damit ein versehentlicher Aufruf keinen
    produktiven Schluessel vernichtet. Anschliessend werden die Rechte auf
    ``0600`` gesetzt, soweit die Plattform es unterstuetzt.

    Args:
        key_file: Zielpfad der Schluesseldatei. Elternverzeichnisse werden
            angelegt, falls noetig.

    Returns:
        Der Pfad der neu angelegten Schluesseldatei.

    Raises:
        KeyFileExistsError: Die Datei existiert bereits.
        KeyFileWriteError: Verzeichnis oder Datei konnten nicht angelegt bzw.
            geschrieben werden.
    """
    path = Path(key_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise KeyFileWriteError(f"Verzeichnis fuer Schluesseldatei nicht anlegbar: {path.parent}") from None

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise KeyFileExistsError(f"Schluesseldatei existiert bereits: {path}") from None
    except OSError:
        raise KeyFileWriteError(f"Schluesseldatei nicht anlegbar: {path}") from None

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(Fernet.generate_key())
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        _unlink_quietly(path)
        raise KeyFileWriteError(f"Schluesseldatei nicht schreibbar: {path}") from None

    _restrict_permissions(path)
    return path


class PinCipher:
    """Verschluesselt und entschluesselt PINs mit Fernet (N-SEC-01, F-SYN-08).

    Der Schluessel wird aus der Datei gelesen, die ``Settings.key_file`` angibt
    (Pfad per Umgebungsvariable). ``settings`` ist optional; ohne Angabe wird
    die gecachte Anwendungskonfiguration verwendet.

    Die Instanz haelt weder die PIN noch das Klartext-Passwort. ``repr`` und
    ``str`` nennen nur die Klasse und den Schluesseldatei-Pfad, nie
    Schluesselmaterial oder PIN-Inhalte (N-SEC-02).
    """

    __slots__ = ("_fernet", "_key_file")

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings if settings is not None else get_settings()
        self._key_file = Path(resolved.key_file)
        self._fernet = Fernet(_read_fernet_key(self._key_file))

    def encrypt_pin(self, pin: str) -> bytes:
        """Verschluessle eine PIN und gib das Fernet-Token als Bytes zurueck.

        Args:
            pin: PIN im Klartext. Wird nur im Aufruf gehalten und nicht
                gespeichert.

        Returns:
            Fernet-Token, geeignet fuer ``BankAccess.pin_encrypted``.
        """
        return self._fernet.encrypt(pin.encode("utf-8"))

    def decrypt_pin(self, token: bytes) -> str:
        """Entschluessle ein Fernet-Token zur PIN.

        Args:
            token: Fernet-Token aus ``BankAccess.pin_encrypted``.

        Returns:
            Die PIN im Klartext.

        Raises:
            PinDecryptionError: Token manipuliert, ungueltig oder mit einem
                anderen Schluessel erzeugt. Die Meldung enthaelt weder PIN
                noch Token (N-SEC-02).
        """
        try:
            plaintext = self._fernet.decrypt(token)
        except (InvalidToken, TypeError, ValueError):
            raise PinDecryptionError(
                "PIN nicht entschluesselbar: falscher Schluessel oder manipuliertes Token"
            ) from None

        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise PinDecryptionError("PIN nicht entschluesselbar: ungueltige Zeichenkodierung") from None

    def __repr__(self) -> str:
        """Geheimnisfreie Darstellung (N-SEC-02)."""
        return f"{type(self).__name__}(key_file={self._key_file!r})"

    def __str__(self) -> str:
        """Geheimnisfreie Darstellung (N-SEC-02)."""
        return f"{type(self).__name__}(key_file={self._key_file})"
