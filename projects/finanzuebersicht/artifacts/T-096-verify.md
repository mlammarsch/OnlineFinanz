# T-096 Verifier-Evidenz (unabhaengig, Detached-Worktree b2d9cbe)

Urteil: PASS (F-DUP-03/04/05, F-CON-01/02). Pruefstand: sauberer Detached-Worktree bei `b2d9cbe`
(enthaelt 9964bb2, 43a554c und T-091-Vertragsfix b9e5fff, per merge-base bestaetigt).

## Ergebnisse (alle Exit 0)
- `pytest tests/unit/test_duplicate_resolution.py tests/unit/test_api_contract.py` -> 48 passed
- `pytest tests/unit` -> 1579 passed, 1 skipped
- `pytest -m integration tests/integration` -> 17 passed
- `ruff check backend tests scripts` -> All checks passed
- `ruff format --check backend tests scripts` -> 209 files already formatted
- `mypy backend` -> Success, 101 Dateien

## Gezielte Pruefpunkte
- merge/ignore mit Vormerkung -> 422 `domain_error` (`_require_booked`), keine Entscheidung, nichts merged.
- ignore nach merge (beide Richtungen) -> 422; Entscheidung bleibt `zusammengefuehrt`; Undo danach 200 und stellt beide Buchungen her; zweiter Undo -> 404.
- Eigene Zusatzproben (Wegwerf-Test, nicht committet): ignore mit veralteter Version -> 409 mit `current`, nichts geschrieben; merge mit veralteter Version der anderen Buchung -> 409, nichts veraendert; Audit-Folge `merged`, `merge_undone`; zwei `duplicate.changed`-Ereignisse; ignore -> merge -> undo stellt `verworfen` wieder her.
- Bestehende Tests decken: Liste (Konto/Gruppe/Paginierung/404/422), FinTS-vor-Datei und Alter-Vorschlag, Bankfelder, Notiz mit Herkunftsvermerk, Kennzeichen, externe Referenz (422 ohne Wahl, mit Wahl), Unsichtbarkeit, Re-Import nach Merge/Ignore = 0 neu, Undo-Rundreise, 409 bei geaendertem Bleiber.
- Auth/Vertrag: vier Routen im Vertragstest (require_user), Router mit `dependencies=[Depends(require_user)]`.
- Scope: Merge nur als Markierung, keine automatische Aufloesung, keine Kategorien/Ueberweisungen, keine Secrets, keine Netzzugriffe.

## Befunde
Keine Defekte, keine Korrektur noetig.
