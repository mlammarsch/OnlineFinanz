# T-112 Bau-Evidenz: Sync-REST, Runner, TAN-Endpunkte, Waechter, Protokoll, Ereignisse

Datum: 2026-11-27
Branch: m4/develop
Commit: 50b8e57 (code, docs, decisions)
Risiko: medium (Verifier ausstehend, kein `task-done`)

## Umfang (DoD)

- `POST /api/sync` (Batch, optional `bank_access_id`) und `POST /api/accounts/{id}/sync`
  kehren sofort mit der/den Job-ID(s) zurueck (202); der Lauf laeuft in einem
  Daemon-Thread des einen Worker-Prozesses (D3) in `services/sync_runner.py`.
- `GET /api/sync/jobs/{job_id}` liefert Status, Ergebnisse je Konto sowie
  Challenge-Text und -Art (`challenge`, `tan_kind`) bei TAN; nie Dialogdaten.
- `POST .../tan {tan}`, `POST .../approve-poll`, `POST .../cancel`; Doppelstart
  liefert den laufenden Job (F-SYN-11); zweite TAN-Antwort -> 409 mit `current`.
- PIN bei `store_pin=false` im Request-Body, nur im Arbeitsspeicher, nie
  geloggt oder zurueckgegeben (N-SEC-01..03).
- `SyncService` als Singleton in `app.state`; 5-Minuten-Timeout ueber
  periodischen Waechter-Thread im App-Prozess (`SyncRunner.start_watchdog`).
- `GET /api/sync/runs` (F-SYN-07, paginiert, filterbar je Konto).
- Ereignisse `sync.status`, `sync.tan_required`, `sync.tan_answered` ueber den
  EventBus (F-CON-04).
- Eintraege in `tests/unit/test_api_contract.py`.
- Tests nur mit Fake-Client in `tests/unit/test_sync_api.py`: Start, TAN,
  App-Freigabe, Abbruch, Timeout, Doppelstart, Leckage (PIN/TAN nicht in
  Antworten, Logs, Ereignissen).

## Geaenderte/neue Dateien

- neu `backend/finue/api/sync.py` (Router, DTOs, Validierung, Fehlerabbildung)
- neu `backend/finue/services/sync_runner.py` (Hintergrund-Thread, Zustand,
  Waechter, Ereignisuebersetzung)
- `backend/finue/services/sync_service.py` (Naht `claim_sync` +
  `start_claimed_sync`; `start_sync` unveraendert auf beiden aufgebaut)
- `backend/finue/api/container.py` (`sync_service`, `sync_runner` im Container)
- `backend/finue/main.py` (`create_app(..., sync_client_factory)`, Bau/Start/Stop
  von Service und Runner)
- `backend/finue/api/routes.py` (Sync-Router-Zeile)
- `backend/finue/api/imports.py` (`_run_dto` -> oeffentlich `run_view` fuer
  Wiederverwendung im Sync-Protokoll)
- neu `tests/unit/test_sync_api.py` (16 Tests)
- `tests/unit/test_api_contract.py` (7 Sync-Eintraege)
- `docs/api.md` (Abschnitt Sync + Ereignisse), `decisions.md` (zwei T-112-Zeilen)

## Evidenz (Verifikationsplan)

- `uv run pytest tests/unit/test_sync_api.py tests/unit/test_sync_session_release.py tests/unit/test_api_contract.py -q`
  = **34 passed**.
- `uv run pytest -m integration tests/integration -q` = **17 passed, 1 deselected**.
- Zusatz: `uv run pytest tests/unit -q --ignore=tests/unit/test_transaction_list.py`
  = **1466 passed, 1 skipped** (die ignorierte Datei ist fremde, noch nicht
  committete Parallelarbeit).
- `uv run ruff check backend tests` = All checks passed.
- `uv run mypy backend` = Success (96 Dateien).
- Hinweis: `uv run ruff format --check backend tests scripts` meldet einzig die
  fremde, noch nicht committete Datei `tests/perf/test_performance.py` (T-103);
  alle T-112-Dateien sind formatiert.

## Kernpunkte fuer den Verifier

- Waehrend `AWAITING_TAN` ist der Verbindungspool leer (`Engine.pool.checkedout() == 0`);
  kein DB-Session-/ORM-Objekt im Runner-Zustand (nur IDs, Werte, Challenge-Text).
- Der Waechter nutzt die injizierbare Registry-Uhr; der Timeout ist deterministisch
  (`check_timeouts`).
- Die Leckagepruefung faengt PIN und TAN aus Antworten, `caplog` und
  `Event.as_dict()` ab.
- `POST /api/sync` ohne `bank_access_id` startet je Bankzugang mit aktiven
  FinTS-Konten einen Job; `account_ids`/`pin` sind dann nur bei genau einem
  Zugang zulaessig (Entscheidung in `decisions.md`).
