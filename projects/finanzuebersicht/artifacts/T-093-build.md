# T-093 Evidenz (builder): Buchungsliste mit Vormerkungen, Tagessalden, Gruppenansicht und Keyset-Paginierung

Datum: 2026-11-27, Branch `m4/develop`, Worker builder (DeepSeek V4.1 Flash).
Risiko medium: die unabhaengige Pruefung durch `verifier` steht aus; `task-done` wurde bewusst nicht aufgerufen.

## Umfang (F-BUC-01..06)

- `backend/finue/services/transaction_list.py` (neu): `build_transaction_list` liefert
  `ListHead` (Name, Icon-Verweis, Saldo, F-BUC-01), `PendingSection` (Liste, Summe,
  Saldo inkl. Vormerkungen, F-BUC-02), Tagesgruppen mit Tagessumme und Tagessaldo
  (F-BUC-03), Zeilenfelder (F-BUC-04) und Keyset-Paginierung (F-BUC-06).
  Wiederverwendung: `services/search.py` (`TransactionQuery`, `build_query`,
  `build_select`, `build_sum_select`, `build_count_select`), Modelle aus T-088.
- `backend/finue/api/transaction_list.py` (neu): `GET /api/accounts/{id}/transactions`
  und `GET /api/groups/{id}/transactions`, Keyset-Cursor, 404/422 im Fehlerformat
  (F-CON-02). Eigene Datei, damit `api/transactions.py` (T-094/T-108) kollisionsfrei bleibt.
- `backend/finue/api/pagination.py`: `resolve_limit` aus `resolve_page_params`
  herausgezogen (eine Quelle fuer die `limit`-Pruefung, SPEC F-MCP-11).
- `backend/finue/api/routes.py`: Fachrouter registriert.
- `tests/unit/test_api_contract.py`: zwei Listenrouten-Eintraege.
- `docs/api.md`: Abschnitt "Buchungsliste (F-BUC-01..06)" plus Endpunktzeilen.
- `projects/finanzuebersicht/decisions.md`: Tagessaldo-/Keyset-Regel.

## Kernregeln (Kurzfassung)

- Ankerregel wie T-095: FinTS-Konto -> `balance_cents`; sonst `opening_balance_cents`
  plus gebuchte Betraege. Tagessaldo(Tag) = aktueller Saldo minus Summe aller juengeren
  gebuchten Betraege (SQL-Aggregat je Seite, kein Python-Vollscan).
- Tagessalden beruecksichtigen alle gebuchten, nicht zusammengefuehrten Buchungen
  (auch filterausgeblendete); `merged` fehlt immer (F-DUP-04).
- Gruppe: Zeilen aller aktiven Mitglieder mit Kontokennzeichnung; Tagessaldo nur in
  der Buchwaehrung, Fremdwaehrung nicht addiert (T-088-Regel).
- Pending nur auf der ersten Seite (ohne Cursor); Summe ueber alle gefilterten
  Vormerkungen, Liste auf 200 Zeilen begrenzt.

## Verifikationsplan

| Befehl | Ergebnis |
|---|---|
| `uv run pytest tests/unit/test_transaction_list.py -q` | 26 passed (1 warning, Starlette-Hinweis) |
| `uv run ruff check <T-093-Dateien>` | All checks passed |
| `uv run ruff format --check <T-093-Dateien>` | 6 files already formatted |
| `uv run mypy backend/finue/services/transaction_list.py backend/finue/api/transaction_list.py backend/finue/api/pagination.py` | Success (3 files) |
| `uv run pytest tests/unit/test_api_contract.py -q` | 13 passed |

### Globale Verifikationsbefehle (Plan) im gemeinsamen Arbeitsbaum

Die planweiten Befehle sind zum Commit-Zeitpunkt durch **parallele, noch nicht
committete** Welle-Arbeit rot, nicht durch T-093:

- `uv run ruff check backend tests scripts`: 3 E501 in `backend/finue/api/import_templates.py`
  und `tests/unit/test_export_json_md.py` (T-097/T-100-Umfeld).
- `uv run ruff format --check backend tests scripts`: 4 Dateien (import_templates,
  test_export_json_md, ...; T-097-Umfeld).
- `uv run mypy backend`: 3 Fehler in `backend/finue/services/export.py` (T-097-Umfeld).

Die T-093-Dateien selbst sind in allen drei Pruefungen gruen. Nach dem Commit der
parallelen Wellen ist der Gesamtplan erneut zu fahren (durch `verifier`), dann
entscheidet `hx.py task-done`.
