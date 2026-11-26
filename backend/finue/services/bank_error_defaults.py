"""Mitgelieferte Standardeintraege fuer das Bankfehler-Mapping (F-SYN-05/09).

Der Seed ist absichtlich klein und nur mit Codes befuellt, die durch SPEC oder
T-060 belegt sind:

* ``9800`` und ``9010``: SPEC F-SYN-05/06 (Dialog abgebrochen bzw.
  Verarbeitung nicht moeglich, beide wiederholbar).
* falsche PIN ``9340``, ``9910``, ``9930``, ``9931``, ``9942`` und gesperrter
  Zugang ``3938``: T-060, python-fints 5.0.1 (``client.py``/``exceptions.py``).
* ING-90-Tage-Regel: SPEC F-SYN-05. Der echte Bankcode ist unbekannt; der
  Eintrag traegt deshalb den internen Platzhalter :data:`ING_LOGIN_EXPIRED_CODE`
  und ist in :data:`UNVERIFIED_DEFAULT_CODES` als unbelegt gekennzeichnet. Der
  Code wird erst nach einem Owner-Live-Test ersetzt (handoff.md, offene Frage).

Weitere Codes werden nicht geseedet. Neue Codes duerfen nur mit Beleg
(FinTS-Spezifikation, python-fints-Quelltext/Doku oder Owner-Live-Test)
hinzukommen; sonst bleiben sie als unbelegt markiert.

N-SEC-02: Kein Standardtext enthaelt PIN, TAN oder sonstige Geheimnisse.
N-DB-01: Alle Zugriffe laufen ueber SQLAlchemy, nie ueber rohes SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.bank_error import BankErrorMapping

__all__ = [
    "DEFAULT_BANK_ERRORS",
    "DEFAULT_BANK_ERROR_CODES",
    "ING_LOGIN_EXPIRED_CODE",
    "UNVERIFIED_DEFAULT_CODES",
    "DefaultBankError",
    "reset_to_defaults",
    "seed_defaults",
]

#: Interner Platzhalter fuer die ING-90-Tage-Regel. Kein belegter Bankcode.
ING_LOGIN_EXPIRED_CODE: Final[str] = "ING90"

#: Codes des Seeds, deren Bankcode oder Verhalten noch nicht belegt ist.
UNVERIFIED_DEFAULT_CODES: Final[frozenset[str]] = frozenset({ING_LOGIN_EXPIRED_CODE})

_PIN_CODES: Final[tuple[str, ...]] = ("9340", "9910", "9930", "9931", "9942")
_PIN_MESSAGE: Final[str] = "PIN nicht akzeptiert."
_PIN_RECOMMENDATION: Final[str] = "PIN pruefen; nach wiederholten Fehlversuchen droht die Sperre des Zugangs."
_RETRY_MESSAGE_9800: Final[str] = "Dialog abgebrochen."
_RETRY_MESSAGE_9010: Final[str] = "Verarbeitung nicht moeglich."


@dataclass(frozen=True, slots=True)
class DefaultBankError:
    """Ein mitgelieferter Standardeintrag samt Beleglage.

    ``verified`` ist ``True``, wenn Code und Verhalten durch SPEC oder T-060
    belegt sind. ``False`` markiert einen Platzhalter, dessen echter Bankcode
    erst nach einem Live-Test bekannt ist (heute nur die ING-90-Tage-Regel).
    """

    code: str
    message: str
    recommendation: str
    retryable: bool
    source: str
    verified: bool


DEFAULT_BANK_ERRORS: Final[tuple[DefaultBankError, ...]] = (
    DefaultBankError(
        code="9800",
        message=_RETRY_MESSAGE_9800,
        recommendation=("Automatisch erneut versuchen (hoechstens 2 Versuche mit neuem Dialog und Wartezeit)."),
        retryable=True,
        source="SPEC F-SYN-05/06",
        verified=True,
    ),
    DefaultBankError(
        code="9010",
        message=_RETRY_MESSAGE_9010,
        recommendation=("Wie bei 9800 automatisch erneut versuchen; danach spaeter erneut anfragen."),
        retryable=True,
        source="SPEC F-SYN-05/06",
        verified=True,
    ),
    *(
        DefaultBankError(
            code=code,
            message=_PIN_MESSAGE,
            recommendation=_PIN_RECOMMENDATION,
            retryable=False,
            source="T-060 (python-fints 5.0.1)",
            verified=True,
        )
        for code in _PIN_CODES
    ),
    DefaultBankError(
        code="3938",
        message="Zugang gesperrt.",
        recommendation=("Bank kontaktieren bzw. den Zugang im Online-Banking entsperren."),
        retryable=False,
        source="T-060 (python-fints 5.0.1)",
        verified=True,
    ),
    DefaultBankError(
        code=ING_LOGIN_EXPIRED_CODE,
        message="FinTS-Zugriff nicht erlaubt.",
        recommendation="Bitte einmal im ING Online-Banking einloggen.",
        retryable=False,
        source="SPEC F-SYN-05; Bankcode unbelegt",
        verified=False,
    ),
)

DEFAULT_BANK_ERROR_CODES: Final[tuple[str, ...]] = tuple(entry.code for entry in DEFAULT_BANK_ERRORS)


def seed_defaults(session: Session) -> int:
    """Legt fehlende Standardeintraege an (F-SYN-09).

    Vorhandene Eintraege werden nie ueberschrieben: Ein eigener Eintrag fuer
    denselben Geltungsbereich hat Vorrang (F-SYN-09) und bleibt unangetastet.
    Ein bereits vorhandener Standardeintrag wird ebenfalls nicht aktualisiert;
    dafuer gibt es :func:`reset_to_defaults`.

    Die Scope-Pruefung erfolgt im Service, weil SQLite, PostgreSQL und MySQL
    mehrere ``NULL``-Werte in ``UNIQUE (code, blz)`` zulassen (T-063-Hinweis).
    Ohne die Pruefung koennten mehrere allgemeine Standardeintraege entstehen.

    Returns:
        Anzahl der neu angelegten Standardeintraege. ``0`` bedeutet, dass alle
        Scopes bereits belegt sind.
    """
    created = _insert_missing_defaults(session)
    if created:
        session.commit()
    return created


def reset_to_defaults(session: Session) -> int:
    """Setzt nur die Standardeintraege auf den mitgelieferten Stand zurueck.

    Alle Eintraege mit ``is_default = True`` werden entfernt und anschliessend
    neu geseedet. Eigene Eintraege (``is_default = False``) bleiben erhalten;
    belegt ein eigener Eintrag denselben Scope wie ein Standard, gewinnt er
    weiterhin und der Standard wird nicht neu angelegt (F-SYN-09).

    Returns:
        Anzahl der nach dem Reset vorhandenen, neu angelegten
        Standardeintraege.
    """
    defaults = list(session.scalars(select(BankErrorMapping).where(BankErrorMapping.is_default.is_(True))))
    for entry in defaults:
        session.delete(entry)
    session.flush()

    created = _insert_missing_defaults(session)
    session.commit()
    return created


def _insert_missing_defaults(session: Session) -> int:
    """Fuegt Defaults ein, deren ``(code, blz)`` noch nicht belegt ist."""
    existing = {(entry.code, entry.blz) for entry in session.scalars(select(BankErrorMapping))}
    created = 0
    for default in DEFAULT_BANK_ERRORS:
        scope = (default.code, None)
        if scope in existing:
            continue
        session.add(
            BankErrorMapping(
                code=default.code,
                blz=None,
                message=default.message,
                recommendation=default.recommendation,
                retryable=default.retryable,
                is_default=True,
            )
        )
        existing.add(scope)
        created += 1
    return created
