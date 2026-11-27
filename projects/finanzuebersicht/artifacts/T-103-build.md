# T-103 Evidenz (builder): Performance-Block mit 100.000 Buchungen (N-PERF-01, F-BUC-06)

Datum: 2026-11-27, Branch `m4/develop`, Worker builder (DeepSeek V4.1 Flash).
Risiko medium: die unabhaengige Pruefung durch `verifier` folgt; `task-done` nach
gruenem Verifikationsplan, Verifier-Prüfung vor Abschluss.

## Umfang

- **Datengenerator** `scripts/perf_data.py` (Bibliothek + CLI): erzeugt
  reproduzierbar **100.000** synthetische Buchungen (fester Seed `20241126`,
  **5 Konten**, **3 Jahre**, realistische Textlaengen aus 24 Vorlagen, Notizen
  **~5 %**, Vormerkungen ~5 %, keine echten Daten, D8). Schema ueber die echte
  Alembic-Kette (`upgrade_to_head`), Einfuegen per SQLAlchemy-Core-`insert` in
  5.000er-Bloecken (N-DB-01, kein rohes Daten-SQL), inklusive gefuellter
  `search_text` (F-SUC-06). Vorkommens-Index je (Konto, Fingerprint) als Multiset
  (F-DUP-02). Laufzeit der Erzeugung: rund 10 s.
- **Perf-Tests** `tests/perf/` (pytest-Marker `perf`): Session-scoped Bestand
  (einmal erzeugen), Messung = Median aus 5 Laeufen nach 2 Aufwaermlaeufen,
  Maximum wird protokolliert. Budget je Messung **< 500 ms**.
- **Migration 0005** `backend/finue/migrations/versions/0005_performance_indizes.py`
  mit zwei abdeckenden Indizes (empirisch begruendet, siehe unten), Modell
  `transaction.py` traegt dieselben Indizes.
- **`scripts/validate.py`**: Block `perf` implementiert (`check_perf`), in
  `STAGE_M4` und in `IMPLEMENTED["perf"]` eingetragen; die Messwerte stehen im
  Meldungstext.
- **`pyproject.toml`**: Marker `perf` registriert; `addopts = "-m 'not perf'"`,
  damit der schwere Block nur ueber `validate.py perf` laeuft.

## DoD-Abgleich

| DoD | Erfuellung |
|---|---|
| Generator: fester Seed, 5 Konten, 3 Jahre, realistische Texte, ~5 % Notizen, keine echten Daten | `scripts/perf_data.py`, `PerfInfo`/`generate_perf_database` |
| Core-Bulk-Insert in temporaere SQLite-Datei inkl. `search_text` | `insert(Transaction.__table__)`, `build_search_text` je Zeile |
| Suche 365 Tage, ein/zwei Begriffe, Betrag, alle Konten < 500 ms (Median 5, Max protokolliert) | Tests `suche_1_begriff`/`suche_2_begriffe`/`suche_betrag` |
| Buchungsliste erste Seite und Seite 50 sowie Gruppenansicht < 500 ms | Tests `liste_seite_1`/`liste_seite_50`/`gruppe_seite_1` |
| einmal zusaetzlich ueber HTTP | Test `http_suche_1_begriff` + `http_liste_seite_1` (`TestClient`, feste Uhr) |
| Index-Migration 0005 mit EXPLAIN-Evidenz bei Bedarf | Migration 0005 + `EXPLAIN QUERY PLAN` unten |
| `validate.py` Block `perf`, Messwerte in der Ausgabe, in `STAGE_M4` | `check_perf`, `STAGE_M4`, `IMPLEMENTED["perf"]` |

## Gemessene Werte (`python scripts/validate.py perf`, 2026-11-27)

| Messung | Median | Maximum | Budget |
|---|---|---|---|
| suche_1_begriff | 188,0 ms | 191,3 ms | < 500 ms |
| suche_2_begriffe | 188,6 ms | 204,9 ms | < 500 ms |
| suche_betrag | 25,4 ms | 27,4 ms | < 500 ms |
| liste_seite_1 | 5,6 ms | 7,4 ms | < 500 ms |
| liste_seite_50 | 4,7 ms | 5,2 ms | < 500 ms |
| gruppe_seite_1 | 196,5 ms | 200,4 ms | < 500 ms |
| http_suche_1_begriff | 221,9 ms | 281,6 ms | < 500 ms |
| http_liste_seite_1 | 14,4 ms | 103,2 ms | < 500 ms |

Ohne Migration 0005 (nur die Basisindizes aus 0003) lagen die Suche bei rund
480-500 ms (Maximum ueber Budget) und die Gruppenansicht bei rund 800-1100 ms
(ueber Budget) - daher ist die Indexmigration **notwendig**, nicht optional.

## EXPLAIN-QUERY-PLAN-Evidenz (frischer Bestand, Schema mit 0005)

```
# Suche: Trefferzahl-Abfrage
  SEARCH transaction USING COVERING INDEX ix_transaction_account_booking_search (account_id=? AND booking_date>? AND booking_date<?)
# Suche: Treffersummen-Abfrage
  SEARCH transaction USING COVERING INDEX ix_transaction_account_booking_search (account_id=? AND booking_date>? AND booking_date<?)
# Buchungsliste/Gruppe: gebuchte Summe je Konto
  SEARCH transaction USING COVERING INDEX ix_transaction_account_status_booking_cover (account_id=? AND status=? AND merged=?)
```

`LIKE '%begriff%'` (fuehrendes Wildcard) kann den Index nicht suchend nutzen; der
Index wird jedoch als `COVERING INDEX` gelesen, sodass kein Zeilen-Nachschlag
noetig ist. Das ist die in F-SUC-06 genannte "normalisierte Suchspalte plus
Indizes" (Entscheidung und Alternativen: `decisions.md` T-103).

## Verifikationsplan

| Befehl | Ergebnis |
|---|---|
| `python scripts/validate.py perf` | Exit 0, 8 Messwerte im Meldungstext, alle "OK" |
| `uv run ruff check backend tests scripts` | All checks passed |
| `uv run ruff format --check backend tests scripts` | 200 files already formatted |
| `uv run mypy backend` | Success: no issues found in 96 source files |
| `uv run pytest tests/unit -q` (Regression des Modell-/Migrationspfads) | 1492 passed, 1 skipped |
| `uv run pytest -m perf tests/perf -q -s` | 7 passed |

## Dateien

- neu: `scripts/perf_data.py`, `tests/perf/conftest.py`, `tests/perf/test_performance.py`,
  `backend/finue/migrations/versions/0005_performance_indizes.py`,
  `tests/unit/test_migration_0005.py`
- geaendert: `backend/finue/models/transaction.py` (zwei Indizes),
  `tests/unit/test_alembic_config.py` (Kante 0004 -> 0005), `scripts/validate.py`
  (Block `perf`), `pyproject.toml` (Marker `perf`, `addopts`)

## Offene Punkte / Hinweise

- Parallel im selben Arbeitsbaum: T-112 (Sync-REST) hat fremde, noch nicht
  committete Dateien (`api/sync.py`, `services/sync_runner.py`, `api/routes.py`,
  `api/container.py`, `main.py`, `api/imports.py`, `services/sync_service.py`,
  `tests/unit/test_api_contract.py`, `tests/unit/test_sync_api.py`). Diese wurden
  **nicht** mitcommittet; der Commit umfasst nur die T-103-Dateien.
- Die Messwerte sind maschinen- und lastabhaengig; die Margen zum 500-ms-Budget
  sind jedoch gross (Suche/Gruppe rund 40 % des Budgets).
- Keine echten Zugangsdaten, keine Bankzugriffe, keine externen Schreibaktionen.
