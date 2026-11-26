"""Fehler-Mapping-Service: Bankcode zu Klartext und Handlungsempfehlung.

Implementiert F-SYN-05 (Bankcode -> Klartext und Handlungsempfehlung,
unbekannte Codes zeigen den Originaltext der Bank), F-SYN-06 (wiederholbare
Codes), F-SYN-09 (pflegbares Mapping mit optionaler BLZ, Standardkennzeichnung,
Reset ohne Verlust eigener Eintraege) und F-CON-01/F-CON-02 (Versionspruefung
bei Aenderungen). N-SEC-02: Meldungen und Standardtexte enthalten keine PIN,
TAN oder sonstige Geheimnisse.

Aufloesungsreihenfolge (F-SYN-09, DoD T-066):

1. eigener Eintrag fuer die BLZ,
2. eigener allgemeiner Eintrag,
3. Standardeintrag fuer die BLZ,
4. allgemeiner Standardeintrag,
5. unbekannt -> Originaltext der Bank.

SQLite, PostgreSQL und MySQL lassen mehrere ``NULL``-Werte in
``UNIQUE (code, blz)`` zu (T-063-Hinweis). Ein allgemeiner Eintrag mit
``blz = NULL`` ist deshalb nicht durch die Datenbank geschuetzt. Der Service
prueft den Scope vor jedem Anlegen und Umhaengen selbst und waehlt bei
Altbestandsduplikaten deterministisch den Eintrag mit der kleinsten ID.

CRUD bleibt in dieser Schicht; die UI folgt in M7. Alle Zugriffe laufen ueber
SQLAlchemy (N-DB-01).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.log_filter import mask_text
from finue.models.bank_error import BankErrorMapping
from finue.services.bank_error_defaults import (
    reset_to_defaults as reset_to_defaults,
    seed_defaults as seed_defaults,
)

__all__ = [
    "BankErrorScopeConflict",
    "BankErrorVersionConflict",
    "ErrorSource",
    "ResolvedError",
    "create_error",
    "delete_error",
    "get_error",
    "list_errors",
    "reset_to_defaults",
    "resolve_error",
    "seed_defaults",
    "update_error",
]

ErrorSource = Literal["eigen", "default", "unbekannt"]

_CODE_MAX_LENGTH: Final[int] = 20
_BLZ_MAX_LENGTH: Final[int] = 8
_UNKNOWN_RECOMMENDATION: Final[str] = (
    "Bitte den Code im Fehler-Mapping ergaenzen oder den Originaltext der Bank pruefen."
)
_UNSET: Final[object] = object()


@dataclass(frozen=True, slots=True)
class ResolvedError:
    """Aufgeloester Bankfehler ohne Geheimnisfelder (F-SYN-05, N-SEC-02).

    ``source`` nennt die Herkunft: ``eigen`` (BLZ-spezifischer oder
    allgemeiner eigener Eintrag), ``default`` (mitgelieferter Standard) oder
    ``unbekannt`` (kein Treffer, ``message`` ist der Originaltext der Bank).

    ``repr`` gibt bewusst nur Code, Quelle und Wiederholbarkeit aus. Der
    Klartext kann bei unbekannten Codes der ungefilterte Banktext sein und
    gehoert deshalb nicht in ein technisches Log-``repr`` (N-SEC-02).
    """

    code: str
    message: str
    recommendation: str
    retryable: bool
    source: ErrorSource

    def __repr__(self) -> str:
        return f"ResolvedError(code={self.code!r}, source={self.source!r}, retryable={self.retryable!r})"


class BankErrorScopeConflict(ValueError):
    """Ein Eintrag fuer ``(code, blz)`` existiert bereits (F-SYN-05/09).

    Die Datenbank-Unique allein reicht wegen mehrfach erlaubter ``NULL``-
    Werte nicht aus (T-063-Hinweis); der Service meldet den Konflikt mit
    diesem Fehler.
    """

    def __init__(self, code: str, blz: str | None) -> None:
        self.code = code
        self.blz = blz
        scope = f"Code {code}" + (f" und BLZ {blz}" if blz else " ohne BLZ")
        super().__init__(f"Bankfehler-Mapping fuer {scope} existiert bereits.")


class BankErrorVersionConflict(RuntimeError):
    """Der Eintrag wurde zwischenzeitlich geaendert (F-CON-01/F-CON-02).

    ``current_version`` ist der inzwischen gespeicherte Stand. Der Aufrufer
    kann ihn in einer 409-Antwort mit dem gesendeten Stand anzeigen.
    """

    def __init__(self, error_id: int, expected_version: int, current_version: int) -> None:
        self.error_id = error_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Bankfehler-Eintrag {error_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


def resolve_error(
    session: Session,
    code: str,
    blz: str | None,
    original_text: str,
) -> ResolvedError:
    """Loest einen Bankcode nach der F-SYN-09-Reihenfolge auf (F-SYN-05).

    Args:
        session: SQLAlchemy-Session (nur lesend).
        code: Fehlercode der Bank.
        blz: Bankleitzahl oder ``None``. Leere Texte gelten als ``None``.
        original_text: Originaltext der Bank fuer unbekannte Codes.

    Returns:
        :class:`ResolvedError` mit Klartext, Handlungsempfehlung,
        Wiederholbarkeit und Quelle. Bei einem unbekannten Code ist
        ``message`` der Originaltext (bzw. ein Ersatztext, wenn er leer ist).

    Raises:
        ValueError: Wenn ``code`` leer oder ungueltig ist.
    """
    checked_code = _checked_code(code)
    checked_blz = _normalized_blz(blz)

    # Reihenfolge gemaess F-SYN-09. BLZ-spezifische Stufen entfallen ohne BLZ.
    candidates: list[tuple[bool, str | None]] = []
    if checked_blz is not None:
        candidates.append((False, checked_blz))
    candidates.append((False, None))
    if checked_blz is not None:
        candidates.append((True, checked_blz))
    candidates.append((True, None))

    for is_default, scope_blz in candidates:
        entry = _find_scope(session, checked_code, scope_blz, is_default=is_default)
        if entry is not None:
            return ResolvedError(
                code=checked_code,
                message=entry.message,
                recommendation=entry.recommendation,
                retryable=entry.retryable,
                source="default" if is_default else "eigen",
            )

    # Unbekannter Code: F-SYN-05 verlangt den Originaltext der Bank. Der Text
    # kann jedoch Eingaben der Bank spiegeln (etwa pin=.../tan=...), deshalb
    # laeuft er vor der Rueckgabe durch den Geheimnis-Filter (N-SEC-02).
    if original_text.strip():
        message = mask_text(original_text)
    else:
        message = f"Unbekannter Bankfehler (Code {checked_code})."
    return ResolvedError(
        code=checked_code,
        message=message,
        recommendation=_UNKNOWN_RECOMMENDATION,
        retryable=False,
        source="unbekannt",
    )


def create_error(
    session: Session,
    *,
    code: str,
    blz: str | None = None,
    message: str,
    recommendation: str,
    retryable: bool = False,
) -> BankErrorMapping:
    """Legt einen eigenen Fehler-Mapping-Eintrag an und committet ihn.

    ``is_default`` ist immer ``False``; Standardeintraege kommen ausschliesslich
    aus :func:`seed_defaults`. Der Scope wird service-seitig auf Duplikate
    geprueft, damit mehrere ``NULL``-Werte nicht zu mehrdeutigen
    allgemeinen Eintraegen fuehren (T-063-Hinweis).

    Raises:
        BankErrorScopeConflict: Wenn ``(code, blz)`` bereits belegt ist.
        ValueError: Bei leeren oder ungueltigen Werten.
    """
    checked_code = _checked_code(code)
    checked_blz = _normalized_blz(blz)
    checked_message = _checked_text(message, "message")
    checked_recommendation = _checked_text(recommendation, "recommendation")
    checked_retryable = _checked_retryable(retryable)

    if _find_scope(session, checked_code, checked_blz) is not None:
        raise BankErrorScopeConflict(checked_code, checked_blz)

    entry = BankErrorMapping(
        code=checked_code,
        blz=checked_blz,
        message=checked_message,
        recommendation=checked_recommendation,
        retryable=checked_retryable,
        is_default=False,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def get_error(session: Session, error_id: int) -> BankErrorMapping | None:
    """Liest einen Eintrag per ID; ``None``, wenn er nicht existiert."""
    return session.get(BankErrorMapping, _checked_id(error_id))


def list_errors(session: Session, *, code: str | None = None) -> list[BankErrorMapping]:
    """Listet Eintraege stabil nach Code, BLZ und ID (F-SYN-09).

    Args:
        session: SQLAlchemy-Session.
        code: Optionaler Filter auf genau diesen Code.

    Raises:
        ValueError: Wenn ``code`` uebergeben, aber leer oder ungueltig ist.
    """
    stmt = select(BankErrorMapping)
    if code is not None:
        stmt = stmt.where(BankErrorMapping.code == _checked_code(code))
    stmt = stmt.order_by(
        BankErrorMapping.code,
        BankErrorMapping.blz,
        BankErrorMapping.id,
    )
    return list(session.scalars(stmt))


def update_error(
    session: Session,
    error_id: int,
    expected_version: int,
    *,
    code: str | None = None,
    blz: object = _UNSET,
    message: str | None = None,
    recommendation: str | None = None,
    retryable: bool | None = None,
) -> BankErrorMapping:
    """Aendert einen Eintrag mit Versionspruefung und committet (F-CON-02).

    Nicht uebergebene Felder bleiben unveraendert. Bei ``blz`` setzt ein
    explizites ``None`` den Wert auf NULL; ein Sentinel unterscheidet das von
    "nicht uebergeben". ``is_default`` ist nicht aenderbar, damit der Reset
    Standard- und eigene Eintraege zuverlaessig trennt.

    Raises:
        LookupError: Wenn der Eintrag nicht existiert.
        BankErrorVersionConflict: Wenn ``expected_version`` nicht dem
            gespeicherten Stand entspricht (auch bei einem Wettlauf zwischen
            Pruefung und Commit).
        BankErrorScopeConflict: Wenn ``code``/``blz`` auf einen bereits
            belegten Scope zeigen.
        ValueError: Bei leeren oder ungueltigen Werten.
    """
    checked_id = _checked_id(error_id)
    checked_version = _checked_version(expected_version)
    entry = session.get(BankErrorMapping, checked_id)
    if entry is None:
        raise LookupError(f"Bankfehler-Eintrag {checked_id} nicht gefunden.")
    _ensure_version(entry, checked_version)

    new_code = entry.code if code is None else _checked_code(code)
    new_blz = entry.blz if blz is _UNSET else _normalized_blz(blz)
    if new_code != entry.code or new_blz != entry.blz:
        if _find_scope(session, new_code, new_blz, exclude_id=entry.id) is not None:
            raise BankErrorScopeConflict(new_code, new_blz)

    entry.code = new_code
    entry.blz = new_blz
    if message is not None:
        entry.message = _checked_text(message, "message")
    if recommendation is not None:
        entry.recommendation = _checked_text(recommendation, "recommendation")
    if retryable is not None:
        entry.retryable = _checked_retryable(retryable)

    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        current = session.get(BankErrorMapping, checked_id)
        current_version = current.version if current is not None else -1
        raise BankErrorVersionConflict(checked_id, checked_version, current_version) from exc

    session.refresh(entry)
    return entry


def delete_error(session: Session, error_id: int, expected_version: int) -> None:
    """Loescht einen Eintrag mit Versionspruefung und committet (F-CON-02).

    Raises:
        LookupError: Wenn der Eintrag nicht existiert.
        BankErrorVersionConflict: Wenn ``expected_version`` veraltet ist.
    """
    checked_id = _checked_id(error_id)
    checked_version = _checked_version(expected_version)
    entry = session.get(BankErrorMapping, checked_id)
    if entry is None:
        raise LookupError(f"Bankfehler-Eintrag {checked_id} nicht gefunden.")
    _ensure_version(entry, checked_version)

    session.delete(entry)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        current = session.get(BankErrorMapping, checked_id)
        current_version = current.version if current is not None else -1
        raise BankErrorVersionConflict(checked_id, checked_version, current_version) from exc


def _find_scope(
    session: Session,
    code: str,
    blz: str | None,
    *,
    is_default: bool | None = None,
    exclude_id: int | None = None,
) -> BankErrorMapping | None:
    """Sucht den ersten Eintrag fuer einen Scope (deterministisch nach ID).

    ``is_default`` filtert optional auf eigene oder Standardeintraege. Der
    Filter ist fuer die Aufloesungsreihenfolge noetig, fuer die
    Duplikatpruefung bleibt er offen.
    """
    stmt = select(BankErrorMapping).where(BankErrorMapping.code == code)
    if blz is None:
        stmt = stmt.where(BankErrorMapping.blz.is_(None))
    else:
        stmt = stmt.where(BankErrorMapping.blz == blz)
    if is_default is not None:
        stmt = stmt.where(BankErrorMapping.is_default.is_(is_default))
    if exclude_id is not None:
        stmt = stmt.where(BankErrorMapping.id != exclude_id)
    stmt = stmt.order_by(BankErrorMapping.id).limit(1)

    rows = list(session.scalars(stmt))
    return rows[0] if rows else None


def _checked_code(value: object) -> str:
    """Prueft einen Fehlercode (nicht leer, maximal 20 Zeichen)."""
    if not isinstance(value, str):
        raise ValueError("code muss ein Text sein.")
    code = value.strip()
    if not code:
        raise ValueError("code darf nicht leer sein.")
    if len(code) > _CODE_MAX_LENGTH:
        raise ValueError(f"code darf hoechstens {_CODE_MAX_LENGTH} Zeichen haben.")
    return code


def _normalized_blz(value: object) -> str | None:
    """Normalisiert eine optionale BLZ; leer wird zu ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("blz muss ein Text oder None sein.")
    blz = value.strip()
    if not blz:
        return None
    if len(blz) > _BLZ_MAX_LENGTH:
        raise ValueError(f"blz darf hoechstens {_BLZ_MAX_LENGTH} Zeichen haben.")
    return blz


def _checked_text(value: object, field: str) -> str:
    """Prueft einen nicht leeren Klartext oder eine Handlungsempfehlung."""
    if not isinstance(value, str):
        raise ValueError(f"{field} muss ein Text sein.")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} darf nicht leer sein.")
    return text


def _checked_retryable(value: object) -> bool:
    """Prueft die Wiederholbarkeit als echten Boolean (F-SYN-06)."""
    if not isinstance(value, bool):
        raise ValueError("retryable muss ein Boolean sein.")
    return value


def _checked_id(value: object) -> int:
    """Prueft eine positive Eintrags-ID."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("error_id muss eine positive Ganzzahl sein.")
    return value


def _checked_version(value: object) -> int:
    """Prueft eine positive erwartete Version (F-CON-01)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_version muss eine positive Ganzzahl sein.")
    return value


def _ensure_version(entry: BankErrorMapping, expected_version: int) -> None:
    """Vergleicht die erwartete mit der geladenen Version (F-CON-01)."""
    if entry.version != expected_version:
        raise BankErrorVersionConflict(entry.id, expected_version, entry.version)
