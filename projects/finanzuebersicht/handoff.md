# Handoff

## Next actions

- **M2-Taskplan angelegt (G-003, T-044 bis T-059, 16 Tasks).** Noch keine Task gestartet. Ausfuehrungswellen,
  Parallelitaet und Modellrouting stehen in `plan.md` Abschnitt "M2 Taskplan". Kurzfassung: Welle 1 (T-044 solo)
  -> Welle 2 (T-045/T-048/T-050, 3x parallel) -> Welle 3 (T-046/T-049, 2x parallel) -> Welle 4 (T-047/T-051/T-052/
  T-056/T-057, bis 5x parallel) -> Welle 5 (T-053 solo, Synchronisationspunkt) -> Welle 6 (T-054 solo) -> Welle 7
  (T-055/T-058, 2x parallel) -> Welle 8 (T-059 Abnahme solo). Jeder `builder`-Task laeuft mit DeepSeek V4.1 Flash,
  Reasoning-Effort max. 13 von 16 Tasks sind `risk_level: medium` (Dubletten-/Import-/Versionslogik) und brauchen
  vor `task-done` eine unabhaengige `verifier`-Pruefung; nur T-044, T-056, T-057 sind `low`. Naechster Schritt:
  Welle 1 (T-044) claimen und per `builder` umsetzen, danach Welle 2 mit bis zu drei parallelen Builder-Sessions.
  Owner-Eingabe weiterhin offen: echte CSV-Beispieldateien N26/Easybank/PayPal/Finanzblick-Export fehlen -- T-056/
  T-057 bauen deshalb "nach bestem Wissen, unbelegt" und sind spaeter mit echten Dateien nachzuschaerfen.
- **T-044 abgeschlossen und unabhaengig verifiziert (PASS).** `scripts/validate.py` registriert `STAGE_M2` leer in
  `IMPLEMENTED`, waehrend `roundtrip` als eigener, noch nicht implementierter Block erhalten bleibt. Der explizite
  Konflikt-Check gegen SPEC.md und plan.md war **Ja**; `python scripts/validate.py m2` lief mit Exit 0. Naechster
  Schritt: Welle 2 mit T-045, T-048 und T-050 ueber getrennte Builder routen; T-045 und T-050 danach jeweils mit
  unabhaengigem Verifier pruefen, T-048 ist laut Taskplan low-risk.
- **M1 (Fundament) VOLLSTAENDIG ABGENOMMEN.** 41 done + 2 cancelled = 43/43 Tasks, status.md/queues.md leer (now/next/blocked). T-042 (Anforderungs-IDs) und T-043 (M1-Abnahme) in dieser Session abgeschlossen:
  - T-042: Alle 8 Pflicht-IDs (N-DB-01..04, N-OPS-01/04/05/06) waren bereits in echten Fachtests belegt, keine Nachtraege noetig. Neue `tests/unit/test_requirement_refs.py` erzwingt das dauerhaft per grep-aehnlicher Pruefung ueber alle Dateien in `tests/` (schliesst sich selbst aus). Evidenz: artifacts/T-042-verify-1795637499.json.
  - T-043: KRITISCHER FIX in `scripts/validate.py` noetig, sonst waere die Abnahme nie gruen geworden: `check_lint/check_types/check_unit/check_smoke` riefen `[sys.executable, "-m", "uv", "run", ...]` auf - `uv` ist ein eigenstaendiges Executable, kein Python-Modul, das schlaegt mit JEDEM System-Python fehl ("No module named uv"). Fix: direkter Aufruf `["uv", "run", ...]` ueber PATH. Zusaetzlich `check_lint` um `scripts` ergaenzt (T-040-DoD verlangte `backend tests scripts`, das Skript prueft vorher nur `backend tests`). `python scripts/validate.py m1` lief dadurch zum ERSTEN MAL end-to-end gruen. Von verifier (Claude Sonnet 5.5) unabhaengig als PASS bestaetigt, inkl. Review-Checkliste (keine Secrets, kein rohes SQL, keine Kategorie/Budget/Ueberweisung-Reste, Anforderungs-IDs referenziert, git status unauffaellig). Evidenz: artifacts/T-043-verify-1795637851.json.
  - Kleiner, nicht blockierender Kosmetik-Hinweis vom Verifier: `IMPLEMENTED["m1"]` kombiniert `STAGE_M0 + STAGE_M1`, wodurch die m0-Checks bei `validate.py m1` doppelt ausgegeben werden (keine Fehlinformation, nur redundante Ausgabe) - bei Gelegenheit auf `IMPLEMENTED["m1"] = STAGE_M1` umstellen.
   - **HISTORISCHER M1-HINWEIS:** M1 wurde noch nach der damaligen Einzel-Commit-Regel abgeschlossen. Fuer M2 und folgende Milestones gilt die aktualisierte DEV-Branch-Regel aus `AGENTS.md`: eigener DEV-Branch, unmittelbare Worker-/Verifier-Commits und Pushes in den DEV-Pfad ohne erneute Freigabe; Merge nach `main` nur nach `task-approve`.
  - Danach: Goal G-002 (M1) ist fachlich fertig. Naechstes Ziel laut plan.md waere M2 - Planer-Lauf fuer M2-Tasks noetig, sobald der Owner den Commit freigegeben hat und "weiter mit M2" sagt.
- T-041 (M1 mypy strict) abgeschlossen und verifiziert. `uv run mypy backend` = Success, 0 Fehler in 30 Dateien (strict). Der Code war bereits typ-sauber; einziger DoD-Verstoss war ein undokumentierter `# type: ignore[no-untyped-def]` in models/audit.py:110 (SQLAlchemy-Event-Listener) - ersetzt durch konkrete Annotation (Mapper[AuditLog], Connection, target: AuditLog), ignore entfernt. Backend enthaelt jetzt 0 `type: ignore`. ruff check backend = All checks passed; pytest tests/unit = 400 passed. Evidenz: projects/finanzuebersicht/artifacts/T-041-verify-1795637243.json. Naechster freier Task: T-042 (Anforderungs-IDs in Tests), danach T-043 (M1-Abnahme).
- T-040 (M1 Lint, ruff) abgeschlossen und verifiziert. Ist-Zustand: `ruff check` meldete nur 3 E501 in scripts/validate.py, `ruff format --check` wollte 19 Dateien reformatieren (u. a. die nachbearbeitete Migration 0001_initial.py, 688 Zeilen). `ruff check --fix` (nur sichere Fixes) + `ruff format` angewandt; danach `All checks passed!` und `62 files already formatted` (Exit 0). Keine noqa, keine Logikaenderung: alle 19 geaenderten Dateien sind per ast.dump-Vergleich AST-identisch zu HEAD. pytest tests: 401 passed. Evidenz: projects/finanzuebersicht/artifacts/T-040-verify-1795636920.json. Naechster freier Task: T-041 (M1 mypy strict) -> danach T-042, T-043 (M1-Abnahme).
- T-038 nachkorrigiert (Defekt aus unabhaengiger Pruefung): launch.json lief mit cwd=backend, tasks.json "Demo-Daten" im Repo-Root ohne DATABASE_URL - der relative Pfad `sqlite:///data/dev.db` loeste daher auf zwei verschiedene Dateien auf (backend/data/dev.db vs. data/finue.db), Demo-Daten fehlten im per F5 gestarteten Server. Fix: launch.json cwd=${workspaceFolder} + PYTHONPATH=${workspaceFolder}/backend, tasks.json options.cwd=${workspaceFolder} + options.env.DATABASE_URL=sqlite:///data/dev.db, docs/entwicklung.md angeglichen. tests/unit/test_vscode_config.py prueft jetzt cwd- und URL-Gleichheit beider Kontexte plus aufgeloesten Absolutpfad. Evidenz: projects/finanzuebersicht/artifacts/T-038-verify-1795635686.json. Naechster freier Task: T-039 (M1 Smoke-Test).
- T-021 bis T-029 und T-036 abgeschlossen (alle PASS ueber builder DeepSeek V4.1 Flash effort max + verifier Claude Sonnet 5.5, T-024/T-025/T-027/T-029 mit effort high wegen Sicherheits-/Strukturrelevanz). T-028+T-036 wurden ECHT PARALLEL bearbeitet (zwei Builder, zwei Verifier gleichzeitig, unabhaengige Dateien, keine Race Condition). T-029 (Erste Migration 0001) per autogenerate erzeugt, alle 15 Tabellen/Indizes/FKs per compare_metadata bestaetigt, downgrade() korrekt ohne Logik (N-DB-03). Naechster freier Task: T-030 (Migrations-Runner) - lineare Kette, keine Parallelisierung aktuell moeglich.
- WICHTIG vor T-031/T-032: env.py braucht einen render_item-Hook fuer finue.db.mixins.UtcDateTime, sonst muss der Import bei jeder kuenftigen Migration manuell nachgetragen werden (T-029 mmusste das einmalig von Hand fixen). Ausserdem test_alembic_config.py::test_initial_migration_is_single_head haertet den Head-Wert "0001" hart - bricht bei der naechsten Migration, vor T-031/T-032 generisch umstellen.
- T-029 bis T-038 abgeschlossen. T-031+T-032 (Migrationstests) und T-033+T-036/T-028 sowie T-034+T-037 und T-035+T-038 jeweils ECHT PARALLEL mit mehreren gleichzeitigen Buildern/Verifiern bearbeitet (insgesamt 3x Dreifach- bzw. Doppel-Parallelisierung in dieser Session), keine Race Conditions, keine Dateikonflikte.
- ZWEI ERSTPRUEFUNGEN FAIL, nach Nachbesserung PASS: (1) T-033 - upgrade_to_head im App-Lifespan loeste Alembics fileConfig aus und ersetzte das Projekt-Logging inkl. N-SEC-02-Secret-Filter durch einen nackten Alembic-Handler; gefixt in migrate.py/env.py (configure_logger-Attribut), zentral fuer alle Aufrufer. (2) T-038 - launch.json (cwd=backend) und der Demo-Daten-Task (cwd=Root, keine DATABASE_URL) zeigten auf zwei verschiedene SQLite-Dateien, Demo-Daten waeren im F5-Server nie sichtbar gewesen; gefixt durch einheitliches cwd=Workspace-Root plus expliziter DATABASE_URL in beiden Konfigurationen. Beide Faelle zeigen: die automatisierten pytest-Verifikationsplaene allein haetten die Mangel nicht gefunden, erst die gezielte Review-Checkliste des Verifiers.
- Status: 36/43 Tasks verifiziert erledigt. Naechster freier Task: T-039 (Smoke-Test). Danach lineare Schlusskette T-040 (Lint) -> T-041 (mypy strict) -> T-042 (Anforderungs-IDs) -> T-043 (M1-Abnahme).
- T-027 kritische, gut begruendete DoD-Korrektur in decisions.md dokumentiert: 6 statt 3 Tabellen ohne version-Spalte (zusaetzlich bank, user_session, mcp_token), bestaetigt gegen SPEC F-CON-02 und bereits abgenommene T-017/T-024.
- SPEC-Planungsluecken aus T-025-Verifikation (kein FAIL, DoD war korrekt umgesetzt, aber SPEC F-BAK-02/05/06 verlangt mehr): smb_domain-Feld bei BackupJob fehlt, encrypt_enabled-Flag + backup_password_encrypted (LargeBinary) fuer das Backup-Verschluesselungspasswort fehlt komplett, optional target_label an BackupRun fuer Ziel-Anzeige nach Job-Loeschung. Vor T-027/Migration (T-028/T-029) durch Planer klaeren, sonst folgt spaeter eine Nachtragsmigration.
- Weitere nicht-blockierende Hinweise aus T-025: backup.py size_bytes sollte vor erster Migration auf BigInteger (PostgreSQL/MySQL-Limit bei Integer ~2 GiB), CheckConstraint retention_count >= 1 fehlt.
- Hinweis aus T-028-Verifikation: env.py ist bisher nur ueber ScriptDirectory/Config-Ladbarkeit getestet, kein echter Migrationslauf (upgrade/downgrade) gegen eine Temp-DB. Bei T-029 oder T-031 nachholen.
- Kleine, nicht blockierende Anmerkung aus T-023-Verifikation: Docstring in import_template.py erwaehnt "passive_deletes", das weder gesetzt noch bei der viewonly-Relation noetig ist - bei Gelegenheit korrigieren.
- WICHTIG fuer Owner: Verifier von T-024 fand im Arbeitsverzeichnis zwei Aenderungen, die NICHT aus einem Harness-Task stammen: `AGENTS.md` (Kilo-Code-Abschnitt, bereits zu Sessionbeginn vorhanden) und `SPEC.md` (83 Zeilen, reine Markdown-Tabellen-Neuformatierung, inhaltlich unveraendert soweit geprueft). Keine Scope-Verletzung durch T-021..T-024, aber `SPEC.md`-Aenderungen sind laut AGENTS.md freigabepflichtig - bitte `git diff SPEC.md` pruefen und bewusst committen oder verwerfen (`git checkout -- SPEC.md`), bevor weitere Tasks laufen.
- Hinweis aus der Verifikation: vor T-027/T-028 `backend/finue/models/__init__.py` um Sammelimport aller Modellmodule ergaenzen, sonst fehlen Tabellen bei Alembic-Autogenerate (Improve-Queue).
- Auf Anweisung des Owners: "weiter mit M1" (Goal G-002). Ablauf siehe `.claude/agents/finanzuebersicht-lead.md`.
- M1 Task 1: Stufe `m1` in `scripts/validate.py` implementieren (lint, types, unit, smoke), Python 3.12 per `uv python install 3.12`.
- M3 Task 1: Machbarkeitspruefung F-AUF-07. `init_tan_response` und Codes fuer falsche PIN/Sperre belegen.
- M5: `uv run playwright install chromium`.

## Blockers

- Keine fuer M1. Docker-Engine laeuft nicht, erst fuer M10 noetig.

## Open questions (Owner-Eingaben, SPEC Abschnitt 7)

- FinTS-Produkt-ID (Live-Test FinTS, M3).
- Anonymisierte CSV-Beispiele N26, Easybank, PayPal, Finanzblick-Export in `data/samples/` (M2, M4).
- Bank-Icons bzw. Websites je Bank (M7).
- SMB-Ziel fuer Backups (Live-Test Backup, `docs/live-test-backup.md`, M8).
- Docker-Hub-Repository und `docker login` (M10).
- Bank-Endpunkte (nur ING als Owner-Vorgabe, unbelegt) und Fehlercodes: Quelle oder Live-Test noetig.
