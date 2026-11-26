"""Prueft die Pflichtabschnitte des Owner-Live-Tests fuer FinTS (T-082).

Referenziert F-SYN-01..08, F-SYN-10, F-AUF-05, F-AUF-07 und N-SEC-01..03.
Der Test liest nur das Markdown-Dokument; er fuehrt keinen Bankzugriff aus und
enthaelt keine echten Zugangsdaten (D8).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOC_PATH: Final[Path] = PROJECT_ROOT / "docs" / "live-test-fints.md"

#: Pflichtabschnitte der zweiten Ebene in der erwarteten Reihenfolge.
REQUIRED_SECTIONS: Final[tuple[str, ...]] = (
    "## Zweck und Grenzen",
    "## Voraussetzungen",
    "## Vorbereitung",
    "## Testfaelle",
    "## Erwartete Nachweise",
    "## Sicherheitsregeln waehrend des Tests",
    "## Offene Owner-Eingaben",
    "## Stand F-AUF-07",
    "## Ergebnisprotokoll",
    "## Nach dem Test",
)

#: Pflichtszenarien innerhalb des Abschnitts "Testfaelle".
REQUIRED_SCENARIOS: Final[tuple[str, ...]] = (
    "### Erstsync ohne TAN",
    "### TAN-Eingabe",
    "### App-Freigabe",
    "### Batch-Sync mit mehreren Konten",
    "### Abbruch",
    "### Zeitueberschreitung (Timeout)",
    "### ING-90-Tage-Regel",
    "### Falsche PIN (nur ein Versuch)",
    "### PIN-Speicheroption (F-SYN-08)",
    "### Wiederholbare Bankcodes 9800 und 9010 (beobachten)",
)

#: Begriffe, die Voraussetzungen und Sicherheitsregeln belegen muessen.
REQUIRED_TERMS: Final[tuple[str, ...]] = (
    "FINTS_PRODUCT_ID",
    "KEY_FILE",
    "FINUE_KEY_FILE",
    "fints_url",
    "store_pin",
    "Fernet",
    "MissingEndpointError",
    "ING90",
    "F-AUF-07",
    "erfolgreich",
    "wartet_auf_tan",
    "teilweise",
    "abgebrochen",
    "zeitueberschreitung",
    "PIN nicht akzeptiert",
    "FinTS-Zugriff nicht erlaubt",
    "Keine Auftragsdaten von der Bank verfuegbar",
    "Nur der Owner",
    "keine echten Zugangsdaten",
)

#: Lange Hex-Tokens waeren Produkt-IDs oder Schluessel und gehoeren nicht in die Doku.
_HEX_SECRET_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b[0-9A-Fa-f]{20,}\b")


def read_doc() -> str:
    """Liest das Live-Test-Dokument als UTF-8."""
    assert DOC_PATH.is_file(), f"Live-Test-Dokument fehlt: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def test_dokument_existiert() -> None:
    """docs/live-test-fints.md ist vorhanden und nicht leer (T-082)."""
    text = read_doc()
    assert text.strip(), "Live-Test-Dokument ist leer."


def test_pflichtabschnitte_vorhanden_und_geordnet() -> None:
    """Alle Pflichtabschnitte stehen in der vorgesehenen Reihenfolge."""
    text = read_doc()
    positions = []
    for heading in REQUIRED_SECTIONS:
        index = text.find(heading)
        assert index >= 0, f"Pflichtabschnitt fehlt: {heading}"
        positions.append(index)
    assert positions == sorted(positions), "Pflichtabschnitte stehen nicht in der erwarteten Reihenfolge."


def test_alle_testszenarien_vorhanden() -> None:
    """Erstsync, TAN, App-Freigabe, Batch, Abbruch, Timeout, ING, falsche PIN usw. fehlen nicht."""
    text = read_doc()
    missing = [scenario for scenario in REQUIRED_SCENARIOS if scenario not in text]
    assert not missing, "Folgende Testszenarien fehlen: " + ", ".join(missing)


def test_voraussetzungen_und_mapping_begriffe() -> None:
    """Produkt-ID, Schluesseldatei, Endpunkt, PIN-Option und Statuswerte sind benannt."""
    text = read_doc()
    missing = [term for term in REQUIRED_TERMS if term not in text]
    assert not missing, "Folgende Pflichtbegriffe fehlen: " + ", ".join(missing)


def test_sicherheitsregeln_und_offene_eingaben() -> None:
    """Nur der Owner testet live; offene Eingaben und F-AUF-07-Stand sind beschrieben."""
    text = read_doc()
    assert "Nur der Owner" in text, "Hinweis auf die Owner-Rolle fehlt."
    assert "keine echten Zugangsdaten" in text, "Hinweis auf erfundene Fixtures fehlt."
    assert "PIN, TAN" in text or "PIN und TAN" in text, "Hinweis auf PIN/TAN in Logs fehlt."
    assert "Produkt-ID" in text, "Offene Owner-Eingabe Produkt-ID fehlt."
    assert "Bank-Endpunkte" in text, "Offene Owner-Eingabe Bank-Endpunkte fehlt."
    assert "Fehlercodes mit Beleg" in text, "Offene Owner-Eingabe Fehlercodes fehlt."


def test_keine_hex_geheimnisse_im_dokument() -> None:
    """Die Doku enthaelt keine langen Hex-Tokens (Produkt-ID, Schluessel)."""
    text = read_doc()
    match = _HEX_SECRET_PATTERN.search(text)
    assert match is None, f"Moegliches Geheimnis im Dokument: {match.group(0) if match else ''}"
