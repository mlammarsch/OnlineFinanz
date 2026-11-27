# T-094 Abschlussgate: Gesamtverifikation (M4, Kennzeichen/Notiz/Referenz/Batch)

Datum: 2026-11-27
Risiko: medium. Abschlusstask gemaess M4-Entlastung (plan.md, Abschnitt
"M4-Entlastung", Zeilen 129-150): Umsetzung in T-108 (Kern), T-109 (Notiz/Referenz)
und T-110 (Batch), hier **nur** Gesamtverifikation, kein Neubau.

## 1. Voraussetzung (Etappen done)

- T-108 Edit-Kern, Kennzeichen-Setz-Semantik und Buchungsdetail (F-ABS-01/02,
  F-CON-01/02, F-DET-01) `status: done`, Evidenz
  `projects/finanzuebersicht/artifacts/T-108-verify-1795790641.json`.
- T-109 Notiz ersetzen/atomar anhaengen und externe Referenz (F-ABS-05,
  F-CON-01/02) `status: done`, Evidenz
  `projects/finanzuebersicht/artifacts/T-109-verify-1795794585.json`.
- T-110 Batch-Aktion mit Ergebnis je Buchung und F-ABS-06-Regression (F-CON-03,
  F-ABS-02/06, F-CON-05) `status: done`, Evidenz
  `projects/finanzuebersicht/artifacts/T-110-verify-1795795732.json`.

Freigabe: `python hx.py task-set finanzuebersicht T-094 pending` — Status danach gezielt
`running` (der generische `task-claim` waehlt die niedrigste faellige Prio und traf in
dieser Welle wiederholt die falsche ID, Improve-Queue).

## 2. Unveraenderter Verifikationsplan (T-094)

| Befehl | Ergebnis |
|---|---|
| `uv run pytest tests/unit/test_transaction_edit.py tests/unit/test_transaction_batch.py -q` | 58 passed, 1 warning |
| `uv run ruff check backend tests scripts` | All checks passed! |
| `uv run ruff format --check backend tests scripts` | 202 files already formatted |
| `uv run mypy backend` | Success: no issues found in 96 source files |

Alle vier Schritte Exit 0. Der Verifikationsplan wurde nicht veraendert.

## 3. Querschnittsintegration

- **Einzel-Edit-Kern:** `backend/finue/services/transaction_edit.py` traegt
  `set_flag(session, id, flag, value, origin, expected_version=None, events=None,
  now=None)` mit `origin` aus `ui`/`mcp`/`import` (D10, M9 nutzt denselben Kern),
  `PendingReconciliationError` fuer vorgemerkte Buchungen (F-ABS-01), `set_note`,
  `append_note` (atomarer Einzel-UPDATE, `NOTE_APPEND_SEPARATOR`), `set_external_ref`
  (255-Zeichen-Grenze) sowie `transaction_detail`/`latest_import_run` (F-DET-01).
- **Notiz/Referenz:** ersetzende Aenderungen mit Versionspruefung (`version` Pflicht
  ueber die Route, 409 mit `current`); Anhaengen ohne Version und ohne
  Read-Modify-Write (F-CON-01), `search_text` wird nach Notiz und Referenz neu gebaut.
- **Batch:** `backend/finue/api/transactions.py` `POST /api/transactions/batch` ruft je
  Eintrag **genau** `set_flag`/`set_note`/`append_note` auf (keine zweite
  Schreibpfad-Logik); Ergebnis je Buchung als `BatchItemResult`
  (`geaendert`/`unveraendert`/`konflikt`/`fehler`), ein Konflikt/Fehler rollt nur den
  Eintrag zurueck (F-CON-03), `versions` optional (F-CON-02 wo angegeben), Grenze ueber
  `check_batch_size`/`api_max_batch_size`.
- **Versions-/Setz-Semantik:** gleicher Wert -> No-op ohne Versionssprung/Zeitstempel/
  Audit/Ereignis (F-CON-01); veraltete Version -> Konflikt (F-CON-02).
- **Audit/Event/search_text:** `AuditLog`-Eintrag je Aenderung (`source`=Herkunft,
  F-MCP-09), Ereignis `transaction.changed`, `refresh_search_text` (F-SUC-01,
  F-ABS-05) - im Kern belegt, im Fokustest mitgeprueft.
- **Routen und Vertrag:** `backend/finue/api/routes.py` bindet `transactions.router` ein
  (Zeile 73); sieben DoD-Routen vorhanden
  (`GET /transactions/{id}`, `PUT .../checked`, `PUT .../reconciled`, `PUT .../note`,
  `POST .../note/append`, `PUT .../external-ref`, `POST /transactions/batch`);
  `tests/unit/test_api_contract.py` fuehrt fuer **alle sieben** passende Vertragseintraege
  (lesen/setzen/version mit Anforderungs-IDs).
- **Belege ausserhalb des Plans (nur Nachweis, nicht Planbestandteil):**
  `uv run pytest tests/unit -q` = 1538 passed/1 skipped;
  `uv run pytest tests/unit/test_api_contract.py -q` = 13 passed;
  `uv run pytest -m integration tests/integration -q` = 17 passed/1 deselected
  (F-ABS-06/F-CON-05: Sync und Re-Import aendern die Felder nicht, im Fokustest
  `test_transaction_batch.py` als Regression mitbelegt).

### Anforderungsabdeckung in den Fokustests

`tests/unit/test_transaction_edit.py` (33 Testfunktionen) und
`tests/unit/test_transaction_batch.py` (19 Testfunktionen) referenzieren die
Anforderungs-IDs: F-ABS-01 (7x), F-ABS-02 (5x), F-ABS-05 (9x), F-ABS-06 (4x),
F-CON-01 (15x), F-CON-02 (19x), F-CON-03 (12x), F-CON-05 (4x); zusaetzlich
`transaction.changed` (7x), `search_text` (12x), `AuditLog` (6x).

## 4. Abgrenzung

- Keine Code-Neubauten, keine Aenderung an Etappen-Implementierung, keine Migration,
  keine echten Zugangsdaten, keine externen Schreibaktionen, keine anderen Tasks gestartet.
- Abschlussentscheidung faellt ueber `hx.py task-done` (Verifikationsplan), nicht ueber
  die Einschaetzung des Agenten.
- Medium-Risiko: unabhaengige `verifier`-Pruefung (Integration und Querschnitt,
  Checkliste AGENTS.md) folgt nach dem Commit auf den festgeschriebenen Stand.
