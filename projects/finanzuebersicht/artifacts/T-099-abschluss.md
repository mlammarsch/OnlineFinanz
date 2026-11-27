# T-099 Abschlussgate: Gesamtverifikation (M4, Sync-REST + DB-Session-Freigabe)

Datum: 2026-11-27
Risiko: medium. Abschlusstask gemaess M4-Entlastung (plan.md, Abschnitt
"M4-Entlastung", Zeilen 129-150): Umsetzung in T-111 und T-112, hier **nur**
Gesamtverifikation, kein Neubau.

## 1. Voraussetzung (Etappen done)

- T-111 (DoD `services/sync_service.py` Session-Freigabe an der TAN-Pause, N-DB-04)
  `status: done`, Evidenz `artifacts/T-111-verify-1795724203.json` (5 Session-Release-Tests,
  106 sync-gefilterte Tests).
- T-112 (DoD Sync-REST, Runner, TAN-Endpunkte, Waechter, Protokoll, Events)
  `status: done`, Evidenz `artifacts/T-112-verify-1795792983.json` (36 Tests
  `test_sync_api.py`+`test_sync_session_release.py`+`test_api_contract.py`,
  Integration 17 passed/1 deselected).

Freigabe: `python hx.py task-set finanzuebersicht T-099 pending` — Status danach gezielt
`running` (der generische `task-claim` waehlt die niedrigste faellige Prio und traf in
dieser Welle wiederholt die falsche ID, Improve-Queue).

## 2. Unveraenderter Verifikationsplan (T-099)

| Befehl | Ergebnis |
|---|---|
| `uv run pytest tests/unit/test_sync_api.py -q` | 18 passed, 1 warning |
| `uv run pytest -m integration tests/integration -q` | 17 passed, 1 deselected |
| `uv run ruff check backend tests scripts` | All checks passed |
| `uv run ruff format --check backend tests scripts` | 201 files already formatted |
| `uv run mypy backend` | Success: no issues found in 96 source files |

Alle fuenf Schritte Exit 0. Der Verifikationsplan wurde nicht veraendert.

## 3. Querschnitt und Integration gegen T-111/T-112

- **Etappen-Naht:** T-111 liefert die Session-Freigabe im `SyncService`
  (`_open_session`/`_close_session`, `context.session = None` an der Pause:
  `backend/finue/services/sync_service.py` Zeilen 877-888); T-112 baut den Runner
  (`backend/finue/services/sync_runner.py`) und die REST-Schicht darauf auf. Kein
  Etappendiff neu implementiert.
- **REST-Vertrag vollstaendig:** `backend/finue/api/sync.py` traegt alle sieben DoD-Routen:
  `POST /sync`, `POST /accounts/{account_id}/sync`, `GET /sync/jobs/{job_id}`,
  `POST /sync/jobs/{job_id}/tan`, `POST .../approve-poll`, `POST .../cancel`,
  `GET /sync/runs`. `tests/unit/test_api_contract.py` fuehrt passende Vertragseintraege
  (Batch F-SYN-01/11, Einzel F-SYN-02, Challenge F-SYN-04/07, TAN), Vertragstest gruen.
- **Ereignisse:** Runner emittiert `sync.status`, `sync.tan_required`, `sync.tan_answered`
  (nur IDs/Status, nie Dialogdaten/Challenge/TAN/PIN, N-SEC-02).
- **Zusatzbeleg Querschnitt (nicht Teil des Plans, nur Nachweis):**
  `uv run pytest tests/unit/test_sync_session_release.py tests/unit/test_api_contract.py -q`
  = 18 passed. Der Plan selbst deckt T-111 ueber die Integration-Suite und T-112 ueber
  `test_sync_api.py` ab.

## 4. Abgrenzung

- Keine Code-Neubauten, keine Aenderung an Etappen-Implementierung, keine Migration,
  keine echten Zugangsdaten, keine externen Schreibaktionen, keine anderen Tasks gestartet.
- Abschlussentscheidung faellt ueber `hx.py task-done` (Verifikationsplan), nicht ueber
  die Einschaetzung des Agenten.
- Medium-Risiko: unabhaengige `verifier`-Pruefung (Integration und Querschnitt) folgt.
