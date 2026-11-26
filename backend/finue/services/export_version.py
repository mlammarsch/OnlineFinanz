"""Versionskonvertierung des eigenen JSON-Exportformats (F-EXP-07).

Der Import liest jede fruehere Formatversion und konvertiert sie schrittweise
auf die aktuelle Version. Eine neuere Version als die der App wird mit klarer
Meldung abgelehnt. Die Konverter liegen in einer Registry als Einzelschritte
``(von_version, nach_version) -> konverter``; dadurch bleibt die Kette
erweiterbar, ohne bestehende Schritte zu aendern.

Aktuell gibt es nur Version 1 (``SUPPORTED_EXPORT_VERSION``). Solange die
Dateiversion der aktuellen Version entspricht, ist die Konvertierung die
Identitaet. Sobald eine neue Version hinzukommt, registriert sie einen
Konverter ``v(n) -> v(n+1)``; :func:`upgrade_to_current` verkettet dann alle
Schritte bis zur aktuellen Version.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from finue.services.export_schema import SUPPORTED_EXPORT_VERSION

ExportDocument = dict[str, Any]
Converter = Callable[[ExportDocument], ExportDocument]

#: Hoechste Formatversion, die diese App versteht.
CURRENT_EXPORT_VERSION: int = SUPPORTED_EXPORT_VERSION

# (von_version, nach_version) -> Konverter fuer genau einen Versionsschritt.
_CONVERTERS: dict[tuple[int, int], Converter] = {}

__all__ = [
    "CURRENT_EXPORT_VERSION",
    "ImportFormatVersionError",
    "register_converter",
    "upgrade_to_current",
]


class ImportFormatVersionError(ValueError):
    """Die Dateiversion fehlt, ist unbekannt oder neuer als die App (F-EXP-07)."""


def register_converter(from_version: int, to_version: int, converter: Converter) -> None:
    """Registriert einen Konverter fuer genau einen Versionsschritt.

    Erlaubt sind nur Einzelschritte ``v(n) -> v(n+1)``, damit
    :func:`upgrade_to_current` die Kette nachvollziehbar schrittweise aufbaut.

    Args:
        from_version: Ausgangsversion, mindestens 1.
        to_version: Zielversion, muss ``from_version + 1`` sein.
        converter: Funktion, die ein Exportdokument der Ausgangsversion
            entgegennimmt und ein Dokument der Zielversion zurueckgibt.

    Raises:
        TypeError: Eine Versionsnummer ist keine ganze Zahl.
        ValueError: Die Versionen bilden keinen zulaessigen Einzelschritt.
    """
    if (
        isinstance(from_version, bool)
        or isinstance(to_version, bool)
        or not isinstance(from_version, int)
        or not isinstance(to_version, int)
    ):
        raise TypeError("Versionsnummern muessen ganze Zahlen sein")
    if from_version < 1:
        raise ValueError("from_version muss mindestens 1 sein")
    if to_version != from_version + 1:
        raise ValueError("Es sind nur Einzelschritte erlaubt: to_version muss from_version + 1 sein")
    _CONVERTERS[(from_version, to_version)] = converter


def upgrade_to_current(data: dict[str, Any]) -> dict[str, Any]:
    """Konvertiert ein Exportdokument schrittweise auf die aktuelle Version.

    Args:
        data: Geparstes JSON-Exportdokument mit Feld ``version``.

    Returns:
        Dokument der aktuellen Formatversion. Bei bereits aktueller Version
        wird ``data`` unveraendert zurueckgegeben (Identitaet).

    Raises:
        ImportFormatVersionError: ``data`` ist kein JSON-Objekt, das Feld
            ``version`` fehlt oder ist ungueltig, die Dateiversion ist neuer
            als die App, oder fuer eine aeltere Version fehlt ein Konverter.
    """
    if not isinstance(data, Mapping):
        raise ImportFormatVersionError("Exportdaten muessen ein JSON-Objekt sein.")

    version = _read_version(data)
    current = CURRENT_EXPORT_VERSION
    while version < current:
        converter = _CONVERTERS.get((version, version + 1))
        if converter is None:
            raise ImportFormatVersionError(
                f"Fuer Exportformat-Version {version} ist kein Konverter auf Version {version + 1} registriert."
            )
        converted = converter(dict(data))
        if not isinstance(converted, Mapping):
            raise ImportFormatVersionError(
                f"Konverter von Version {version} auf Version {version + 1} lieferte kein JSON-Objekt."
            )
        upgraded = dict(converted)
        upgraded["version"] = version + 1
        data = upgraded
        version += 1
    return data


def _read_version(data: Mapping[str, Any]) -> int:
    """Liest und prueft das Feld ``version`` mit klaren Fehlermeldungen."""
    if "version" not in data:
        raise ImportFormatVersionError(
            "Feld 'version' fehlt. Ein Export der Finanzuebersicht muss die Formatversion enthalten."
        )
    value = data["version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImportFormatVersionError(
            "Feld 'version' muss eine ganze Zahl sein (kein String, kein Boolean, keine Gleitkommazahl)."
        )
    if value < 1:
        raise ImportFormatVersionError(f"Exportformat-Version {value} ist ungueltig; Versionen beginnen bei 1.")
    if value > CURRENT_EXPORT_VERSION:
        raise ImportFormatVersionError(
            f"Exportformat-Version {value} ist neuer als die von dieser App unterstuetzte "
            f"Version {CURRENT_EXPORT_VERSION}."
        )
    return value
