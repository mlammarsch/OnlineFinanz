# T-108 Verifier-Bericht

Urteil: **PASS** (Commit ef9aed1, sauberer Detached-Worktree, unabhaengig erneut ausgefuehrt, 2026-11-27).

## Geprueft
- Edit-Kern `services/transaction_edit.py`: origin ui/mcp/import validiert (Fremdwert -> ValueError); ad hoc origin=import gesetzt und gespeichert.
- Setz-Semantik: gleicher Wert = No-op (kein Versionssprung, Zeitstempel/Herkunft unveraendert, kein Audit, kein Ereignis); ad hoc mit Folgeaufruf origin=ui belegt.
- Echte Aenderung: Zeitstempel (UTC), Herkunft, ein Audit-Eintrag (source=origin), Ereignis transaction.changed (Test).
- Vorgemerkt + reconciled=true -> 422 Klartext; checked auf vorgemerkt erlaubt; Zuruecksetzen erlaubt.
- expected_version: Konflikt mit current_version (Service ad hoc) und 409 mit aktuellem Stand (API-Test).
- GET detail: alle F-DET-01-Felder, latest_import_run (Art je Quelle, juengster Lauf, None ohne Lauf), 404, 422 bei ungueltigem Body.
- Auth: mit auth_required=True liefern GET sowie beide PUT 401 (ad hoc); Vertragstest require_user gruen, drei Routen im CONTRACT.
- Scope: Diff zu 6247eb9 nur routes.py, transactions.py, transaction_edit.py, docs/api.md, Tests, Harness-Dateien; keine Notiz/Referenz (T-109), kein Batch (T-110), keine Migration/Import/Sync-Aenderung.

## Laeufe (Worktree ef9aed1)
- pytest test_transaction_edit.py + test_api_contract.py: 30 passed
- pytest tests/unit: 1447 passed, 1 skipped
- ruff check: ok; ruff format --check: 191 formatiert; mypy backend: no issues (93 Dateien)

## Hinweis (nicht blockierend)
Kein eigener Test fuer origin="import" und 401 im Etappentest; ad hoc bestaetigt, Vertragstest deckt Auth strukturell ab.
