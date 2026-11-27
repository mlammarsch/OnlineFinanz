"""Icon-Verarbeitung und -Speicherung fuer Konten und Banken (F-KTO-11).

Der Upload ist auf PNG, JPEG und WebP begrenzt (SVG ist wegen des Skript-Risikos
ausgeschlossen). Der Server prueft den Dateityp am **Inhalt** (Signatur plus
Pillow-``verify``), schuetzt vor Dekompressionsbomben ueber eine Pixelgrenze,
skaliert das Bild auf 128 x 128 px mit erhaltenem Seitenverhaeltnis und speichert
es als PNG in ``account.icon`` bzw. ``bank.icon`` (samt ``icon_mime``).

Die Groessengrenze von 512 KB wird **vor** dem Dekodieren geprueft, damit ein
grosser Upload nicht erst Pillow erreicht. Es findet **kein** ausgehender Abruf
statt (F-KTO-08 gehoert zu M7, N-SEC-05).

Referenzierte Anforderungen:

- F-KTO-11 (Upload PNG/JPEG/WebP, hoechstens 512 KB, Pruefung am Inhalt,
  Skalierung auf 128 x 128 PNG in der Datenbank),
- F-CON-02 (optimistisches Sperren beim Ersetzen/Loeschen des Icons),
- N-SEC-05 (kein ausgehender Abruf), N-OPS-02 (Speicherung in der Datenbank).
"""

from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Final

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.account import Account
from finue.models.bank import Bank
from finue.services.accounts import AccountVersionConflict, get_account
from finue.services.banks import BankVersionConflict, get_bank

__all__ = [
    "ICON_MIME",
    "MAX_ICON_BYTES",
    "MAX_PIXELS",
    "TARGET_SIZE",
    "IconChange",
    "IconDecompressionBomb",
    "IconError",
    "IconTooLarge",
    "IconTypeInvalid",
    "ProcessedIcon",
    "clear_account_icon",
    "clear_bank_icon",
    "detect_image_format",
    "icon_etag",
    "process_icon",
    "set_account_icon",
    "set_bank_icon",
]

#: Hoechstgroesse des Uploads (F-KTO-11: 512 KB), geprueft vor dem Dekodieren.
MAX_ICON_BYTES: Final = 512 * 1024

#: Zielformat und Zielmass des gespeicherten Icons (F-KTO-11).
ICON_MIME: Final = "image/png"
TARGET_SIZE: Final = (128, 128)

#: Pixelgrenze gegen Dekompressionsbomben (deutlich ueber einem sinnvollen Icon).
MAX_PIXELS: Final = 4096 * 4096

_ALLOWED_FORMATS: Final = frozenset({"PNG", "JPEG", "WEBP"})
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
_BOMB_MESSAGE: Final = "Das Bild ist zu gross (Schutz vor Dekompressionsbomben)."
_TYPE_MESSAGE: Final = "Nur PNG, JPEG oder WebP sind erlaubt."
_SVG_MESSAGE: Final = "SVG-Dateien sind nicht erlaubt (Skript-Risiko). Erlaubt sind PNG, JPEG oder WebP."


class IconError(ValueError):
    """Basisklasse fuer abgelehnte Icon-Uploads."""


class IconTooLarge(IconError):
    """Der Upload ueberschreitet die Groessengrenze (F-KTO-11, 512 KB)."""


class IconTypeInvalid(IconError):
    """Der Dateiinhalt ist kein zulaessiges Bild (PNG, JPEG oder WebP)."""


class IconDecompressionBomb(IconTypeInvalid):
    """Der Bildinhalt ueberschreitet die Pixelgrenze (Dekompressionsschutz)."""


@dataclass(frozen=True, slots=True)
class ProcessedIcon:
    """Ergebnis der Icon-Verarbeitung: PNG-Bytes und Masse."""

    data: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class IconChange:
    """Ergebnis einer Icon-Aenderung an Konto oder Bank."""

    id: int
    version: int
    has_icon: bool
    width: int | None
    height: int | None


# ------------------------------------------------------------------ Verarbeitung


def detect_image_format(data: bytes) -> str | None:
    """Erkennt PNG, JPEG oder WebP an der Signatur (Typpruefung am Inhalt, F-KTO-11)."""

    if data.startswith(_PNG_SIGNATURE):
        return "PNG"
    if data.startswith(_JPEG_SIGNATURE):
        return "JPEG"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def _looks_like_svg(data: bytes) -> bool:
    """Erkennt SVG an ``<svg`` oder einem XML-Kopf mit ``<svg`` (ausgeschlossen)."""

    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1024].lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _verify_image(data: bytes) -> str:
    """Oeffnet und verifiziert das Bild mit Pillow; liefert das Format.

    Raises:
        IconDecompressionBomb: Pillow meldet eine Dekompressionsbombe.
        IconTypeInvalid: Der Inhalt ist kein lesbares PNG/JPEG/WebP.
    """

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(data))
            image_format = image.format
            image.verify()
    except Image.DecompressionBombError as exc:
        raise IconDecompressionBomb(_BOMB_MESSAGE) from exc
    except Image.DecompressionBombWarning as exc:
        raise IconDecompressionBomb(_BOMB_MESSAGE) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise IconTypeInvalid("Der Dateiinhalt ist kein lesbares PNG-, JPEG- oder WebP-Bild.") from exc
    if image_format not in _ALLOWED_FORMATS:
        raise IconTypeInvalid(_TYPE_MESSAGE)
    return image_format


def process_icon(data: bytes) -> ProcessedIcon:
    """Prueft, skaliert und kodiert einen Icon-Upload als PNG (F-KTO-11).

    Reihenfolge: Groesse (vor dem Dekodieren), Signatur, Pillow-``verify``,
    Pixelgrenze, dann Skalierung auf 128 x 128 mit erhaltenem Seitenverhaeltnis.

    Raises:
        IconTooLarge: ``data`` ist groesser als :data:`MAX_ICON_BYTES`.
        IconTypeInvalid: Der Inhalt ist SVG oder kein PNG/JPEG/WebP.
        IconDecompressionBomb: Die Pixelzahl ueberschreitet :data:`MAX_PIXELS`.
    """

    if not data:
        raise IconTypeInvalid("Die Datei ist leer.")
    if len(data) > MAX_ICON_BYTES:
        raise IconTooLarge(
            f"Das Icon darf hoechstens {MAX_ICON_BYTES // 1024} KB gross sein.",
        )

    sniffed = detect_image_format(data)
    if sniffed is None:
        if _looks_like_svg(data):
            raise IconTypeInvalid(_SVG_MESSAGE)
        raise IconTypeInvalid(_TYPE_MESSAGE)

    verified = _verify_image(data)
    if verified != sniffed:
        raise IconTypeInvalid("Der Dateiinhalt passt nicht zur erkannten Bildart.")

    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise IconDecompressionBomb(_BOMB_MESSAGE)
        converted = image.convert("RGBA")
        converted.thumbnail(TARGET_SIZE, Image.Resampling.LANCZOS)

    buffer = BytesIO()
    converted.save(buffer, format="PNG", optimize=True)
    return ProcessedIcon(data=buffer.getvalue(), mime=ICON_MIME, width=converted.width, height=converted.height)


def icon_etag(data: bytes) -> str:
    """Bildet einen stabilen ETag aus den gespeicherten PNG-Bytes (F-KTO-11)."""

    return f'"{hashlib.sha256(data).hexdigest()[:32]}"'


# ------------------------------------------------------------------- Speicherung


def _checked_version(value: object) -> int:
    """Prueft eine erwartete Version (positive Ganzzahl)."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("expected_version muss eine positive Ganzzahl sein.")
    return value


def _account_version(session: Session, account_id: int) -> int:
    """Liest die aktuelle Kontoversion nach einem Rollback (fuer den 409-Stand)."""

    current = session.get(Account, account_id)
    return current.version if current is not None else -1


def _bank_version(session: Session, bank_id: int) -> int:
    """Liest die aktuelle Bankversion nach einem Rollback (fuer den 409-Stand)."""

    current = session.get(Bank, bank_id)
    return current.version if current is not None else -1


def set_account_icon(
    session: Session,
    account_id: int,
    *,
    expected_version: int,
    data: bytes,
) -> IconChange:
    """Setzt das Icon eines Kontos als PNG (F-KTO-11, F-CON-02).

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        AccountVersionConflict: ``expected_version`` ist veraltet.
        IconError: Der Upload ist zu gross, kein zulaessiges Bild oder eine Bombe.
    """

    processed = process_icon(data)
    checked_version = _checked_version(expected_version)
    account = get_account(session, account_id)
    if account.version != checked_version:
        raise AccountVersionConflict(account_id, checked_version, account.version)

    account.icon = processed.data
    account.icon_mime = processed.mime
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise AccountVersionConflict(account_id, checked_version, _account_version(session, account_id)) from exc

    session.refresh(account)
    return IconChange(
        id=account.id,
        version=account.version,
        has_icon=True,
        width=processed.width,
        height=processed.height,
    )


def clear_account_icon(session: Session, account_id: int, *, expected_version: int) -> IconChange:
    """Entfernt das Icon eines Kontos (F-KTO-11, F-CON-02).

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        AccountVersionConflict: ``expected_version`` ist veraltet.
    """

    checked_version = _checked_version(expected_version)
    account = get_account(session, account_id)
    if account.version != checked_version:
        raise AccountVersionConflict(account_id, checked_version, account.version)

    account.icon = None
    account.icon_mime = None
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise AccountVersionConflict(account_id, checked_version, _account_version(session, account_id)) from exc

    session.refresh(account)
    return IconChange(id=account.id, version=account.version, has_icon=False, width=None, height=None)


def set_bank_icon(
    session: Session,
    bank_id: int,
    *,
    expected_version: int,
    data: bytes,
) -> IconChange:
    """Setzt das Icon einer Bank als PNG (F-KTO-11, F-CON-02).

    Raises:
        BankNotFound: Die Bank existiert nicht.
        BankVersionConflict: ``expected_version`` ist veraltet.
        IconError: Der Upload ist zu gross, kein zulaessiges Bild oder eine Bombe.
    """

    processed = process_icon(data)
    checked_version = _checked_version(expected_version)
    bank = get_bank(session, bank_id)
    if bank.version != checked_version:
        raise BankVersionConflict(bank_id, checked_version, bank.version)

    bank.icon = processed.data
    bank.icon_mime = processed.mime
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise BankVersionConflict(bank_id, checked_version, _bank_version(session, bank_id)) from exc

    session.refresh(bank)
    return IconChange(
        id=bank.id,
        version=bank.version,
        has_icon=True,
        width=processed.width,
        height=processed.height,
    )


def clear_bank_icon(session: Session, bank_id: int, *, expected_version: int) -> IconChange:
    """Entfernt das Icon einer Bank (F-KTO-11, F-CON-02).

    Raises:
        BankNotFound: Die Bank existiert nicht.
        BankVersionConflict: ``expected_version`` ist veraltet.
    """

    checked_version = _checked_version(expected_version)
    bank = get_bank(session, bank_id)
    if bank.version != checked_version:
        raise BankVersionConflict(bank_id, checked_version, bank.version)

    bank.icon = None
    bank.icon_mime = None
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise BankVersionConflict(bank_id, checked_version, _bank_version(session, bank_id)) from exc

    session.refresh(bank)
    return IconChange(id=bank.id, version=bank.version, has_icon=False, width=None, height=None)
