"""Service-Schicht fuer Banken-Stammdaten (F-KTO-10, F-CON-02).

Buendelt die Fachlogik der Banken-Stammdaten, damit REST (M4) und MCP (M9)
dieselbe Schicht nutzen (D10) und die API-Router nur HTTP uebersetzen.

Referenzierte Anforderungen:

- F-KTO-10 (Banken-Stammdaten: BLZ, Name, FinTS-Endpunkt, Website, Icon;
  editierbare Tabelle; vorbelegt sind nur Endpunkte mit belegter Quelle - siehe
  ``decisions.md``: es werden keine unbelegten Endpunkte vorbelegt),
- F-CON-02 (optimistisches Sperren ueber die Versionsspalte; 409 mit Stand),
- N-SEC-05 (FinTS-Endpunkt nur per HTTPS; kein ausgehender Abruf durch diesen
  Service),
- N-DB-01 (kein rohes Daten-SQL, nur SQLAlchemy).

Die Schicht kennt bewusst nur Standardfehler (``LookupError``, ``ValueError``,
``RuntimeError``); die API-Schicht uebersetzt sie in HTTP-Fehler.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.bank import Bank, BankAccess

__all__ = [
    "BankInUse",
    "BankNotFound",
    "BankValidationError",
    "BankVersionConflict",
    "count_accesses",
    "create_bank",
    "delete_bank",
    "get_bank",
    "list_banks",
    "update_bank",
]

_BLZ_PATTERN: Final = re.compile(r"[0-9]{8}")
_NAME_MAX: Final = 255
_URL_MAX: Final = 255


class BankNotFound(LookupError):
    """Die angeforderte Bank existiert nicht."""


class BankValidationError(ValueError):
    """Fachliche Validierung einer Bank ist fehlgeschlagen."""


class BankVersionConflict(RuntimeError):
    """Optimistisches Sperren: die bekannte Bankversion passt nicht (F-CON-02)."""

    def __init__(self, bank_id: int, expected_version: int, current_version: int) -> None:
        self.bank_id = bank_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Bank {bank_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


class BankInUse(RuntimeError):
    """Die Bank hat Bankzugaenge und darf nicht geloescht werden (F-KTO-10)."""

    def __init__(self, bank_id: int, access_count: int) -> None:
        self.bank_id = bank_id
        self.access_count = access_count
        super().__init__(
            f"Die Bank {bank_id} hat {access_count} Bankzugang/Bankzugaenge und kann nicht geloescht werden."
        )


# ------------------------------------------------------------------- Pruefungen


def _checked_version(value: object) -> int:
    """Prueft eine erwartete Version (positive Ganzzahl)."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BankValidationError("expected_version muss eine positive Ganzzahl sein.")
    return value


def _checked_blz(value: object) -> str:
    """Prueft eine Bankleitzahl (genau acht Ziffern, F-KTO-10)."""

    if not isinstance(value, str):
        raise BankValidationError("Die BLZ muss ein Text aus acht Ziffern sein.")
    blz = value.strip()
    if not _BLZ_PATTERN.fullmatch(blz):
        raise BankValidationError("Die BLZ muss aus genau acht Ziffern bestehen.")
    return blz


def _checked_name(value: object) -> str:
    """Prueft einen Banknamen (nicht leer, maximal 255 Zeichen)."""

    if not isinstance(value, str):
        raise BankValidationError("Der Name muss ein Text sein.")
    name = value.strip()
    if not name:
        raise BankValidationError("Der Name darf nicht leer sein.")
    if len(name) > _NAME_MAX:
        raise BankValidationError(f"Der Name darf hoechstens {_NAME_MAX} Zeichen lang sein.")
    return name


def _checked_url(value: object, *, field: str, https_only: bool) -> str | None:
    """Prueft eine optionale URL; der FinTS-Endpunkt ist nur per HTTPS erlaubt (N-SEC-05).

    Raises:
        BankValidationError: Die URL ist kein Text, zu lang oder hat ein
            unzulaessiges Schema bzw. keinen Host.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise BankValidationError(f"{field} muss ein Text sein.")
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > _URL_MAX:
        raise BankValidationError(f"{field} darf hoechstens {_URL_MAX} Zeichen lang sein.")
    parts = urlsplit(raw)
    allowed = {"https"} if https_only else {"http", "https"}
    if parts.scheme.lower() not in allowed:
        if https_only:
            raise BankValidationError("Der FinTS-Endpunkt muss mit https:// beginnen.")
        raise BankValidationError(f"{field} muss mit http:// oder https:// beginnen.")
    if not parts.netloc:
        raise BankValidationError(f"{field} ist keine gueltige URL.")
    return raw


def _ensure_bank_version(bank: Bank, expected_version: int) -> None:
    """Vergleicht die erwartete mit der gespeicherten Bankversion (F-CON-02)."""

    if bank.version != expected_version:
        raise BankVersionConflict(bank.id, expected_version, bank.version)


# ------------------------------------------------------------------ Abruf/Aendern


def get_bank(session: Session, bank_id: int) -> Bank:
    """Liefert eine Bank oder wirft ``BankNotFound``."""

    bank = session.get(Bank, bank_id)
    if bank is None:
        raise BankNotFound(f"Bank {bank_id} nicht gefunden.")
    return bank


def list_banks(session: Session) -> list[Bank]:
    """Listet alle Banken, nach Name und ID sortiert (F-KTO-10)."""

    stmt = select(Bank).order_by(Bank.name, Bank.id)
    return list(session.scalars(stmt))


def count_accesses(session: Session, bank_id: int) -> int:
    """Zaehlt die Bankzugaenge einer Bank (Loeschschutz, F-KTO-10)."""

    stmt = select(func.count()).select_from(BankAccess).where(BankAccess.bank_id == bank_id)
    return int(session.scalar(stmt) or 0)


def create_bank(
    session: Session,
    *,
    blz: object,
    name: object,
    website: object = None,
    fints_url: object = None,
) -> Bank:
    """Legt eine Bank an (F-KTO-10).

    Vorbelegt wird nichts: es gibt keine Standardendpunkte, weil die SPEC nur
    Endpunkte mit belegter Quelle zulaesst und keine vorliegt (``decisions.md``).

    Raises:
        BankValidationError: Ein Feld ist ungueltig.
    """

    bank = Bank(
        blz=_checked_blz(blz),
        name=_checked_name(name),
        website=_checked_url(website, field="Die Website", https_only=False),
        fints_url=_checked_url(fints_url, field="Der FinTS-Endpunkt", https_only=True),
    )
    session.add(bank)
    session.commit()
    session.refresh(bank)
    return bank


_BANK_UPDATE_FIELDS: Final = frozenset({"blz", "name", "website", "fints_url"})


def update_bank(
    session: Session,
    bank_id: int,
    *,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Bank:
    """Aendert Banken-Stammdaten mit Versionspruefung (F-KTO-10, F-CON-02).

    Nur die Schluessel aus :data:`_BANK_UPDATE_FIELDS` werden uebernommen; ein
    explizites ``None`` bei ``website`` oder ``fints_url`` loescht den Wert.

    Raises:
        BankNotFound: Die Bank existiert nicht.
        BankVersionConflict: ``expected_version`` ist veraltet.
        BankValidationError: Ein Wert ist ungueltig.
    """

    checked_version = _checked_version(expected_version)
    bank = get_bank(session, bank_id)
    _ensure_bank_version(bank, checked_version)

    unknown = set(changes) - _BANK_UPDATE_FIELDS
    if unknown:
        raise BankValidationError(f"Unbekannte Felder: {', '.join(sorted(unknown))}.")

    new_values: dict[str, Any] = {}
    if "blz" in changes:
        new_values["blz"] = _checked_blz(changes["blz"])
    if "name" in changes:
        new_values["name"] = _checked_name(changes["name"])
    if "website" in changes:
        new_values["website"] = _checked_url(changes["website"], field="Die Website", https_only=False)
    if "fints_url" in changes:
        new_values["fints_url"] = _checked_url(changes["fints_url"], field="Der FinTS-Endpunkt", https_only=True)

    for field_name, value in new_values.items():
        setattr(bank, field_name, value)

    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise BankVersionConflict(bank_id, checked_version, _current_version(session, bank_id)) from exc

    session.refresh(bank)
    return bank


def delete_bank(session: Session, bank_id: int, *, expected_version: int) -> None:
    """Loescht eine Bank; nur ohne Bankzugaenge (F-KTO-10, F-CON-02).

    Raises:
        BankNotFound: Die Bank existiert nicht.
        BankVersionConflict: ``expected_version`` ist veraltet.
        BankInUse: Die Bank hat mindestens einen Bankzugang.
    """

    checked_version = _checked_version(expected_version)
    bank = get_bank(session, bank_id)
    _ensure_bank_version(bank, checked_version)

    access_count = count_accesses(session, bank_id)
    if access_count > 0:
        raise BankInUse(bank_id, access_count)

    session.delete(bank)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise BankVersionConflict(bank_id, checked_version, _current_version(session, bank_id)) from exc


def _current_version(session: Session, bank_id: int) -> int:
    """Liest die aktuelle Bankversion nach einem Rollback (fuer den 409-Stand)."""

    current = session.get(Bank, bank_id)
    return current.version if current is not None else -1
