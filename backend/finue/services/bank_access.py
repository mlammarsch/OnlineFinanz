"""Service fuer Bankzugaenge mit optionaler PIN-Speicherung.

Implementiert F-SYN-08 (PIN je Bankzugang verschluesselt speichern oder bei
jedem Sync abfragen), N-SEC-01 (PINs nur Fernet-verschluesselt in der DB, der
Schluessel liegt ausserhalb der DB), N-SEC-02 (keine PIN in Logs, Fehlern oder
Klartextfeldern) und F-CON-01/F-CON-02 (Versionspruefung bei Aenderungen eines
Bankzugangs).

Begriffe:

- ``store_pin = True``: Die PIN wird ueber :class:`~finue.services.pin_crypto.PinCipher`
  verschluesselt in ``BankAccess.pin_encrypted`` abgelegt. Eine je Aufruf
  uebergebene PIN ersetzt den gespeicherten Chiffretext.
- ``store_pin = False``: ``pin_encrypted`` bleibt leer. Die PIN kommt je Sync
  aus dem Aufruf, wird nur im Arbeitsspeicher gehalten und nie gespeichert
  oder geloggt. Ein trotzdem uebergebenes ``pin`` wird bewusst verworfen.

Der Wechsel von ``True`` auf ``False`` loescht den Chiffretext. Der Wechsel von
``False`` auf ``True`` verlangt eine PIN, sonst wird
:class:`PinRequiredError` ausgeloest.

Alle Zugriffe laufen ueber SQLAlchemy (N-DB-01). PIN-Werte werden weder in
Fehlermeldungen noch in ``repr``/``str`` der Fehlerklassen ausgegeben
(N-SEC-02).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.bank import Bank, BankAccess
from finue.services.pin_crypto import PinCipher

__all__ = [
    "BankAccessVersionConflict",
    "PinRequiredError",
    "create_access",
    "delete_access",
    "get_access",
    "list_accesses",
    "resolve_pin",
    "update_access",
]

_BLZ_MAX_LENGTH: Final[int] = 8
_NAME_MAX_LENGTH: Final[int] = 255
_TAN_METHOD_MAX_LENGTH: Final[int] = 255
_PIN_REQUIRED_MESSAGE: Final[str] = "PIN bitte eingeben."
_UNSET: Final[object] = object()


class PinRequiredError(Exception):
    """Die PIN muss vom Aufrufer im Arbeitsspeicher geliefert werden.

    Tritt auf, wenn ``store_pin = False`` ist und keine PIN uebergeben wurde
    oder wenn ``store_pin = True`` gesetzt werden soll, aber keine PIN zum
    Verschluesseln vorliegt. Die Meldung enthaelt nie eine PIN (N-SEC-02).
    """

    def __init__(self) -> None:
        super().__init__(_PIN_REQUIRED_MESSAGE)


class BankAccessVersionConflict(RuntimeError):
    """Der Bankzugang wurde zwischenzeitlich geaendert (F-CON-01/F-CON-02).

    ``current_version`` ist der inzwischen gespeicherte Stand. Der Aufrufer
    kann ihn in einer 409-Antwort zusammen mit der gesendeten Version anzeigen.
    """

    def __init__(self, access_id: int, expected_version: int, current_version: int) -> None:
        self.access_id = access_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Bankzugang {access_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


def create_access(
    session: Session,
    *,
    blz: str,
    login_name: str,
    cipher: PinCipher | None = None,
    tan_method: str | None = None,
    store_pin: bool = True,
    pin: str | None = None,
    bank_name: str | None = None,
) -> BankAccess:
    """Legt einen Bankzugang an und committet ihn (F-SYN-08, N-SEC-01).

    Die Bank wird ueber die BLZ gesucht oder neu angelegt. ``bank_name`` wird
    nur beim Anlegen einer neuen Bank verwendet; sonst gilt ``Bank <BLZ>``.

    Args:
        session: SQLAlchemy-Session.
        blz: Bankleitzahl (nicht leer, maximal 8 Zeichen).
        login_name: Loginname des Zugangs (nicht leer, maximal 255 Zeichen).
        cipher: :class:`PinCipher` zum Verschluesseln. Ohne Angabe wird eine
            Instanz aus ``Settings`` erzeugt, sobald eine PIN gespeichert
            werden soll.
        tan_method: Optionales TAN-Verfahren (z. B. ``"900"``). Leere Texte
            werden als ``None`` gespeichert.
        store_pin: ``True`` speichert die PIN verschluesselt; ``False`` haelt
            sie nur im Arbeitsspeicher des Aufrufers.
        pin: PIN im Klartext. Bei ``store_pin = False`` wird sie bewusst nicht
            gespeichert. Bei ``store_pin = True`` ist sie Pflicht.
        bank_name: Name fuer eine neu anzulegende Bank.

    Returns:
        Der persistierte :class:`~finue.models.bank.BankAccess`.

    Raises:
        PinRequiredError: ``store_pin = True`` ohne PIN.
        ValueError: Ungueltige Felder.
    """
    checked_blz = _checked_blz(blz)
    checked_login = _checked_login(login_name)
    checked_tan = _normalized_tan_method(tan_method)
    checked_store_pin = _checked_bool(store_pin, "store_pin")

    if checked_store_pin:
        pin_token = _encrypt_required_pin(cipher, pin)
    else:
        # F-SYN-08: PIN nicht speichern, auch wenn sie uebergeben wurde.
        pin_token = None

    bank = _find_or_create_bank(session, checked_blz, bank_name)
    access = BankAccess(
        bank_id=bank.id,
        login_name=checked_login,
        pin_encrypted=pin_token,
        tan_method=checked_tan,
        store_pin=checked_store_pin,
    )
    session.add(access)
    session.commit()
    session.refresh(access)
    return access


def get_access(session: Session, access_id: int) -> BankAccess | None:
    """Liest einen Bankzugang per ID; ``None``, wenn er nicht existiert."""
    return session.get(BankAccess, _checked_id(access_id))


def list_accesses(session: Session, *, bank_id: int | None = None) -> list[BankAccess]:
    """Listet Bankzugaenge stabil nach ID (optional je Bank)."""
    stmt = select(BankAccess)
    if bank_id is not None:
        stmt = stmt.where(BankAccess.bank_id == _checked_id(bank_id))
    stmt = stmt.order_by(BankAccess.id)
    return list(session.scalars(stmt))


def update_access(
    session: Session,
    access_id: int,
    expected_version: int,
    *,
    blz: str | None = None,
    bank_name: str | None = None,
    login_name: str | None = None,
    cipher: PinCipher | None = None,
    tan_method: object = _UNSET,
    store_pin: bool | None = None,
    pin: str | None = None,
) -> BankAccess:
    """Aendert einen Bankzugang mit Versionspruefung und committet (F-CON-02).

    Nicht uebergebene Felder bleiben unveraendert. ``blz = None`` bedeutet
    "unveraendert"; die BLZ ist auf der Bank nicht nullable. Fuer
    ``tan_method`` setzt ein explizites ``None`` den Wert auf NULL; ein
    Sentinel unterscheidet das von "nicht uebergeben".

    PIN-Semantik (F-SYN-08, N-SEC-01):

    - ``store_pin`` ``True`` -> ``False``: ``pin_encrypted`` wird geloescht.
      Ein uebergebenes ``pin`` wird nicht gespeichert.
    - ``store_pin`` ``False`` -> ``True``: ``pin`` ist Pflicht und wird
      verschluesselt gespeichert.
    - ``True`` bleibt ``True``: Ein uebergebenes ``pin`` ersetzt den
      Chiffretext. Ohne ``pin`` bleibt ein vorhandener Chiffretext erhalten;
      fehlt er, ist ``pin`` Pflicht.
    - ``False`` bleibt ``False``: Ein uebergebenes ``pin`` wird verworfen.

    Raises:
        LookupError: Wenn der Zugang nicht existiert.
        BankAccessVersionConflict: Wenn ``expected_version`` nicht dem
            gespeicherten Stand entspricht (auch bei einem Wettlauf zwischen
            Pruefung und Commit).
        PinRequiredError: Wenn eine PIN benoetigt wird, aber fehlt.
        ValueError: Ungueltige Felder.
    """
    checked_id = _checked_id(access_id)
    checked_version = _checked_version(expected_version)
    access = session.get(BankAccess, checked_id)
    if access is None:
        raise LookupError(f"Bankzugang {checked_id} nicht gefunden.")
    _ensure_version(access, checked_version)

    # Erst alle neuen Werte pruefen und die PIN verarbeiten, dann anwenden.
    # So bleibt das Objekt bei PinRequiredError unveraendert.
    checked_blz = _checked_blz(blz) if blz is not None else None
    new_login = access.login_name if login_name is None else _checked_login(login_name)
    new_tan = access.tan_method if tan_method is _UNSET else _normalized_tan_method(tan_method)

    new_store_pin = access.store_pin if store_pin is None else _checked_bool(store_pin, "store_pin")
    new_pin_encrypted = access.pin_encrypted
    if new_store_pin and not access.store_pin:
        # False -> True: PIN ist Pflicht.
        new_pin_encrypted = _encrypt_required_pin(cipher, pin)
    elif not new_store_pin and access.store_pin:
        # True -> False: Chiffretext loeschen (DoD T-071).
        new_pin_encrypted = None
    elif new_store_pin and pin is not None:
        # True bleibt True mit neuer PIN: Chiffretext ersetzen.
        new_pin_encrypted = _encrypt_required_pin(cipher, pin)
    elif new_store_pin and access.pin_encrypted is None:
        # True ohne vorhandenen Chiffretext und ohne neue PIN.
        raise PinRequiredError()
    # False bleibt False: uebergebenes PIN bleibt ungespeichert.

    new_bank: Bank | None = None
    if checked_blz is not None and checked_blz != access.bank.blz:
        new_bank = _find_or_create_bank(session, checked_blz, bank_name)

    if new_bank is not None:
        access.bank = new_bank
    access.login_name = new_login
    access.tan_method = new_tan
    access.pin_encrypted = new_pin_encrypted
    access.store_pin = new_store_pin

    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        current = session.get(BankAccess, checked_id)
        current_version = current.version if current is not None else -1
        raise BankAccessVersionConflict(checked_id, checked_version, current_version) from exc

    session.refresh(access)
    return access


def delete_access(session: Session, access_id: int, expected_version: int) -> None:
    """Loescht einen Bankzugang mit Versionspruefung (F-CON-02).

    Raises:
        LookupError: Wenn der Zugang nicht existiert.
        BankAccessVersionConflict: Wenn ``expected_version`` veraltet ist.
    """
    checked_id = _checked_id(access_id)
    checked_version = _checked_version(expected_version)
    access = session.get(BankAccess, checked_id)
    if access is None:
        raise LookupError(f"Bankzugang {checked_id} nicht gefunden.")
    _ensure_version(access, checked_version)

    session.delete(access)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        current = session.get(BankAccess, checked_id)
        current_version = current.version if current is not None else -1
        raise BankAccessVersionConflict(checked_id, checked_version, current_version) from exc


def resolve_pin(
    access: BankAccess,
    supplied_pin: str | None,
    *,
    cipher: PinCipher | None = None,
) -> str:
    """Liefert die fuer einen Sync gueltige PIN (F-SYN-08, N-SEC-01).

    Bei ``store_pin = True`` wird der gespeicherte Chiffretext entschluesselt;
    ``supplied_pin`` wird dann ignoriert. Bei ``store_pin = False`` kommt die
    PIN ausschliesslich aus ``supplied_pin`` und bleibt im Arbeitsspeicher.

    Args:
        access: Persistierter Bankzugang.
        supplied_pin: PIN aus dem Aufruf oder ``None``.
        cipher: :class:`PinCipher` zum Entschluesseln. Ohne Angabe wird eine
            Instanz aus ``Settings`` erzeugt, wenn entschluesselt werden muss.

    Returns:
        PIN im Klartext. Der Aufrufer darf sie nicht loggen oder speichern.

    Raises:
        PinRequiredError: ``store_pin = False`` ohne ``supplied_pin`` oder
            ``store_pin = True`` ohne gespeicherten Chiffretext.
        PinDecryptionError: Gespeicherter Chiffretext passt nicht zum
            Schluessel oder ist manipuliert (aus :class:`PinCipher`).
        ValueError: ``supplied_pin`` hat einen ungueltigen Typ.
    """
    if access.store_pin:
        if access.pin_encrypted is None:
            raise PinRequiredError()
        active_cipher = cipher if cipher is not None else PinCipher()
        return active_cipher.decrypt_pin(access.pin_encrypted)

    return _checked_supplied_pin(supplied_pin)


def _find_or_create_bank(session: Session, blz: str, bank_name: str | None) -> Bank:
    """Sucht eine Bank per BLZ oder legt sie mit sicherem Standardnamen an."""
    stmt = select(Bank).where(Bank.blz == blz).order_by(Bank.id).limit(1)
    existing = session.scalars(stmt).first()
    if existing is not None:
        return existing

    name = _checked_optional_name(bank_name) if bank_name is not None else f"Bank {blz}"
    bank = Bank(blz=blz, name=name)
    session.add(bank)
    session.flush()
    return bank


def _encrypt_required_pin(cipher: PinCipher | None, pin: str | None) -> bytes:
    """Verschluesselt eine zwingend erforderliche PIN (N-SEC-01)."""
    checked_pin = _checked_pin(pin)
    active_cipher = cipher if cipher is not None else PinCipher()
    return active_cipher.encrypt_pin(checked_pin)


def _checked_pin(value: object) -> str:
    """Prueft eine PIN, ohne sie in eine Fehlermeldung aufzunehmen."""
    if not isinstance(value, str) or not value:
        raise PinRequiredError()
    return value


def _checked_supplied_pin(value: object) -> str:
    """Prueft eine nur im Arbeitsspeicher gelieferte PIN (N-SEC-02)."""
    if value is None:
        raise PinRequiredError()
    if not isinstance(value, str):
        raise ValueError("supplied_pin muss ein Text sein.")
    if not value:
        raise PinRequiredError()
    return value


def _checked_blz(value: object) -> str:
    """Prueft eine Bankleitzahl (nicht leer, maximal 8 Zeichen)."""
    if not isinstance(value, str):
        raise ValueError("blz muss ein Text sein.")
    blz = value.strip()
    if not blz:
        raise ValueError("blz darf nicht leer sein.")
    if len(blz) > _BLZ_MAX_LENGTH:
        raise ValueError(f"blz darf hoechstens {_BLZ_MAX_LENGTH} Zeichen haben.")
    return blz


def _checked_login(value: object) -> str:
    """Prueft einen Loginnamen (nicht leer, maximal 255 Zeichen)."""
    if not isinstance(value, str):
        raise ValueError("login_name muss ein Text sein.")
    login = value.strip()
    if not login:
        raise ValueError("login_name darf nicht leer sein.")
    if len(login) > _NAME_MAX_LENGTH:
        raise ValueError(f"login_name darf hoechstens {_NAME_MAX_LENGTH} Zeichen haben.")
    return login


def _checked_optional_name(value: object) -> str:
    """Prueft einen optionalen Banknamen (nicht leer, maximal 255 Zeichen)."""
    if not isinstance(value, str):
        raise ValueError("bank_name muss ein Text sein.")
    name = value.strip()
    if not name:
        raise ValueError("bank_name darf nicht leer sein.")
    if len(name) > _NAME_MAX_LENGTH:
        raise ValueError(f"bank_name darf hoechstens {_NAME_MAX_LENGTH} Zeichen haben.")
    return name


def _normalized_tan_method(value: object) -> str | None:
    """Normalisiert ein optionales TAN-Verfahren; leere Texte werden ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("tan_method muss ein Text oder None sein.")
    tan_method = value.strip()
    if not tan_method:
        return None
    if len(tan_method) > _TAN_METHOD_MAX_LENGTH:
        raise ValueError(f"tan_method darf hoechstens {_TAN_METHOD_MAX_LENGTH} Zeichen haben.")
    return tan_method


def _checked_bool(value: object, field: str) -> bool:
    """Prueft einen echten Boolean."""
    if not isinstance(value, bool):
        raise ValueError(f"{field} muss ein Boolean sein.")
    return value


def _checked_id(value: object) -> int:
    """Prueft eine positive ID."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("access_id muss eine positive Ganzzahl sein.")
    return value


def _checked_version(value: object) -> int:
    """Prueft eine positive erwartete Version (F-CON-01)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_version muss eine positive Ganzzahl sein.")
    return value


def _ensure_version(access: BankAccess, expected_version: int) -> None:
    """Vergleicht die erwartete mit der geladenen Version (F-CON-01)."""
    if access.version != expected_version:
        raise BankAccessVersionConflict(access.id, expected_version, access.version)
