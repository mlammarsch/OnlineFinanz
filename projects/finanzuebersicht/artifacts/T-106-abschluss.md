# T-106 Abschlussgate: API-E2E-Szenario gegen echten Uvicorn (M4)

Stand: 2026-11-28. Risiko medium, kein Verifier-Lauf in diesem Schritt (unabhaengige
Pruefung folgt). Keine echten Zugangsdaten, keine externen Schreibaktionen. Kein Push.

## Voraussetzungen

- T-113 (API-E2E Rahmen + Szenario Teil 1) `done` — Evidenz
  `artifacts/T-113-verify-1795867977.json`.
- T-115 (API-E2E Szenario Teil 2) `done` — Evidenz
  `artifacts/T-115-verify-1795870353.json`.

## Task-Status

- Uebergang gezielt per `hx.py task-set` (der generische `task-claim` traf in dieser
  Welle wiederholt die falsche ID): `T-106 blocked -> pending -> running`.
- Kein Neubau von Szenarien. Keine Aenderung an `tests/e2e_api/*`, `pyproject.toml`
  oder anderen Tasks.

## validate.py-Aenderung (einzige Code-Aenderung)

`scripts/validate.py`:

- neuer Check `check_api_e2e` (`API_E2E_CHECK`) — fuehrt zuerst einen Sammellauf
  (`pytest -m api_e2e tests/e2e_api --collect-only -q`) aus und verlangt mindestens
  einen gesammelten Test, danach `API_E2E_RUNS = 3` Laeufe des Markers `api_e2e`;
  jeder Lauf muss gruen sein, die Gesamtlaufzeit der drei Laeufe muss unter
  `API_E2E_BUDGET_SECONDS = 180.0` (3 Minuten) bleiben.
- `STAGE_M4` um `API_E2E_CHECK` ergaenzt (nach `ROUNDTRIP_CHECK`, `PERF_CHECK`,
  `CONCURRENCY_CHECK`).
- `IMPLEMENTED["api-e2e"] = [API_E2E_CHECK]` registriert; `"api-e2e"` neu in `BLOCKS`.
- `import time` ergaenzt (Standardbibliothek; `test_validate_stage_m4.py`
  `test_validate_nutzt_nur_standardbibliothek` bleibt gruen).

## Verifikation (alle Exit 0)

| Befehl | Ergebnis |
|---|---|
| `python scripts/validate.py api-e2e` | `2 gesammelt; 3 Laeufe gruen in 74.3s (2 passed ... in 21.69s)` — bestanden |
| `uv run pytest -m api_e2e tests/e2e_api -q` | `2 passed` (beide Szenarien) in 21.31s |
| `uv run ruff check backend tests scripts` | `All checks passed!` |
| `uv run ruff format --check backend tests scripts` | `218 files already formatted` |
| `uv run mypy backend` | `Success: no issues found in 101 source files` |
| Querschnitt `uv run pytest tests/unit/test_validate_stage_m4.py tests/unit/test_api_contract.py -q` | `18 passed` |

Beide E2E-Szenarien (`tests/e2e_api/test_scenario_a.py`, `test_scenario_b.py`) laufen
gegen einen echten Uvicorn-Prozess mit genau einem Worker (D3), bei dem nur der
FinTS-Client durch `FakeFinTS3PinTanClient` ersetzt ist. Dreimal hintereinander gruen,
Gesamtlaufzeit 74.3 s < 180 s.

## Bewertung gegen DoD

- Echter Uvicorn, ein Worker, Fake-FinTS, temp. DATA_DIR, freier Port, Warten auf
  `/api/health/ready`, sauberes Beenden: in T-113/T-115 umgesetzt, hier als Ganzes
  dreimal gruen nachgewiesen.
- `validate.py`-Check `api-e2e` (pytest `-m api_e2e`, mindestens ein gesammelter Test)
  in `STAGE_M4`: ergaenzt.
- Laufzeit unter 3 Minuten, dreimal hintereinander gruen: `74.3 s`, 3/3 gruen.

## Offen

- Medium-Risiko: unabhaengige `verifier`-Pruefung (Review-Checkliste AGENTS.md) folgt
  auf dem committeten Stand; `task-done` erst nach dem Verifikationsplan.
- Kein Push. Fremde uncommittete Welle-Arbeit (Task-/Status-/events-Dateien,
  Verifier-Artefakte, `tests/unit/test_transaction_list.py`) bleibt unangetastet.
