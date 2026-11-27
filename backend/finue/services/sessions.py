"""Fachlogik der Sitzungsuebersicht (F-CON-06, N-SEC-02).

Aktive Sitzungen auflisten (Geraet/Browser vereinfacht aus dem User-Agent,
letzter Zugriff) und einzelne Sitzungen abmelden. Die Anlage einer Sitzung
gehoert zur Anmeldung (N-SEC-04, M10); dieser Baustein liest und entfernt nur
Bestandszeilen des Modells :class:`finue.models.security.UserSession`.

Sicherheitsentscheidung (N-SEC-02):
    Weder die Ergebnisse noch die Fehler dieses Moduls tragen ``token_hash``
    oder ein sonstiges Geheimnis, und es wird nichts protokolliert. Die
    Sitzungs-ID ist eine Datenbank-ID und kein Geheimnis; der rohe User-Agent
    wird nur zu einem kurzen Geraete-/Browser-Text verkuerzt und nicht
    ausgeliefert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.security import UserSession

__all__ = [
    "UNKNOWN_DEVICE",
    "SessionSummary",
    "describe_device",
    "list_active_sessions",
    "revoke_session",
]

#: Anzeigetext, wenn der User-Agent kein Geraet und keinen Browser erkennen laesst.
UNKNOWN_DEVICE = "Unbekanntes Geraet"

# Betriebssystem-Muster in Pruefreihenfolge. "Android" enthaelt "linux", daher
# vor "linux" pruefen; iOS-Geraete vor "mac os x" (iPadOS nennt sich "Macintosh").
_OS_MARKERS: tuple[tuple[str, str], ...] = (
    ("android", "Android"),
    ("iphone", "iPhone"),
    ("ipod", "iPhone"),
    ("ipad", "iPad"),
    ("windows", "Windows"),
    ("mac os x", "macOS"),
    ("macintosh", "macOS"),
    ("linux", "Linux"),
)

# Browser-Muster in Pruefreihenfolge. Edge und Opera enthalten "chrome", Chrome
# und Edge enthalten "safari"; daher spezifische Marken zuerst pruefen. Die
# iOS-Varianten (EdgiOS/CriOS/FxiOS) stehen jeweils vor den Desktop-Marken.
_BROWSER_MARKERS: tuple[tuple[str, str], ...] = (
    ("edgios", "Edge"),
    ("edg/", "Edge"),
    ("opr/", "Opera"),
    ("opera", "Opera"),
    ("crios", "Chrome"),
    ("chrome/", "Chrome"),
    ("fxios", "Firefox"),
    ("firefox/", "Firefox"),
    ("safari/", "Safari"),
)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Eine aktive Sitzung fuer die Anzeige (F-CON-06).

    Traegt bewusst kein ``token_hash`` und keinen rohen User-Agent: nur die
    Sitzungs-ID, den verkuerzten Geraetetext, die Zeitpunkte und das Kennzeichen
    der laufenden Sitzung (N-SEC-02).
    """

    id: int
    device: str
    last_seen_at: datetime
    expires_at: datetime
    current: bool


def _match(text: str, markers: tuple[tuple[str, str], ...]) -> str | None:
    """Liefert das Label des ersten passenden Musters oder ``None``."""

    for needle, label in markers:
        if needle in text:
            return label
    return None


def describe_device(user_agent: str | None) -> str:
    """Verkuerzt einen User-Agent zu ``Browser auf System`` (F-CON-06).

    Die Zuordnung ist bewusst grob ("vereinfacht aus dem User-Agent", SPEC
    F-CON-06). Ein fehlender oder unbekannter User-Agent ergibt
    :data:`UNKNOWN_DEVICE`; ist nur eine Seite erkennbar, wird die andere als
    ``Unbekannter Browser`` bzw. ``unbekanntes System`` benannt.
    """

    if user_agent is None or not user_agent.strip():
        return UNKNOWN_DEVICE
    lowered = user_agent.lower()
    system = _match(lowered, _OS_MARKERS)
    browser = _match(lowered, _BROWSER_MARKERS)
    if system is None and browser is None:
        return UNKNOWN_DEVICE
    return f"{browser or 'Unbekannter Browser'} auf {system or 'unbekanntem System'}"


def list_active_sessions(
    session: Session,
    *,
    now: datetime,
    current_session_id: int | None = None,
) -> list[SessionSummary]:
    """Liefert die nicht abgelaufenen Sitzungen, zuletzt genutzte zuerst (F-CON-06).

    "Aktiv" heisst: ``expires_at`` liegt nach ``now`` (N-SEC-04). Abgelaufene
    Sitzungen erscheinen nicht. ``current_session_id`` markiert die Sitzung der
    laufenden Anfrage; in M4 gibt es noch keine Anmeldung (N-SEC-04, M10), daher
    bleibt der Wert ``None`` und keine Sitzung ist als aktuell gekennzeichnet.

    Args:
        session: Offene SQLAlchemy-Session (N-DB-01).
        now: Bezugszeitpunkt (injizierbare Uhr, aware UTC).
        current_session_id: ID der laufenden Sitzung oder ``None``.

    Returns:
        Die aktiven Sitzungen, nach ``last_seen_at`` absteigend sortiert.
    """

    stmt = (
        select(UserSession)
        .where(UserSession.expires_at > now)
        .order_by(UserSession.last_seen_at.desc(), UserSession.id.desc())
    )
    rows = session.execute(stmt).scalars().all()
    return [
        SessionSummary(
            id=row.id,
            device=describe_device(row.user_agent),
            last_seen_at=cast(datetime, row.last_seen_at),
            expires_at=cast(datetime, row.expires_at),
            current=row.id == current_session_id,
        )
        for row in rows
    ]


def revoke_session(session: Session, session_id: int) -> bool:
    """Meldet eine Sitzung ab, indem ihre Zeile geloescht wird (F-CON-06).

    Die Operation ist idempotent: ein unbekanntes oder bereits entferntes ``id``
    ist kein Fehler, sondern wirkungslos.

    Args:
        session: Offene SQLAlchemy-Session (N-DB-01).
        session_id: ID der abzumeldenden Sitzung.

    Returns:
        ``True``, wenn eine Sitzung entfernt wurde, sonst ``False``.
    """

    row = session.get(UserSession, session_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True
