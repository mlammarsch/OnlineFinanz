# T-096 Fix-Evidenz: Vormerkungen und bereits zusammengefuehrte Buchungen ablehnen (F-DUP-04, F-DUP-05)

- Task: T-096, Ziel G-005, Milestone M4, Risiko medium
- Branch: `m4/develop`
- Fix-Commit: `43a554c` -- "M4 T-096: Vormerkungen und bereits zusammengefuehrte Buchungen bei Merge/Ignore ablehnen (F-DUP-04, F-DUP-05)"
- Umfang: `backend/finue/services/duplicate_resolution.py` (+28/-2),
  `tests/unit/test_duplicate_resolution.py` (+76)

## Verifierbefund (zwei Defekte)

1. `merge_duplicates` und `ignore_duplicates` prueften den Status der Buchungen
   nicht. Vormerkungen (`status='vorgemerkt'`) nehmen laut F-DUP-04 nicht am
   Verdachtsabgleich teil; die Liste filtert sie zwar, die Aufloesungsfunktionen
   akzeptierten sie aber. Ein Paar mit einer Vormerkung liess sich so
   zusammenfuehren oder ignorieren.
2. `ignore_duplicates` prueft `_is_suspected_pair` **vor** dem Zustand. Nach einem
   Merge ist die andere Buchung `merged=True`; `_is_suspected_pair` liefert
   weiterhin `True` (Betrag/Tag/Text unveraendert), und `_upsert_decision` setzte
   die vorhandene Entscheidung von `zusammengefuehrt` auf `verworfen`. Damit war
   die Merge-Entscheidung ueberschrieben und `undo_merge` (verlangt
   `decision == "zusammengefuehrt"`) unmoeglich.

## Fix

- Neue private Hilfe `_require_booked(*transactions)` in
  `services/duplicate_resolution.py`: wirft `DuplicateResolutionError`, sobald
  eine Buchung `status != BOOKED_STATUS` traegt.
- `merge_duplicates` ruft `_require_booked(keep, other)` nach der Konto-Pruefung
  auf.
- `ignore_duplicates` ruft `_require_booked(keep, other)` auf **und** prueft
  `keep.merged or other.merged` **vor** `_is_suspected_pair`; beides lehnt mit
  `DuplicateResolutionError` ab.
- Kein Router-Eingriff: der bestehende Uebersetzer `DuplicateResolutionError ->
  DomainError` (`api/duplicates.py`) liefert unveraendert HTTP 422 `domain_error`.

## Regressionstests (aus dem Befund, `tests/unit/test_duplicate_resolution.py`)

- `test_merge_vorgemerkte_buchung_ist_422` (F-DUP-04): Merge mit Vormerkung -> 422,
  keine Entscheidung, nichts `merged`.
- `test_ignore_vorgemerkte_buchung_ist_422` (F-DUP-04): Ignore mit Vormerkung ->
  422, keine Entscheidung.
- `test_ignore_nach_merge_ist_422_und_erhaelt_merge` (F-DUP-05): Ignore nach Merge
  -> 422, die Entscheidung bleibt `zusammengefuehrt`, `undo` liefert 200 und
  stellt die andere Buchung wieder her.

## Evidenz (alle Exit 0, Commit-Stand `43a554c`)

- `uv run pytest tests/unit/test_duplicate_resolution.py -q` -> **35 passed**
  (vorher 32, +3 Regressionstests).
- `uv run pytest tests/unit/test_api_contract.py -q` -> **13 passed**
  (T-096-Vertragseintraege unveraendert).
- `uv run pytest tests/unit -q` -> **1605 passed/1 skipped**.
- `uv run ruff check backend tests scripts` -> **All checks passed!**
- `uv run ruff format --check backend tests scripts` -> **210 Dateien**.
- `uv run mypy backend` -> **Success: no issues found in 101 source files**.
- Beide geaenderten Dateien ohne BOM (Erstbytes `34 34 34`).

## Mutationstest (Wirksamkeit der neuen Tests)

Im Arbeitsstand `_require_booked(...)` neutralisiert und in `ignore_duplicates`
die `merged`-Pruefung entfernt: die drei neuen Tests werden rot
(`test_merge_vorgemerkte_buchung_ist_422`,
`test_ignore_vorgemerkte_buchung_ist_422`,
`test_ignore_nach_merge_ist_422_und_erhaelt_merge`; 3 failed). Datei danach aus
Sicherung exakt wiederhergestellt; `35 passed` erneut.

## Grenzen

Nur `duplicate_resolution.py` und der T-096-Test geaendert. Keine
T-091-Vertragseintraege, keine Suche/Export/Sync/Kontenloeschung, keine
Migration. Keine echten Zugangsdaten, keine externen Schreibaktionen.

## Offen

T-096 bleibt **offen**: unabhaengige Wiederholungspruefung durch `verifier`
(Review-Checkliste AGENTS.md); kein `task-done`, kein Push. `handoff.md`/`queues.md`
wurden bewusst nicht angefasst (parallele Welle-Arbeit im Arbeitsbaum).
