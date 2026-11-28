# T-112 Verifier-Bericht

Urteil: **PASS** (Commit 08c71f6 inkl. 50b8e57/7401699, sauberer Detached-Worktree, unabhaengig erneut ausgefuehrt, 2026-11-27).

## Geprueft
- Cancel vor erster TAN-Pause: sync_service faengt SyncCancelledError/TanTimeoutError beim ersten next(generator) ab und meldet `abgebrochen` bzw. `zeitueberschreitung`; Abbruchmarke nach Rueckkehr des Netzaufrufs beendet den Lauf regulaer (Test test_abbruch_vor_erster_tan_pause_f_syn_10, Status `abgebrochen`, nicht `fehler`).
- Timeout: Waechter-Test (`zeitueberschreitung`, Registry und Sperre frei), Session-Release-Test mit Pool checkedout==0.
- Leerzeichen-TAN: `Field(pattern=r"\S")` -> 422 `validation_error`; Antwort ohne Eingabewert (localize_validation_errors gibt nur Feld/Meldung aus); Abfrage bleibt offen, gueltige TAN setzt fort.
- RAM-only Dialogzustand, keine Session/ORM-Objekte im Laufzustand, PIN/TAN-Leckagetests (Antworten, Logs, Ereignisse, DB, repr) gruen; Pool checkedout==0 in API- und Release-Tests.
- Auth/Vertrag (test_api_contract), 409 bei zweiter TAN, Doppelstart, read-only FinTS (Fake-Client).

## Laeufe (Worktree 08c71f6, clean)
- pytest test_sync_api, test_sync_session_release, test_api_contract, test_sync_tan, test_sync_secrets, test_sync_service: 64 passed
- pytest -m integration tests/integration: 17 passed, 1 deselected
- pytest tests/unit: 1468 passed, 1 skipped
- ruff check: ok; ruff format --check: 199 formatiert; mypy backend: no issues (96 Dateien)
- Worktree nach den Laeufen weiterhin sauber.

Keine Defekte, keine Korrekturen noetig.
