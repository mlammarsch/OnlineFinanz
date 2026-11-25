"""Meta-Test: Anforderungs-IDs müssen in den Tests referenziert sein.

Setzt die Review-Checkliste aus AGENTS.md (Abschnitt "Review-Checkliste fuer
verifier": Anforderungs-IDs in Tests referenziert) um. Dieser Test stellt
sicher, dass die nicht-funktionalen Anforderungen aus den Bereichen
Datenhaltung (N-DB) und Betrieb (N-OPS) in mindestens einer echten Testdatei
unter tests/ namentlich genannt werden.

Die Meta-Testdatei schliesst sich selbst von der Suche aus, damit die Pflicht-IDs
ausschliesslich aus Fachtests stammen und nicht trivial durch die Auflistung in
dieser Datei erfuellt werden.
"""

from pathlib import Path

# Wurzelverzeichnis tests/ (eine Ebene über tests/unit/).
TESTS_DIR = Path(__file__).resolve().parent.parent

# Diese Meta-Testdatei wird von der Suche ausgeschlossen.
SELF = Path(__file__).resolve()

# Pflicht-IDs: nicht-funktionale Anforderungen mit Bezug zu Datenhaltung (N-DB)
# und Betrieb (N-OPS). Jede muss mindestens einmal als Substring in einer echten
# Testdatei unter tests/ vorkommen.
REQUIRED_IDS = (
    "N-DB-01",
    "N-DB-02",
    "N-DB-03",
    "N-DB-04",
    "N-OPS-01",
    "N-OPS-04",
    "N-OPS-05",
    "N-OPS-06",
)


def _collect_test_sources() -> dict[Path, str]:
    """Liest alle .py-Dateien unter tests/ (ohne diese Meta-Datei) als utf-8."""
    sources: dict[Path, str] = {}
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == SELF:
            continue
        sources[path] = path.read_text(encoding="utf-8")
    return sources


def test_alle_pflicht_ids_sind_in_tests_belegt() -> None:
    """Jede Pflicht-ID kommt in mindestens einer echten Testdatei vor."""
    sources = _collect_test_sources()
    assert sources, f"Keine Testdateien unter {TESTS_DIR} gefunden."

    missing = [req_id for req_id in REQUIRED_IDS if not any(req_id in text for text in sources.values())]

    assert not missing, "Folgende Anforderungs-IDs werden in keiner Testdatei unter tests/ referenziert: " + ", ".join(
        missing
    )
