"""PayPal-Hilfszeilen-Filter fuer den CSV-Import (F-IMP-08).

PayPal-Exporte enthalten neben echten Zahlungen auch Hilfszeilen, die kein
Bankumsatz sind: Waehrungsumrechnungen, Einbehalte und den Bankeinzug zur
Deckung. Der Import uebernimmt nur abgeschlossene Zahlungen. Dieses Modul
filtert die Zeilen vor dem Mapping (F-IMP-01, F-IMP-03), damit Vorschau und
Import-Lauf keine Kunstbuchungen erhalten.

Die Erkennungsmuster sind **unbelegt, nach bestem Wissen** -- eine echte
PayPal-Beispieldatei vom Owner steht noch offen (SPEC Abschnitt 7,
handoff.md). Sie liegen deshalb als benannte Konstanten vor und lassen sich
nach Vorliegen echter Exporte an einer Stelle korrigieren.

Integration: Die PayPal-Vorlage (T-056) kann im Mapping-Ablauf das Flag
``pre_filter='paypal'`` tragen. Der Aufrufer wendet den Filter vor
:func:`finue.services.mapping.apply_mapping` an::

    rows = PRE_FILTERS[vorlage.pre_filter](rows, status_field="Status")

``status_field`` ist die Spalte mit dem PayPal-Status (z. B. ``Status``).
Unbekannte Statuswerte werden konservativ behalten. Entfernt werden nur
explizit bekannte, nicht abgeschlossene Status und explizit bekannte
Hilfszeilen; damit gehen bei einem unbekannten Export keine echten Zahlungen
verloren.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

__all__ = [
    "COMPLETED_STATUSES",
    "HELPER_ROW_PATTERNS",
    "INCOMPLETE_STATUSES",
    "PAYPAL_PRE_FILTER",
    "PRE_FILTERS",
    "filter_paypal_rows",
]

#: Flag in einer Import-Vorlage, das diesen Filter auswaehlt (T-056, F-IMP-05).
PAYPAL_PRE_FILTER = "paypal"

# Statuswerte in PayPal-Exporten sind sprachabhaengig. Verglichen wird
# normalisiert (Kleinschreibung, Umlaute ausgeschrieben, Leerraum vereinheitlicht).
COMPLETED_STATUSES: frozenset[str] = frozenset(
    {
        "completed",
        "complete",
        "abgeschlossen",
        "erfolgreich",
        "successful",
    }
)

# Explizit bekannte, nicht abgeschlossene Status. Nur diese werden entfernt;
# alles andere gilt als unbekannt und bleibt erhalten (konservativ).
INCOMPLETE_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "ausstehend",
        "denied",
        "abgelehnt",
        "refused",
        "abgewiesen",
        "refunded",
        "erstattet",
        "rueckerstattet",
        "rueckgezahlt",
        "partially refunded",
        "teilweise erstattet",
        "teilweise rueckerstattet",
        "reversed",
        "rueckgebucht",
        "rueckabgewickelt",
        "canceled",
        "cancelled",
        "storniert",
        "abgebrochen",
        "voided",
        "ungueltig",
        "nichtig",
        "expired",
        "abgelaufen",
        "failed",
        "fehlgeschlagen",
        "processing",
        "in bearbeitung",
        "wird bearbeitet",
        "held",
        "einbehalten",
        "zurueckgehalten",
        "blocked",
        "gesperrt",
        "incomplete",
        "unvollstaendig",
    }
)

# Hilfszeilen: Erkennungsmuster fuer Typ- und Beschreibungsfelder.
# ACHTUNG: unbelegt, nach bestem Wissen -- echte PayPal-Beispieldatei vom
# Owner offen (handoff.md, SPEC Abschnitt 7). Bei Vorliegen echter Exporte
# hier korrigieren. Im Typfeld wird als Teilstring geprueft, im
# Beschreibungsfeld nur als exakter Anfang, damit ein Verwendungszweck, der
# das Wort nur nebenbei nennt, keine echte Zahlung entfernt.
# Bewusst nicht enthalten: das blosse "Bankeinzug" (kann eine normale
# Aufladung sein); gefiltert wird nur der Einzug *zur Deckung*.
HELPER_ROW_PATTERNS: tuple[str, ...] = (
    "waehrungsumrechnung",
    "currency conversion",
    "einbehalt",
    "temporary hold",
    "payment hold",
    "bankeinzug zur deckung",
    "lastschrift zur deckung",
    "bank debit to cover",
    "debit to cover",
)

_TYPE_FIELDS: tuple[str, ...] = (
    "typ",
    "type",
    "transaktionstyp",
    "transaction type",
)
_DESCRIPTION_FIELDS: tuple[str, ...] = (
    "beschreibung",
    "description",
    "name",
    "artikelbezeichnung",
    "item title",
    "verwendungszweck",
    "memo",
    "hinweis",
    "betreff",
    "subject",
)

#: Normalisierte Umlaute/Es-Zett, damit deutsche und ausgeschriebene Schreibweisen
#: denselben Mustern entsprechen (z. B. "Währungsumrechnung" -> "waehrungsumrechnung").
_UMLAUTS = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
)

PreFilter = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]


def filter_paypal_rows(rows: list[dict[str, Any]], status_field: str) -> list[dict[str, Any]]:
    """Filtert PayPal-Exportzeilen fuer den Import (F-IMP-08).

    Behalten werden abgeschlossene Zahlungen sowie Zeilen mit unbekanntem oder
    fehlendem Status. Entfernt werden:

    - Zeilen mit einem explizit bekannten, nicht abgeschlossenen Status
      (z. B. ``Pending``, ``Ausstehend``, ``Refunded``),
    - Hilfszeilen fuer Waehrungsumrechnung, Einbehalte und Bankeinzug zur
      Deckung, auch wenn ihr Status ``Completed`` ist.

    Die Eingabe bleibt unveraendert; die Reihenfolge der behaltenen Zeilen
    entspricht der Eingabe.

    Args:
        rows: PayPal-Zeilen als Woerterbuch, Schluessel sind die
            Export-Spaltennamen.
        status_field: Name der Statusspalte, z. B. ``Status``. Der Name wird
            exakt und ersatzweise ohne Beachtung der Gross-/Kleinschreibung
            gesucht. Fehlt die Spalte, gilt der Status als unbekannt und die
            Zeile bleibt erhalten.

    Returns:
        Neue Liste mit den verbleibenden Zeilen (unveraenderte Dict-Objekte).

    Raises:
        ValueError: Wenn ``status_field`` leer ist.
        TypeError: Wenn eine Zeile kein Mapping ist.
    """
    if not isinstance(status_field, str) or not status_field.strip():
        raise ValueError("status_field muss der Name der PayPal-Statusspalte sein.")

    kept: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("PayPal-Zeilen muessen Woerterbuecher sein.")
        if _is_helper_row(row):
            continue
        status = _status_value(row, status_field)
        if _status_is_known_incomplete(status):
            continue
        kept.append(row)
    return kept


def _status_is_known_incomplete(status: str | None) -> bool:
    """True nur fuer einen explizit bekannten, nicht abgeschlossenen Status.

    Unbekannte, fehlende und leere Statuswerte gelten als unbekannt und werden
    konservativ behalten (F-IMP-08). ``COMPLETED_STATUSES`` hat Vorrang, falls
    ein Export denselben Wert in beiden Listen fuehrt.
    """
    if status is None or status in COMPLETED_STATUSES:
        return False
    return status in INCOMPLETE_STATUSES


def _is_helper_row(row: Mapping[str, Any]) -> bool:
    """True, wenn die Zeile eine bekannte PayPal-Hilfszeile ist."""
    for field_name in _TYPE_FIELDS:
        raw = _field_value(row, field_name)
        if raw is not None and _matches_helper_pattern(raw, substring=True):
            return True

    for field_name in _DESCRIPTION_FIELDS:
        raw = _field_value(row, field_name)
        if raw is not None and _matches_helper_pattern(raw, substring=False):
            return True

    return False


def _matches_helper_pattern(value: object, *, substring: bool) -> bool:
    """Prueft einen Text gegen die Hilfszeilen-Muster."""
    if not isinstance(value, str):
        return False
    text = _normalise_text(value)
    if not text:
        return False
    if substring:
        return any(pattern in text for pattern in HELPER_ROW_PATTERNS)
    return any(text == pattern or text.startswith(pattern) for pattern in HELPER_ROW_PATTERNS)


def _status_value(row: Mapping[str, Any], status_field: str) -> str | None:
    """Liest und normalisiert den Status einer Zeile; ``None`` heisst unbekannt."""
    raw = _field_value(row, status_field)
    if not isinstance(raw, str):
        return None
    status = _normalise_text(raw)
    return status or None


def _field_value(row: Mapping[str, Any], field_name: str) -> object | None:
    """Liest ein Feld exakt oder ersatzweise ohne Beachtung der Schreibweise."""
    if field_name in row:
        return cast(object, row[field_name])
    target = _normalise_text(field_name)
    for key, value in row.items():
        if isinstance(key, str) and _normalise_text(key) == target:
            return cast(object, value)
    return None


def _normalise_text(value: str) -> str:
    """Vereinheitlicht Status- und Typwerte fuer den Mustervergleich."""
    text = value.strip().casefold().translate(_UMLAUTS)
    text = text.replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    return text.strip(" \t\r\n.,;:")


#: Vorverarbeitungsschritte fuer Import-Vorlagen: Flag -> Filterfunktion.
#: Der Aufrufer (Import-Assistent, T-056-Vorlage) waehlt hierueber den Filter,
#: bevor er :func:`finue.services.mapping.apply_mapping` ausfuehrt.
PRE_FILTERS: dict[str, PreFilter] = {PAYPAL_PRE_FILTER: filter_paypal_rows}
