"""Meta-Test: Anforderungs-IDs müssen in den Tests referenziert sein.

Setzt die Review-Checkliste aus AGENTS.md (Abschnitt "Review-Checkliste fuer
verifier": Anforderungs-IDs in Tests referenziert) um. Dieser Test stellt
sicher, dass die nicht-funktionalen Anforderungen aus den Bereichen
Datenhaltung (N-DB), Betrieb (N-OPS) und Sicherheit (N-SEC) sowie die
Abnahme-IDs der Milestones M2 (F-DUP, F-IMP, F-EXP-04/07), M3 (Sync F-SYN,
Auftraege F-AUF-04/05/07, Gleichzeitigkeit F-CON-05) und M4 (Konten und Gruppen
F-KTO-01..07/10/11, Buchungsliste F-BUC-01..06, Dublettenauflösung F-DUP-05,
Kennzeichen/Notiz/Abstimmung F-ABS-01..06, Suche F-SUC-01..07, Export
F-EXP-01..07, Gleichzeitigkeit F-CON-01..06, Performance N-PERF-01) in
mindestens einer echten Testdatei unter tests/ namentlich genannt werden.

Die Meta-Testdatei schliesst sich selbst von der Suche aus, damit die Pflicht-IDs
ausschliesslich aus Fachtests stammen und nicht trivial durch die Auflistung in
dieser Datei erfuellt werden.
"""

from pathlib import Path

# Wurzelverzeichnis tests/ (eine Ebene über tests/unit/).
TESTS_DIR = Path(__file__).resolve().parent.parent

# Diese Meta-Testdatei wird von der Suche ausgeschlossen.
SELF = Path(__file__).resolve()

# Pflicht-IDs: nicht-funktionale Anforderungen mit Bezug zu Datenhaltung (N-DB),
# Betrieb (N-OPS) und Sicherheit (N-SEC), die funktionalen Abnahme-IDs des
# Milestones M2 (Dubletten F-DUP, Import F-IMP, Export-Schema F-EXP-04/07), die
# Abnahme-IDs des Milestones M3 (Sync F-SYN-01..11, Auftraege F-AUF-04/05/07,
# Gleichzeitigkeit F-CON-05, Sicherheit N-SEC-01..03) und die Abnahme-IDs des
# Milestones M4 (Konten und Gruppen F-KTO-01..07/10/11, Buchungsliste
# F-BUC-01..06, Dublettenaufloesung F-DUP-05, Kennzeichen/Notiz/Abstimmung
# F-ABS-01..06, Suche F-SUC-01..07, Export F-EXP-01..07, Gleichzeitigkeit
# F-CON-01..06, Performance N-PERF-01). Jede muss mindestens einmal als
# Substring in einer echten Testdatei unter tests/ vorkommen.
REQUIRED_IDS = (
    "N-DB-01",
    "N-DB-02",
    "N-DB-03",
    "N-DB-04",
    "N-OPS-01",
    "N-OPS-04",
    "N-OPS-05",
    "N-OPS-06",
    "F-DUP-01",
    "F-DUP-02",
    "F-DUP-03",
    "F-DUP-04",
    "F-IMP-01",
    "F-IMP-02",
    "F-IMP-03",
    "F-IMP-04",
    "F-IMP-05",
    "F-IMP-06",
    "F-IMP-07",
    "F-IMP-08",
    "F-EXP-04",
    "F-EXP-07",
    "F-SYN-01",
    "F-SYN-02",
    "F-SYN-03",
    "F-SYN-04",
    "F-SYN-05",
    "F-SYN-06",
    "F-SYN-07",
    "F-SYN-08",
    "F-SYN-09",
    "F-SYN-10",
    "F-SYN-11",
    "F-AUF-04",
    "F-AUF-05",
    "F-AUF-07",
    "F-CON-05",
    # M4: Konten und Gruppen (F-KTO-01..07, F-KTO-10, F-KTO-11).
    "F-KTO-01",
    "F-KTO-02",
    "F-KTO-03",
    "F-KTO-04",
    "F-KTO-05",
    "F-KTO-06",
    "F-KTO-07",
    "F-KTO-10",
    "F-KTO-11",
    # M4: Buchungsliste mit Vormerkungen, Tagessalden, Gruppenansicht (F-BUC-01..06).
    "F-BUC-01",
    "F-BUC-02",
    "F-BUC-03",
    "F-BUC-04",
    "F-BUC-05",
    "F-BUC-06",
    # M4: Aufloesung von Verdachtsdubletten (F-DUP-05).
    "F-DUP-05",
    # M4: Kennzeichen, Notiz, Abstimmung (F-ABS-01..06).
    "F-ABS-01",
    "F-ABS-02",
    "F-ABS-03",
    "F-ABS-04",
    "F-ABS-05",
    "F-ABS-06",
    # M4: Suche (F-SUC-01..07).
    "F-SUC-01",
    "F-SUC-02",
    "F-SUC-03",
    "F-SUC-04",
    "F-SUC-05",
    "F-SUC-06",
    "F-SUC-07",
    # M4: Export CSV/JSON/Markdown und Profile (F-EXP-01..07; F-EXP-04/07 oben).
    "F-EXP-01",
    "F-EXP-02",
    "F-EXP-03",
    "F-EXP-05",
    "F-EXP-06",
    # M4: Gleichzeitigkeit und Konsistenz (F-CON-01..06; F-CON-05 oben).
    "F-CON-01",
    "F-CON-02",
    "F-CON-03",
    "F-CON-04",
    "F-CON-06",
    # M4: Performance bei 100.000 Buchungen (N-PERF-01).
    "N-PERF-01",
    "N-SEC-01",
    "N-SEC-02",
    "N-SEC-03",
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
