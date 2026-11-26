"""Normalisierte Suchspalte einer Buchung (F-SUC-01, F-SUC-06, F-ABS-05).

Die Suche laeuft nach F-SUC-06 portable ueber ``LIKE`` auf einer normalisierten
Spalte statt ueber datenbankspezifische Volltext-Erweiterungen. Dieses Modul ist
die einzige Stelle, die diese Spalte aufbaut:

* :func:`build_search_text` erzeugt den Inhalt der Spalte ``search_text`` aus
  Gegenpartei, Verwendungszweck, Notiz, Gegenpartei-IBAN und externer Referenz
  (F-SUC-01, F-ABS-05). Die IBAN wird zusaetzlich ohne Leerzeichen angehaengt,
  damit eine Suche mit der kompakten Schreibweise trifft.
* :func:`normalize_query_term` normalisiert einen Suchbegriff mit exakt
  derselben Regel; die Suchschicht T-092 nutzt das, damit Begriff und
  Spalteninhalt deckungsgleich verglichen werden.
* :func:`refresh_search_text` baut die Spalte aus dem aktuellen Stand einer
  gespeicherten Buchung neu. Notiz- und Referenzaenderungen (T-094, T-096)
  rufen es auf, weil beide Felder Teil der Suchspalte sind.

Normalisierung je Baustein (identisch fuer Spalte und Suchbegriff):

1. Unicode NFC.
2. ``casefold``. Das ist die Gross-/Kleinschreibungs-unabhaengige Form und
   bildet das scharfe s (``ß``) auf ``ss`` ab, sodass ``Strasse`` und
   ``Straße`` denselben Treffer ergeben.
3. Zeilenumbrueche und Mehrfach-Leerzeichen zu genau einem Leerzeichen
   zusammenfassen (ein Umbruch trennt zwei Woerter, nicht Zeichen).
4. Trimmen.

N-DB-01: Es wird kein SQL erzeugt; dieses Modul ist reine Textverarbeitung.
N-SEC-02: Die Suchspalte enthaelt nur Buchungstexte, keine Geheimnisse.
"""

from __future__ import annotations

import re
import unicodedata

from finue.models.transaction import Transaction

__all__ = [
    "build_search_text",
    "normalize_query_term",
    "normalize_search_text",
    "refresh_search_text",
]

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_search_text(value: str | None) -> str:
    """Normalisiert einen Textbaustein fuer die Suche (F-SUC-01, F-SUC-06).

    Args:
        value: Rohtext aus Buchung oder Suchfeld. ``None`` und der leere
            String ergeben einen leeren String.

    Returns:
        NFC-normalisierter, per ``casefold`` kleingeschriebener Text ohne
        Zeilenumbrueche, mit zusammengefassten Leerzeichen und ohne Rand-
        Leerraum.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFC", value)
    text = text.casefold()
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def normalize_query_term(term: str | None) -> str:
    """Normalisiert einen Suchbegriff mit derselben Regel wie die Suchspalte.

    Die Suchschicht (T-092) nutzt das, damit ein Begriff nach derselben Regel
    wie der Inhalt von ``search_text`` verglichen wird (F-SUC-01).
    """
    return normalize_search_text(term)


def build_search_text(
    counterparty: str | None = None,
    purpose: str | None = None,
    note: str | None = None,
    counterparty_iban: str | None = None,
    external_ref: str | None = None,
) -> str:
    """Baut die normalisierte Suchspalte einer Buchung (F-SUC-01, F-ABS-05).

    Durchsucht werden nach F-SUC-01 Gegenpartei, Verwendungszweck, Notiz und
    IBAN; nach F-ABS-05 ist die optionale externe Referenz ebenfalls
    durchsuchbar. Leere Bausteine entfallen. Die Gegenpartei-IBAN wird
    zusaetzlich ohne Leerzeichen angehaengt, damit eine kompakte Eingabe
    trifft.

    Args:
        counterparty: Gegenpartei.
        purpose: Verwendungszweck.
        note: Nutzernotiz.
        counterparty_iban: Gegenpartei-IBAN, ggf. mit Leerzeichen.
        external_ref: Optionale externe Referenz (F-ABS-05).

    Returns:
        Ein mit einzelnen Leerzeichen verbundener, normalisierter Text. Sind
        alle Felder leer, ist das Ergebnis der leere String.
    """
    parts: list[str] = []
    for value in (counterparty, purpose, note, counterparty_iban, external_ref):
        normalized = normalize_search_text(value)
        if normalized:
            parts.append(normalized)

    normalized_iban = normalize_search_text(counterparty_iban)
    compact_iban = normalized_iban.replace(" ", "")
    if compact_iban and compact_iban != normalized_iban:
        parts.append(compact_iban)

    return " ".join(parts)


def refresh_search_text(transaction: Transaction) -> str:
    """Baut ``search_text`` aus dem aktuellen Stand neu (F-SUC-01, F-ABS-05).

    Notiz- und Referenzaenderungen (T-094, T-096) rufen das auf, weil beide
    Felder Teil der Suchspalte sind und sonst veralten wuerden.

    Args:
        transaction: Gespeicherte oder neu anzulegende Buchung mit allen
            Quellfeldern.

    Returns:
        Der neu gesetzte Inhalt der Suchspalte.
    """
    text = build_search_text(
        counterparty=transaction.counterparty,
        purpose=transaction.purpose,
        note=transaction.note,
        counterparty_iban=transaction.counterparty_iban,
        external_ref=transaction.external_ref,
    )
    transaction.search_text = text
    return text
