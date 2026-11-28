# T-113 Bau-Evidenz: API-E2E Rahmen und Szenario Teil 1

Datum: 2026-11-28. Branch `m4/develop`. Etappe von T-106 (Risiko low, nur Testcode).

## Umfang

- `tests/e2e_api/app_factory.py`: Uvicorn-Fabrik
  `python -m uvicorn --factory tests.e2e_api.app_factory:create_app` mit genau einem
  Worker (D3). Ersetzt ausschliesslich den FinTS-Client durch
  `FakeFinTS3PinTanClient` (D8); Settings aus `FINUE_E2E_DATA_DIR`, Skriptwahl ueber
  `FINUE_E2E_SCRIPT` (Standard `happy_path`, fuer T-115 z. B. `tan_app`).
- `tests/e2e_api/conftest.py`: `e2e_server` (echter Uvicorn-Prozess, temporaeres
  Datenverzeichnis, freier Port, Warten auf `/api/health/ready`, sicheres Beenden
  auch bei Fehlschlag, Logdatei), `api_client` (httpx), `sse_listener`
  (Hintergrund-Thread auf `GET /api/events` mit `wait_for`). Marker `api_e2e`
  in `pyproject.toml`; Standardlauf deselektiert ihn wie `perf`/`concurrency`.
- `tests/e2e_api/test_scenario_a.py`: eigenstaendiges Szenario auf frischem
  Server (Leerzustand, Gruppe, Offline-Konto, CSV-Import mit Erkennen/Vorschau/
  Lauf, Vormerkung ueber eigenes JSON, Buchungsliste mit Vormerkungen und
  Tagessalden, Gruppenansicht, Suche Text/Betrag/Zeitraum/Filter, Kennzeichen/
  Notiz ersetzen+anhaengen/externe Referenz, Batch mit provoziertem 409,
  Abstimmungsuebersicht gegen Handrechnung, Konto archivieren/wiederherstellen,
  Icon hochladen/abrufen). Jeder aendernde Schritt prueft die erwarteten
  SSE-Ereignisse; reine Leseschritte pruefen das Ausbleiben von Ereignissen.

Kein Eingriff in `scripts/validate.py` (Check `api-e2e` folgt in T-106).
Keine Produktionsdatei geaendert.

## Verifikation (Etappen-Plan)

- `uv run pytest -m api_e2e tests/e2e_api/test_scenario_a.py -q` = 1 passed
  (11,97 s; Wiederholung 12,29 s), Laufzeit < 90 s.
- Deselektion: `uv run pytest tests/e2e_api --collect-only -q` = no tests
  collected (1 deselected) ohne `-m api_e2e`.
- `uv run ruff check backend tests scripts` = All checks passed.
- `uv run ruff format --check backend tests scripts` = 217 files already formatted.
- `uv run mypy backend` = Success (101 Dateien).

## Hinweise

- Das CSV-Mapping (`TARGET_FIELDS`) kennt kein Status-Zielfeld (F-IMP-03); eine
  Vormerkung wird deshalb im Szenario ueber das dokumentierte eigene JSON-Format
  importiert (F-IMP-04). Siehe Improve-Queue.
- Die Ereignis-Zustellung des SSE-Verteilers pollt je nach Heartbeat; die
  E2E-Settings setzen `api_sse_heartbeat_seconds=0.5`, die Listener-Fixture wartet
  dennoch mit Zeitpuffer.
