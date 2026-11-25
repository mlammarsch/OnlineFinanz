"""Zeitzonen-Hilfsfunktionen für Finanzübersicht Backend.

Implementiert N-OPS-06 (sichere Zeitzonen-Verwaltung). Zeitzone wird aus
Settings.tz geladen und validiert. Alle Datetimes sind timezone-aware.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finue.config import get_settings


def get_tz() -> ZoneInfo:
    """Gibt ZoneInfo-Objekt aus Settings zurück.

    Liest Settings.tz und gibt das entsprechende ZoneInfo-Objekt zurück.
    Wenn die Zeitzone ungültig ist, wird eine aussagekräftige ValueError
    aufgeworfen.

    Returns:
        ZoneInfo: Zeitzone-Objekt für die konfigurierte Zeitzone

    Raises:
        ValueError: Wenn die konfigurierte Zeitzone ungültig ist
    """
    settings = get_settings()
    try:
        return ZoneInfo(settings.tz)
    except ZoneInfoNotFoundError as e:
        raise ValueError(
            f"Ungültige Zeitzone '{settings.tz}': {e}"
        ) from e


def utc_now() -> datetime:
    """Gibt aktuelle Uhrzeit in UTC als aware datetime zurück.

    Returns:
        datetime: Aktuelle Uhrzeit in UTC mit timezone-info
    """
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    """Konvertiert datetime zur lokalen Zeitzone.

    Wenn dt naive ist, wird angenommen, dass es UTC ist.
    Wenn dt aware ist, wird es direkt konvertiert.

    Args:
        dt: datetime in UTC oder aware

    Returns:
        datetime: datetime in der lokalen Zeitzone (aware)
    """
    local_tz = get_tz()

    if dt.tzinfo is None:
        # Naive datetime als UTC interpretieren
        dt_utc = dt.replace(tzinfo=timezone.utc)
    else:
        # Bereits aware
        dt_utc = dt

    return dt_utc.astimezone(local_tz)
