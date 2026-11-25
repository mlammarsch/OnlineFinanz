# Plan

Einzige Planungsquelle neben Goals, Tasks und `status.md`. Tasks entstehen je Milestone erst beim Start (`planner`).
Spaetere Milestones greifen nicht auf Anforderungen vorheriger Planung vor. Start ab M1 nur auf Anweisung "weiter mit M<n>".

| Milestone | Goal-ID | Abnahme-Stufe | Anforderungs-IDs |
|---|---|---|---|
| M0 Projekt-Harness | G-001 | `validate.py m0` | - |
| M1 Fundament | G-002 | `validate.py m1` (lint, types, unit, smoke) | N-DB-*, N-OPS-01, N-OPS-04..06 |
| M2 Import-Kern und Datei-Import | G-003 | `validate.py m2` (+ roundtrip Import-Teil) | F-DUP-01..04, F-IMP-*, F-EXP-04, F-EXP-07 |
| M3 FinTS-Sync | G-004 | `validate.py m3` (+ integration) | F-SYN-*, F-AUF-04/05/07, F-CON-05, N-SEC-01..03 |
| M4 Service-Schicht, REST-API, Suche, Export | G-005 | `validate.py m4` (+ roundtrip, perf, concurrency) | F-KTO-01..07,10,11, F-BUC-*, F-DUP-05, F-ABS-*, F-SUC-*, F-EXP-*, F-CON-*, N-PERF-01 |
| M5 Frontend-Grundlayout, Design-System | G-006 | `validate.py m5` (+ e2e Desktop und Mobile) | F-DES-*, F-UI-01..07, F-BUC-*, F-AUF-01..03 (bedingt), F-DET-01, F-MOB-01..06, 09, 12, 15 |
| M6 Frontend-Interaktion | G-007 | `validate.py m6` | F-SEL-*, F-DET-02, F-ABS-02..04, F-SUC-*, F-SYN-04, F-SYN-10, F-IMP-01, F-EXP-01..05, F-CON-02..04 |
| M7 Kontenverwaltung, Feinschliff | G-008 | `validate.py m7` (+ e2e Mobile inkl. Gesten) | F-KTO-*, F-DUP-03, F-DUP-05, F-SYN-09, F-AUF-06, F-UI-03, F-UI-08, F-CON-06, F-MOB-07, 08, 10, 11, 13, 14 |
| M8 Backup und Restore | G-009 | `validate.py m8` (+ backup) | F-BAK-*, F-UI-05 (Restore-Einstieg), N-DB-03 |
| M9 MCP-Server | G-010 | `validate.py m9` (+ mcp) | F-MCP-*, N-SEC-06 |
| M10 Sicherheit und Betrieb | G-011 | `validate.py m10` (+ docker, mcp gegen Container, SMB gegen Samba-Container) | N-SEC-*, N-OPS-02..03 |

Inhalt je Milestone: siehe `<milestones>` in `initialPrompt.md`. Stufen von `validate.py` ab m1 werden im ersten Task des
jeweiligen Milestones implementiert; bis dahin endet die Stufe mit Exit 1 ("Stufe nicht implementiert").

Besonderheiten:
- M3 Task 1: Machbarkeitspruefung F-AUF-07 (Ergebnis in `decisions.md`, SPEC-Aenderung nur mit Owner-Freigabe).
- M3 Abnahme: Mock-Tests gruen und `docs/live-test-fints.md` vorhanden (Owner-Aufgabe in `handoff.md`).
- M10 Abnahme erst bei laufendem Docker Desktop. Docker-Hub-Push als separater Task mit Risiko `high`.
- Playwright-Browser in M5 per `uv run playwright install chromium` installieren.
