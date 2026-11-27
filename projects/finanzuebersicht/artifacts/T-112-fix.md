# T-112 Fehlerpfad-Fix: Abbruch vor erster TAN-Pause und Leerzeichen-TAN

Datum: 2026-11-27
Branch: m4/develop
Bezug: T-112 (offen, Risiko medium), Verifier-Befund der unabhaengigen Pruefung
Umfang: nur die zwei T-112-Defekte; keine anderen T-112-/T-103-Slices geaendert.

## Befund und Fix

1. Abbruch/Timeout vor der ersten TAN-Pause
   - Befund: `SyncService.start_claimed_sync` fing `SyncCancelledError`/
     `TanTimeoutError` nicht ab; die Exception lief in `SyncRunner._run_initial`
     und der Job endete als generischer `fehler` ("Der Sync-Lauf konnte nicht
     gestartet werden.") statt `abgebrochen` (F-SYN-10).
   - Fix: `start_claimed_sync` beendet den Lauf bei `SyncCancelledError` als
     `abgebrochen`, bei `TanTimeoutError` als `zeitueberschreitung` (analog zu
     `_advance`) und prueft nach dem `try` zusaetzlich
     `context.job.abort_requested` (Abbruch waehrend des Netzaufrufs vor der
     Pause) und beendet dann regulaer.

2. TAN aus reinen Leerzeichen liefert 500 statt 422
   - Befund: `TanSubmitRequest.tan` hatte nur `min_length=1`; `"   "` passierte
     die Validierung und `SyncService.submit_tan` warf `ValueError` -> 500.
   - Fix: `tan: str = Field(min_length=1, max_length=64, pattern=r"\S")`.

## Regressionstests (tests/unit/test_sync_api.py)

- `test_abbruch_vor_erster_tan_pause_f_syn_10`: blockierender Fake-Client
  (`GatedClient` in `get_balance`), `POST .../cancel` liefert `laeuft`, nach
  Freigabe des Netzaufrufs endet der Job als `abgebrochen`.
- `test_leerzeichen_tan_liefert_422_f_syn_04`: `{"tan":"   "}` -> 422
  `validation_error`; die offene Abfrage bleibt bestehen, eine gueltige TAN
  setzt den Lauf danach erfolgreich fort.
- Mutationstest: beide Tests sind rot, wenn der jeweilige Fix zurueckgenommen
  wird (Cancel -> `fehler`, Leerzeichen -> 500).

## Angepasster M3-Test (Folgewirkung des Fixes)

`start_sync()` baut auf `start_claimed_sync()` auf; der Abbruch waehrend des
Netzaufrufs wird deshalb nun auch dort als Ergebnis (`status="abgebrochen"`)
zurueckgegeben statt als `SyncCancelledError` geworfen. Der M3-Test
`tests/integration/test_sync_orders.py::test_abort_during_orders_saves_no_orders_for_running_account_f_syn_10`
prueft genau diesen Fall und wurde auf die konsistente Semantik angepasst
(Rueckgabe `abgebrochen` statt Exception). Die fachlichen Zusicherungen
(unveraenderter Auftragsbestand, Protokollzeile `abgebrochen`) bleiben
unveraendert und gruen. F-SYN-10 verlangt einen Lauf-Status, keine Exception.

## Evidenz

- `uv run pytest tests/unit/test_sync_api.py tests/unit/test_sync_session_release.py tests/unit/test_sync_tan.py tests/unit/test_sync_secrets.py -q` = 43 passed
- `uv run pytest tests/unit -q` = 1494 passed, 1 skipped
- `uv run pytest -m integration tests/integration -q` = 17 passed, 1 deselected
- `uv run ruff check backend tests scripts` = All checks passed
- `uv run ruff format --check backend tests scripts` = 200 files already formatted
- `uv run mypy backend` = Success (96 source files)

## Status

T-112 bleibt offen; kein `task-done`. Kein Push. Keine echten Zugangsdaten,
keine externe Schreibaktion.
