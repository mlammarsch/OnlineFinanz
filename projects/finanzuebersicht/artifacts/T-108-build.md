# T-108 Build-Evidenz (M4-Entlastung, Edit-Service-Kern und Buchungsdetail)

Status: umgesetzt, Etappen-Verifikationsplan gruen; unabhaengige `verifier`-Pruefung (medium) offen.

## Umfang

Nur Edit-Service-Kern, Kennzeichen-Setz-Semantik, Versionspruefung, Buchungsdetail
und der zugehoerige API-Vertrag (F-ABS-01/02, F-CON-01/02, F-DET-01). Keine
Notiz-, externe-Referenz- oder Batch-Logik (T-109/T-110).

## Neu/geaendert

- `backend/finue/services/transaction_edit.py` (neu): `set_flag(...)` mit
  `origin` (ui/mcp/import), `expected_version`, `events`, `now`; Setz-Semantik
  (gleicher Wert -> no-op ohne Versionssprung), Audit-Eintrag je Aenderung,
  Ereignis `transaction.changed`; `PendingReconciliationError` fuer vorgemerkte
  Buchungen; `transaction_detail(...)` fuer F-DET-01 inkl. `latest_import_run`.
- `backend/finue/api/transactions.py` (neu): `GET /api/transactions/{id}`,
  `PUT /api/transactions/{id}/checked`, `PUT /api/transactions/{id}/reconciled`;
  Body `{value, version?}`; 404/409 (mit `current`)/422 im einheitlichen Format.
- `backend/finue/api/routes.py`: Router `transactions` eingebunden.
- `tests/unit/test_api_contract.py`: drei Vertragseintraege
  (GET lesen; zwei PUT `setzen`).
- `tests/unit/test_transaction_edit.py` (neu): 21 Tests (Setz-Semantik,
  Idempotenz, 422 Vormerkung, 409 Konflikt, Audit, Ereignis, Herkunft,
  Detailfelder, Import-Lauf).
- `docs/api.md`: Abschnitt "Einzelbuchung: Detail und Kennzeichen" plus
  Ergaenzung im Abschnitt Ereignisse.
- `projects/finanzuebersicht/decisions.md`: T-108-Eintrag.
- `projects/finanzuebersicht/improve.md`: Kandidat "direkte Verknuepfung
  Buchung -> ImportRun".

## Evidenz

- `uv run pytest tests/unit/test_transaction_edit.py tests/unit/test_api_contract.py -q`
  = **30 passed** (Etappen-Verifikationsplan).
- `uv run ruff check` und `uv run ruff format --check` der geaenderten Dateien =
  gruen; `uv run mypy backend/finue/services/transaction_edit.py
  backend/finue/api/transactions.py backend/finue/api/routes.py` = Success.
- `uv run mypy backend` = Success (93 Dateien).
- Planweit `uv run ruff check backend tests scripts` ist rot wegen fremder,
  noch unfertiger Parallelarbeit (`scripts/perf_data.py`, T-103); meine Dateien
  sind sauber.

## Offen / Grenzen

- Risiko medium: unabhaengige Verifierpruefung vor `task-done`.
- `import_run` im Detail ist der juengste Lauf des Kontos mit passender Art
  (keine Modellspalte); als Improve-Kandidat und in `decisions.md` dokumentiert.
- Keine echten Zugangsdaten, keine externen Schreibaktionen, keine Migration.
