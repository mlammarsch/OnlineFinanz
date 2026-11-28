# T-104 Bau-Evidenz: Concurrency-Block (F-CON-01..05, N-DB-04)

Risiko medium. Branch m4/develop. Stand 2026-11-28.

## Umfang

Neuer Nebenlaeufigkeits-Block `tests/concurrency/` (pytest-Marker `concurrency`)
gegen **datei-basierte** SQLite im WAL-Modus mit mehreren Threads und je eigener
`Session` (bzw. dem geteilten Test-Client). Plus der vorgesehene Check in
`scripts/validate.py` (`check_concurrency`, `CONCURRENCY_CHECK`, Eintrag in
`STAGE_M4` und unter `IMPLEMENTED["concurrency"]`) und der Marker `concurrency`
in `pyproject.toml` (Standardlauf deselektiert ihn: `-m 'not perf and not concurrency'`).

Keine fachliche Produktionsaenderung: die Concurrency-Hooks (Versionsspalte
`VersionedMixin`, `set_flag`-Setz-Semantik, atomares `append_note`, SQLite-WAL +
`busy_timeout`, Sync-Registry-Sperre, SSE-Verteiler) sind bereits in HEAD. Der
Block belegt sie.

## Dateien

- `tests/concurrency/support.py` – Helfer: Konten/Buchungen anlegen, Session-Factory,
  Fingerprint fuer Sync-Lieferungen, `run_parallel` (Barriere statt `sleep`),
  `drain` fuer SSE, `any_locked_error`.
- `tests/concurrency/test_concurrency_flags.py` – F-CON-01 (Kennzeichen-Race, 20
  Anhaenge), F-CON-02 (Notiz-Ersetzung gleiche Version, HTTP-409, Konto-PATCH).
- `tests/concurrency/test_concurrency_api.py` – F-CON-03 (Batch-Isolation),
  F-CON-04 (zwei SSE-Clients).
- `tests/concurrency/test_concurrency_sync.py` – F-SYN-11 (Sync-Sperre je Zugang),
  F-CON-05 (Sync-Persistenz parallel zu Nutzeraenderungen), N-DB-04.
- `scripts/validate.py` – `check_concurrency`, `STAGE_M4`, `IMPLEMENTED`.
- `pyproject.toml` – Marker `concurrency`, addopts.

## DoD-Abdeckung

| DoD-Punkt | Test |
|---|---|
| Gleichzeitiges Setzen desselben Kennzeichens -> identisches Ergebnis, hoechstens ein Versionssprung (F-CON-01) | `test_paralleles_setzen_desselben_kennzeichens_f_con_01` (12 Threads, `version == 2`, 1 Audit-Eintrag) |
| 20 parallele Notiz-Anhaenge -> jeder Text genau einmal (F-CON-01) | `test_20_parallele_notizanhaenge_f_con_01` (jeder Text `count == 1`, `version == 21`) |
| Zwei Notiz-Ersetzungen gleiche Version -> eine 409 mit aktuellem Stand (F-CON-02) | `test_zwei_notiz_ersetzungen_gleicher_version_f_con_02` + `test_notiz_ersetzen_veraltete_version_http_409_f_con_02` |
| Konto-PATCH-Konflikt | `test_konto_patch_konflikt_parallel` (Service-Race + HTTP-409) |
| Batch mit einem Konflikt -> uebrige geaendert (F-CON-03) | `test_batch_mit_einem_konflikt_f_con_03` |
| Zweiter SSE-Client erhaelt die Ereignisse (F-CON-04) | `test_zweiter_sse_client_erhaelt_ereignisse_f_con_04` (2 Clients, 8 parallele Schreiber, gleiche IDs) |
| Sync-Persistenz parallel zu Nutzeraenderungen -> Nutzerfelder unveraendert (F-CON-05) | `test_sync_persistenz_parallel_zu_nutzeraenderungen_f_con_05` |
| Keine `database is locked`-Fehler (N-DB-04) | `test_gleichzeitige_schreiber_ohne_locked_fehler_n_db_04` + `any_locked_error` in jedem Test |
| Determinismus (Barrieren statt `sleep`), dreimal gruen | `CONCURRENCY_RUNS = 3` in `check_concurrency`; `run_parallel` nutzt `threading.Barrier` |

## Evidenz

- `python scripts/validate.py concurrency` -> `[OK] Nebenlaeufigkeit Datei-SQLite,
  3x gruen (F-CON-01..05, N-DB-04) - 3 Laeufe gruen (10 passed, 1 warning in 4.71s)`,
  Ergebnis bestanden.
- `uv run pytest tests/concurrency -m concurrency -q` dreimal -> `10 passed` je Lauf
  (4.65 s bis 5.24 s).
- `uv run ruff check tests/concurrency scripts/validate.py` -> All checks passed.
- `uv run ruff format --check tests/concurrency scripts/validate.py` -> 5 files already formatted.
- `uv run mypy backend` -> Success: no issues found in 101 source files.

Planweiter `ruff check backend tests scripts` / `ruff format --check ...` ist rot
**nur** durch fremde, noch nicht committete Parallelarbeit
(`tests/e2e_api/test_scenario_a.py:95`, E501, T-113/T-106-A) – nicht durch T-104.

## Hinweise / Umgebung

- Gemeinsamer Arbeitsbaum (bekanntes Problem, queues.md-T-091/T-113-Notizen):
  `scripts/validate.py` und `pyproject.toml` enthielten bereits fremde
  uncommittete Hunks (T-105 roundtrip, T-113 api_e2e). Der T-104-Commit
  `d666e7f` staged diese Shared-Files **hunkweise** ueber
  `git hash-object -w` + `git update-index --cacheinfo` (HEAD-Blob plus nur
  die T-104-Zeilen); fremde Arbeitsbaum-Hunks blieben unangetastet.
- Keine echten Zugangsdaten, keine externen Schreibaktionen, nur erfundene
  Testdaten (D8), kein Bankzugriff.

## Offen

- Risiko medium: unabhaengige `verifier`-Pruefung (Review-Checkliste AGENTS.md)
  folgt; `task-done` erst danach (kein `task-done` durch den Builder).
- Kein Push.
