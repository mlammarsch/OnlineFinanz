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

Inhalt je Milestone: siehe diese Tabelle und `SPEC.md`. Stufen von `validate.py` ab m1 werden im ersten Task des
jeweiligen Milestones implementiert; bis dahin endet die Stufe mit Exit 1 ("Stufe nicht implementiert").

Besonderheiten:
- M3 Task 1: Machbarkeitspruefung F-AUF-07 (Ergebnis in `decisions.md`, SPEC-Aenderung nur mit Owner-Freigabe).
- M3 Abnahme: Mock-Tests gruen und `docs/live-test-fints.md` vorhanden (Owner-Aufgabe in `handoff.md`).
- M10 Abnahme erst bei laufendem Docker Desktop. Docker-Hub-Push als separater Task mit Risiko `high`.
- Playwright-Browser in M5 per `uv run playwright install chromium` installieren.

## M2 Taskplan (G-003, T-044 bis T-059)

Modellrouting: jeder `builder`-Task laeuft mit **DeepSeek V4.1 Flash, Reasoning-Effort max** (owner im Task:
`builder-deepseek`). Tasks mit `risk_level: medium` oder `high` werden zusaetzlich von `verifier` (Claude Sonnet,
unabhaengige Pruefung) nachgeprueft, bevor `hx.py task-done` greift -- Pflicht laut `AGENTS.md`/Harness-Regel
4. In diesem Milestone sind 14 von 16 Tasks `medium` (Dubletten-, Import- und Versionslogik; vgl.
`AGENTS.md`-Risikostufen: Dublettenlogik ist explizit `medium`). Nur T-044 (validate.py-Geruest), T-056
(Vorlagen-CRUD) und T-057 (PayPal-Filter) sind `low` und koennen ohne zweite Pruefung direkt ueber
`hx.py task-done` abgeschlossen werden, sofern der Verifikationsbefehl gruen ist.

Ausfuehrungswellen (Tasks je Welle sind voneinander unabhaengig und koennen auf parallelen `builder`-Sessions
laufen; eine Welle startet erst, wenn alle Abhaengigkeiten der Vorwelle als `done` verifiziert sind):

| Welle | Tasks | Parallelitaet | Abhaengig von | Risiko |
|---|---|---|---|---|
| 1 | T-044 (Stufe m2 in validate.py) | solo | - | low |
| 2 | T-045 (Normalisierung/Fingerprint F-DUP-01), T-048 (CSV-Erkennung F-IMP-02), T-050 (JSON-Schema v1 F-EXP-04) | 3x parallel | T-044 | medium, low, medium |
| 3 | T-046 (Multimengen-Abgleich F-DUP-02), T-049 (Mapping-Engine F-IMP-03) | 2x parallel | T-045 bzw. T-048 | medium, medium |
| 4 | T-047 (Verdachtsdubletten F-DUP-03), T-051 (JSON-Import eigenes Format F-IMP-04), T-052 (Versionskonvertierung F-EXP-07), T-056 (Mapping-Vorlagen F-IMP-05), T-057 (PayPal-Filter F-IMP-08) | bis 5x parallel | T-046 bzw. T-050 bzw. T-049 | medium, medium, medium, low, low |
| 5 | T-053 (Import-Vorschau-Service F-IMP-06) | solo | T-047, T-049, T-051 | medium |
| 6 | T-054 (Import-Durchfuehrung + ImportRun-Protokoll F-IMP-06/F-SYN-07) | solo | T-053 | medium |
| 7 | T-055 (Kennzeichen-Uebernahme F-IMP-07), T-058 (roundtrip-Block Import-Teil) | 2x parallel | T-054 (T-058 zusaetzlich T-051, T-052) | medium, low |
| 8 | T-059 (M2 Abnahme) | solo | alle T-044..T-058 | medium |

Hinweise:
- T-049 ist bewusst nach T-048 statt nach T-045 gereiht (gleiche Datei-/Themenfamilie CSV, weniger Merge-Risiko bei
  paralleler Bearbeitung durch verschiedene Builder-Sessions als Querverdrahtung zur Dubletten-Kette).
- T-053 buendelt drei Vorketten (Dubletten F-DUP-03, Mapping F-IMP-03, JSON-Import F-IMP-04) und ist deshalb ein
  harter Synchronisationspunkt vor der schreibenden Import-Logik (T-054) -- hier lohnt sich keine weitere
  Parallelisierung, da alle drei Ergebnisse in einer Funktion zusammenlaufen.
- Owner-Eingabe weiterhin offen (siehe `handoff.md`): echte CSV-Beispieldateien N26/Easybank/PayPal. T-056/T-057
  bauen deshalb nach SPEC Abschnitt 3 "nach bestem Wissen, unbelegt" und sind bei Eintreffen der Dateien in M4 oder
  per Nachtrag zu korrigieren (keine Blockade, siehe `AGENTS.md`-Edge-Case-Regel).
- `scripts/validate.py` STAGE_M2 schliesst den `roundtrip`-Block (Import-Teil) nach T-058 aktiv mit ein, da
  `plan.md`-Abnahmestufe fuer M2 explizit "`validate.py m2` (+ roundtrip Import-Teil)" verlangt.

## M3 Taskplan (G-004, T-060 bis T-083)

M3 startet mit der Machbarkeitspruefung F-AUF-07. Der Ausgang ist ein Gate:
Bei positivem Ergebnis werden T-075 und T-079 ausgefuehrt; bei negativem Ergebnis wird nur T-077 nach
Owner-Freigabe fuer die SPEC-Zurueckstellung ausgefuehrt. Der nicht gewaehlte Zweig wird ueber `hx.py task-set`
als `cancelled` beendet und nicht als technische Blockade behandelt.

| Welle | Tasks | Abhaengigkeiten | Risiko |
|---|---|---|---|
| 1 | T-060 Machbarkeit F-AUF-07 und Abhaengigkeiten | - | medium |
| 2 | T-061 validate-m3, T-062 PIN-Fernet, T-063 Modelle/Migration 0002, T-064 Job-Registry/Sperre, T-065 FinTS-Typen | T-060 | low, medium, medium, medium, low |
| 3 | T-066 Fehler-Mapping, T-067 Fake-Client, T-068 camt/MT940, T-069 Vormerkungen, T-070 Retry | T-063/T-065 bzw. T-060 | medium, low, medium, medium, medium |
| 4 | T-071 Bankzugang, T-072 Read-only-Guard, T-073 FinTS-Adapter, T-074 Sync-Persistenz | jeweilige Welle 2/3 | medium, low, medium, medium |
| 5 | T-075 Auftragsabruf bei positivem Gate, T-076 Sync-Orchestrierung, T-077 SPEC-Zurueckstellung bei negativem Gate | Gate/Abhaengigkeiten | medium, medium, high |
| 6 | T-078 TAN-Pause/Resume | T-076 | medium |
| 7 | T-079 Auftragsbestand bei positivem Gate, T-080 Geheimnis-Leckagetests, T-081 Sync-Integration, T-082 Live-Test-Doku | T-075/T-078 bzw. T-078 | medium, medium, medium, low |
| 8 | T-083 M3-Abnahme und Aufraeumen | gewaehlter Gate-Zweig sowie T-060..T-074, T-076, T-078, T-080..T-082 | medium |

Alle Builder-Tasks laufen mit DeepSeek V4.1 Flash, Effort max. Alle medium/high Tasks erhalten vor `task-done`
eine unabhaengige Verifier-Pruefung mit Claude Sonnet 5.5, Effort medium. Low-Risk-Tasks koennen nach ihrem
Verifikationsbefehl direkt abgeschlossen werden. FinTS bleibt lesend; Tests verwenden ausschliesslich Fake-Clients
und Mocks, echte Bankzugriffe und Secrets sind ausgeschlossen. T-077 ist high risk und braucht `task-approve`.
