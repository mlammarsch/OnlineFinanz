# T-101 Bau-Evidenz: Server-Sent Events `/api/events` (F-CON-04)

Branch: `m4/develop`. Risiko: medium (unabhaengige `verifier`-Pruefung folgt).
Kein Push, keine echten Zugangsdaten, keine externe Schreibaktion.

## Umfang

- `backend/finue/services/events.py` (Erweiterung): `SseBroadcaster` (ein
  Bus-Empfaenger, fortlaufende Event-ID, Ringpuffer fuer `Last-Event-ID`,
  begrenzte Warteschlange je Client, `resync` bei Ueberlauf und bei nicht
  nachlieferbarem Stand), `SseConnection` (beschraenkte, threadsichere
  Warteschlange), `SseFrame`/`encode_sse`/`keepalive_frame` sowie der
  asynchrone Auslieferungs-Generator `stream_sse` (Heartbeat, Disconnect-Poll).
- `backend/finue/api/events.py`: `GET /api/events` (`text/event-stream`),
  Auth-Naht `require_user`, kein `get_session`; liest `Last-Event-ID`.
- `backend/finue/api/container.py`, `backend/finue/main.py`,
  `backend/finue/api/deps.py`: SSE-Verteiler als App-Singleton im
  Service-Container (`app.state.services.sse_broadcaster`), Start/Stop im
  Lifespan.
- `backend/finue/config.py`: `api_sse_heartbeat_seconds` (15.0),
  `api_sse_queue_size` (256).
- `backend/finue/api/routes.py`: Router-Eintrag (`events.router`).
- `tests/unit/test_api_contract.py`: Vertragseintrag `/api/events` (lesen, auth).
- `tests/unit/test_sse.py`: 14 Tests.
- Doku: `docs/api.md` (Endpunkt-Abschnitt, Config-Grenzen),
  `projects/finanzuebersicht/decisions.md` (T-101), `improve.md`.

Nicht angefasst: `services/sync_runner.py` (Ereignis-Uebersetzung aus T-112),
`services/sync_registry.py`, keine Migration, keine Import-/Edit-/Export-Logik.

## Ereignisfluss

`EventBus` (T-086) -> `SseBroadcaster` (ein Empfaenger, ID + Ringpuffer) ->
`SseConnection` je Client -> `stream_sse` -> HTTP. Die Sync-Ereignisse
`sync.status`/`sync.tan_required`/`sync.tan_answered` stammen unveraendert aus
dem Runner (T-112, `SyncRegistry.add_listener` -> `EventBus`).

## Sicherheit (N-SEC-02/03)

Nutzlast = `{"type", "ids", "occurred_at"}` aus `Event.as_dict()`; PIN, TAN,
Challenge-Text und Dialogdaten stehen nicht in den IDs. Der Leckagetest
(`test_payload_traegt_keine_geheimnisse`) fuehrt einen echten TAN-Statuswechsel
ueber Registry -> Runner -> Bus -> Verteiler und prueft, dass `123456`, `pin`,
`challenge` und `dialog` im Strom fehlen.

## Evidenz

Fokus (Verifikationsplan):

- `uv run pytest tests/unit/test_sse.py -q` -> **14 passed**.
- `uv run ruff check backend/finue/services/events.py backend/finue/api/events.py
  backend/finue/api/container.py backend/finue/api/deps.py backend/finue/config.py
  backend/finue/main.py backend/finue/api/routes.py tests/unit/test_sse.py` ->
  All checks passed.
- `uv run ruff format --check` (dieselben Dateien) -> alle formatiert.
- `uv run mypy backend/finue/services/events.py backend/finue/api/events.py
  backend/finue/api/container.py backend/finue/api/deps.py
  backend/finue/config.py` -> Success (5 Dateien).

Zusatz:

- `uv run pytest tests/unit/test_api_contract.py tests/unit/test_events.py -q`
  -> 23 passed (Vertragseintrag `/api/events` integriert, EventBus unveraendert).
- Mutationstest: `_plan_replay_locked` liefert bei zu alter ID `False` statt
  `resync` -> `test_last_event_id_nicht_nachlieferbar_folgt_resync` rot; Tests
  sind wirksam.

Planweite Befehle (rot nur durch fremde, unfertige Parallelarbeit; eigener
Slice sauber):

- `uv run ruff check backend tests scripts` -> 4 x E501 in
  `tests/unit/test_duplicate_resolution.py` (T-096, in Arbeit).
- `uv run ruff format --check backend tests scripts` -> 3 Dateien:
  `backend/finue/api/sync.py` (T-091: UTF-8-BOM), `duplicate_resolution.py` und
  `test_duplicate_resolution.py` (T-096, in Arbeit).
- `uv run mypy backend` -> 2 Fehler in `duplicate_resolution.py:686,689`
  (`_iso`-Argumenttyp, T-096, in Arbeit).

Diese Fremddateien sind untracked/uncommittet und nicht Teil des T-101-Commits;
der Plan wird nach deren Commit erneut gefahren.

## Offene Punkte

- Medium-Risiko: unabhaengige `verifier`-Pruefung (Review-Checkliste AGENTS.md)
  steht vor `task-done`.
- Kein `task-done` bei rotem planweitem Baum; nach T-091/T-096-Commit erneut
  fahren.
- Frontend-Anbindung (`EventSource`, `resync` -> Neuladen) gehoert zu M5/M6
  (Improve-Eintrag).
