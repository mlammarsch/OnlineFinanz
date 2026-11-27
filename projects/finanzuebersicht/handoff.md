# Handoff

## Next actions

- **T-112 umgesetzt (M4-Entlastung T-099-B, Sync-REST, Runner, TAN-Endpunkte, Waechter, Protokoll,
  Ereignisse; F-SYN-01/02/04/07/10/11, N-SEC-01..03, N-DB-04, Risiko medium).** Konflikt-Check: Ja (nur
  Sync-REST/Protokoll; keine Aenderung am T-099-Abschlussgate ausserhalb der Etappe, keine T-111-Sessionlogik
  umgebaut, keine Migration, keine Import-/Edit-Logik). Neu: `backend/finue/services/sync_runner.py`
  (`SyncRunner` startet den Lauf in einem Daemon-Thread des einen Worker-Prozesses (D3), haelt Job-Momentaufnahmen
  im Arbeitsspeicher, uebersetzt Registry-Statuswechsel in `sync.status`/`sync.tan_required`/`sync.tan_answered`
  (nur IDs + Status, nie Challenge-Text/TAN/PIN), traegt den periodischen 5-Minuten-Waechter und die testbare
  Einzelpruefung `check_timeouts`), `backend/finue/api/sync.py` (`POST /api/sync` Batch mit optionalem
  `bank_access_id` → 202 mit Job-IDs; `POST /api/accounts/{id}/sync`; `GET /api/sync/jobs/{job_id}` mit Status,
  Kontoresultats sowie `challenge`/`tan_kind`; `POST .../tan` → zweite Antwort 409 mit `current`;
  `POST .../approve-poll`; `POST .../cancel`; `GET /api/sync/runs` paginiert/filterbar), `services/sync_service.py`
  (Naht `claim_sync` + `start_claimed_sync`; `start_sync` unveraendert auf beiden aufgebaut), `api/container.py`
  (`sync_service`/`sync_runner` im Container/Singleton in `app.state`), `main.py` (`create_app(...,
  sync_client_factory)` fuer Tests, Start/Stop des Waechters), `api/routes.py` (Sync-Router),
  `api/imports.py` (`_run_dto` → oeffentlich `run_view` fuer Wiederverwendung). PIN bei `store_pin=false` nur im
  Request-Body und im RAM, fehlt sie → synchroner 422; keine PIN/TAN in Antwort, Log oder Ereignis. `GET
  /api/sync/runs` nutzt die vorhandene `kind=sync`-Protokollzeile. Evidenz: Verifikationsplan
  `uv run pytest tests/unit/test_sync_api.py tests/unit/test_sync_session_release.py tests/unit/test_api_contract.py -q`
  = 34 passed; `uv run pytest -m integration tests/integration -q` = 17 passed/1 deselected; Zusatz
  `uv run pytest tests/unit -q` = 1466 passed/1 skipped; `ruff check backend tests` = All checks passed;
  `mypy backend` = Success (96 Dateien); Artefakt `artifacts/T-112-build.md`; Commit `50b8e57`. **Offen:** Risiko
  medium - unabhaengige `verifier`-Pruefung steht vor `task-done`. Planweiter `ruff format --check` ist durch
  fremde, noch unfertige Parallelarbeit rot (`tests/perf/test_performance.py`, T-103); fremde uncommittete
  Welle-Arbeit (`models/transaction.py`, `migrations/versions/0005_*`, `scripts/perf_data.py`, `tests/perf/`,
  `tests/unit/test_transaction_list.py`, Task-/Status-/events-Dateien) bleibt unangetastet. Kein Push.

- **T-103 Perf-Block umgesetzt (M4 Welle 6, 100.000 Buchungen, N-PERF-01/F-BUC-06, Risiko medium).**
  Konflikt-Check: Ja (nur Perf-Daten/Perf-Tests, Index-Migration 0005, `validate.py`-Block `perf`;
  keine fachliche Aenderung an Suche/Buchungsliste/Export). Neu: `scripts/perf_data.py` (Generator +
  CLI, reproduzierbar: Seed 20241126, 5 Konten, 3 Jahre, ~5 % Notizen, Vormerkungen ~5 %, keine echten
  Daten; Schema ueber Alembic, Core-Bulk-Insert inkl. `search_text`), `tests/perf/` (Marker `perf`,
  Session-scoped Bestand, Median aus 5 Laeufen nach Aufwaermen, Maximum protokolliert; Suche mit einem
  und zwei Begriffen und Betragssuche, Liste Seite 1/50, Gruppenansicht, einmal ueber HTTP), Migration
  `0005_performance_indizes.py` mit zwei abdeckenden Indizes (`ix_transaction_account_booking_search`,
  `ix_transaction_account_status_booking_cover`) plus dieselben Indizes im Modell `transaction.py`,
  `tests/unit/test_migration_0005.py`, `scripts/validate.py` (`check_perf`, `STAGE_M4`,
  `IMPLEMENTED["perf"]`), `pyproject.toml` (Marker `perf`, `addopts = "-m 'not perf'"`). Ergebnis:
  Suche ~188-222 ms, Betrag ~25 ms, Liste ~5 ms, Gruppenansicht ~196 ms, HTTP ~222/14 ms (alle Median
  < 500 ms). Ohne Migration 0005 war die Gruppenansicht ~800-1100 ms und die Suche am Budget - der
  `EXPLAIN QUERY PLAN` zeigt die zwei Indizes als `COVERING INDEX` (Evidenz im Artefakt). Evidenz:
  `scripts/validate.py perf` Exit 0 (8 Messwerte im Text), `ruff check`/`ruff format --check` gruen,
  `mypy backend` Success (96 Dateien), `pytest tests/unit` 1492 passed/1 skipped, `pytest -m perf` 7 passed;
  Artefakt `artifacts/T-103-build.md`. **Offen:** Risiko medium - unabhaengige `verifier`-Pruefung folgt.
  Kein Push. Fremde uncommittete Welle-Arbeit (T-112: `api/sync.py`, `services/sync_runner.py`, u. a.)
  bleibt unangetastet und ist nicht mitcommittet.

- **T-108 umgesetzt (M4-Entlastung T-094-A, Edit-Kern, Kennzeichen-Setz-Semantik und Buchungsdetail,
  F-ABS-01/02, F-CON-01/02, F-DET-01, Risiko medium).** Konflikt-Check: Ja (nur Einzelbuchung; keine
  Notiz-/externe-Referenz-/Batch-Logik aus T-109/T-110, keine Migration, keine Import-/Sync-Aenderung). Neu:
  `backend/finue/services/transaction_edit.py` (`set_flag` mit `origin` ui/mcp/import, `expected_version`,
  `events`, `now`; Setz-Semantik: gleicher Wert -> no-op ohne Versionssprung/Zeitstempel/Audit/Ereignis; echte
  Aenderung setzt Wert+Zeitpunkt+Herkunft, schreibt einen Audit-Eintrag (`source`=Herkunft, F-MCP-09) und meldet
  `transaction.changed`; `PendingReconciliationError` fuer vorgemerkte Buchungen; `transaction_detail` fuer
  F-DET-01 inkl. `latest_import_run`), `backend/finue/api/transactions.py` (`GET /api/transactions/{id}`,
  `PUT .../checked`, `PUT .../reconciled`; Body `{value, version?}`, 404/409 mit `current`/422 im einheitlichen
  Format), `api/routes.py` (Router-Zeile), `tests/unit/test_transaction_edit.py` (21 Tests), drei Eintraege in
  `tests/unit/test_api_contract.py`, `docs/api.md` (Abschnitt Einzelbuchung + Ereignisse), `decisions.md` (T-108)
  und `improve.md`. Offen: **Import-Lauf** im Detail ist der juengste Lauf des Kontos mit passender Art (FinTS->
  `sync`, CSV->`csv`, JSON->`json`), weil das Modell keine direkte Verknuepfung Buchung -> ImportRun hat
  (Improve-Kandidat, decisions.md). Evidenz: Etappen-Plan `uv run pytest
  tests/unit/test_transaction_edit.py tests/unit/test_api_contract.py -q` = 30 passed; ruff check/format der
  geaenderten Dateien gruen; `mypy backend` = Success (93 Dateien); Artefakt `artifacts/T-108-build.md`. Commit
  `ef9aed1`. **Offen:** Risiko medium - unabhaengige `verifier`-Pruefung steht vor `task-done`. Planweites
  `ruff check` ist durch fremde, noch unfertige Parallelarbeit rot (`scripts/perf_data.py`, T-103); fremde
  uncommittete Welle-Arbeit (`container.py`/`sync_service.py`/`sync_runner.py` T-112, `perf_data.py`, Task-/
  Status-/events-Dateien) bleibt unangetastet. Kein Push.

- **T-098 Roundtrip-Defekt behoben (M4, F-EXP-03/F-EXP-06, Risiko medium) -- T-098 bleibt offen.**
  Befund der Verifierpruefung: `_guard_cell` und `_unguard_cell` in `backend/finue/services/export.py` waren fuer
  Originalwerte mit fuehrendem Apostroph nicht umkehrbar (`'=x` wurde beim Re-Import zu `=x`). Fix: gemeinsame
  Pruefung `_is_guard_candidate` -- `value.lstrip("'")` muss nichtleer sein und mit einem Formula-Trigger
  (`= + - @` Tab CR) beginnen; `_guard_cell` entschaerft genau diese Werte, `_unguard_cell` nimmt genau einen
  fuehrenden Apostroph auf einem Kandidaten zurueck (`'abc` bleibt erhalten). Regressionstests in
  `tests/unit/test_export_csv.py`: `test_export_guard_ist_umkehrbar_fuer_apostroph_originale` (`'=x`, `''-x`,
  `'abc`) und `test_csv_roundtrip_erhaelt_originale_mit_fuehrendem_apostroph` (Export -> Vorlage -> Re-Import =
  0 neue). Evidenz: `tests/unit/test_export_csv.py`+Profile = 27 passed; Export-/Profil-/Importtests = 129 passed;
  `tests/unit` = 1456 passed/1 skipped; `ruff check`/`ruff format --check` (189 Dateien)/`mypy backend` (91 Dateien)
  gruen; `scripts/validate.py roundtrip` = OK; Artefakt `artifacts/T-098-fix.json`. **Offen:** unabhaengige
  Wiederholungspruefung durch `verifier` folgt; kein `task-done`, kein Push. Keine anderen
  T-098-/T-100-/T-102-Slices geaendert; fremde uncommittete Welle-Arbeit bleibt unangetastet.

- **T-098 umgesetzt (M4 Welle 6, CSV-Export mit Optionen, Exportprofilen und passender Importvorlage,
  F-EXP-03/F-EXP-06, Risiko medium).** Konflikt-Check: Ja (nur CSV-Export, Exportprofil-CRUD und der eigene
  Seed; JSON-/Markdown-Logik unveraendert ausser gemeinsamer Verdrahtung). Neu/geaendert:
  `backend/finue/services/export.py` (`ExportFormat.CSV`, `CsvExportOptions`, `validate_csv_options`/
  `validate_csv_columns`, `merge_csv_options`, `build_csv`, `encode_content`, `strip_export_guard`,
  `ExportResult.encoding`; Spaltenlabels ASCII, Standardprofil "Finanzblick-angelehnt" **unbelegt**),
  `backend/finue/services/export_profiles.py` (Profil-CRUD mit `ExportProfileVersionConflict`, genau ein
  Standardprofil), `backend/finue/api/export_profiles.py` (`GET/POST /api/export-profiles`,
  `GET/PATCH/DELETE /api/export-profiles/{id}` mit 409/`current`), `backend/finue/api/export.py`
  (`format="csv"`, `profile_id`, `csv`-Overrides, Kodierung), `backend/finue/services/import_templates.py`
  (vierter Seed "Eigener CSV-Export", `pre_filter='own_csv'`), `backend/finue/api/imports.py`
  (Import-Vorverarbeitungsregister um `strip_export_guard`), `api/routes.py` (Router). Formel-Injection:
  Freitextzellen mit fuehrendem `= + - @`/Tab/CR werden mit `'` entschaerft, Betrag/Datum nie; die
  Importvorlage macht die Entschaerfung rueckgaengig, der Roundtrip bleibt verlustfrei. Evidenz:
  `uv run pytest tests/unit/test_export_csv.py tests/unit/test_export_profiles_api.py -q` = 25 passed;
  `uv run pytest tests/unit -q` = 1454 passed/1 skipped; `uv run ruff check backend tests scripts` =
  All checks passed; `uv run ruff format --check backend tests scripts` = 189 files already formatted;
  `uv run mypy backend` = Success (91 Dateien); Artefakt `artifacts/T-098-build.json`. **Offen:** Risiko
  medium: unabhaengige `verifier`-Pruefung (Review-Checkliste AGENTS.md) steht vor `task-done`. Kein Push;
  fremde uncommittete Welle-Arbeit (status.md/queues.md/events.jsonl, Task-Dateien T-092/093/100/108/112,
  `tests/unit/test_transaction_list.py`, Verifier-Artefakte) bleibt unangetastet.

- **T-102 umgesetzt (M4 Welle 3, Sitzungsuebersicht und Abmelden einzelner Sitzungen, F-CON-06, Risiko low).**
  Konflikt-Check: Ja (nur Sitzungen; keine Sync-/SSE-/Import-/Export-Aenderung ausser der Router-Verdrahtung;
  keine N-SEC-04-Anmeldung, Anlage bleibt M10). Neu: `backend/finue/services/sessions.py`
  (`describe_device` verkuerzt den User-Agent zu `Browser auf System`, unbekannt -> `Unbekanntes Geraet`;
  `list_active_sessions` liefert nur `expires_at > jetzt`, nach `last_seen_at` absteigend;
  `revoke_session` loescht die Zeile und meldet `bool`), `backend/finue/api/sessions.py`
  (`GET /api/sessions` paginiert mit `{items, next_cursor}` und `limit`; `DELETE /api/sessions/{id}` idempotent 204).
  Naht fuer M10: `CurrentUser.session_id` ergaenzt (in M4 `None`, daher `current` ueberall `false`).
  `token_hash` verlaesst das Backend nie (DTOs ohne Hash, keine Logausgabe, N-SEC-02). Vertragstabelle,
  `docs/api.md` (Abschnitt Sitzungen inkl. Beispiel), `decisions.md` (Loeschen statt `revoked_at`, idempotentes
  DELETE, verkuerzter Geraetetext) und `improve.md` (UtcDateTime-Casts, Index auf `expires_at`) ergaenzt.
  Evidenz: `uv run pytest tests/unit/test_sessions_api.py -q` = 24 passed; `tests/unit -q` = 1429 passed/1 skipped;
  `uv run ruff check backend tests scripts` = All checks passed; `uv run ruff format --check backend tests scripts`
  = 185 files already formatted; `uv run mypy backend` = Success (89 Dateien). Risiko low: nach gruenem
  Verifikationsplan ohne separaten Verifier abgeschlossen (`python hx.py task-done finanzuebersicht T-102`,
  Evidenz `artifacts/T-102-verify-1795787785.json`). **Offen:** Fremde uncommittete
  Welle-Arbeit (`backend/finue/services/export.py` aus T-097) bleibt unangetastet; die vorbestehenden
  Task-/Status-Aenderungen (T-092/T-093/T-100/T-108/T-112, status.md/queues.md/events.jsonl) wurden nicht
  mitcommittet. Kein Push.

- **T-093 umgesetzt (M4 Welle 5, Buchungsliste mit Vormerkungen, Tagessalden, Gruppenansicht und
  Keyset-Paginierung, F-BUC-01..06, Risiko medium).** Konflikt-Check: Ja (nur Buchungsliste; keine
  Kennzeichen-/Notiz-Editierung, keine Export-/Sync-REST-Aenderung; `api/transactions.py` bleibt T-094/T-108).
  Neu: `backend/finue/services/transaction_list.py` (Kopf mit Name/Icon-Verweis/Saldo F-BUC-01;
  Vormerkungsbereich mit Liste/Summe/Saldo inkl. Vormerkungen F-BUC-02; Tagesgruppen mit Tagessumme und
  rueckwaerts vom aktuellen Saldo berechnetem Tagessaldo F-BUC-03; Zeilenfelder F-BUC-04; Gruppenansicht
  mit Kontokennzeichnung je Zeile F-BUC-05; Keyset-Paginierung F-BUC-06) und `backend/finue/api/transaction_list.py`
  (`GET /api/accounts/{id}/transactions`, `GET /api/groups/{id}/transactions`; Filter/Zeitraum/Sortierung aus
  `services/search.py`/`TransactionQuery` T-092). Kernregeln: Ankerregel wie T-095 (FinTS `balance_cents`, sonst
  `opening_balance_cents` + gebuchte Betraege); Tagessaldo(Tag) = aktueller Saldo minus Summe aller juengeren
  gebuchten Betraege, auf Folgeseiten per SQL-Aggregat statt Python-Vollscan; Tagessalden filterunabhaengig
  (alle gebuchten, nicht zusammengefuehrten Buchungen zaehlen); Gruppe summiert nur die Buchwaehrung (T-088-Regel) und
  zeigt Fremdwaehrung nur als Zeile. `api/pagination.py`: `resolve_limit` aus `resolve_page_params` extrahiert (eine
  Quelle der `limit`-Pruefung). Vertragstabelle, `docs/api.md`, `decisions.md` (Tagessaldo-/Keyset-Regel),
  `improve.md` ergaenzt. Evidenz: `uv run pytest tests/unit/test_transaction_list.py -q` = 26 passed;
  `uv run pytest tests/unit/test_api_contract.py -q` = 13 passed; ruff check/format und mypy der T-093-Dateien gruen
  (Artefakt `artifacts/T-093-build.md`). **Offen:** Die planweiten Befehle (ruff/mypy ueber `backend tests scripts`)
  sind zum Commit-Zeitpunkt durch parallele, noch nicht committete Welle-Arbeit rot
  (T-097 `services/export.py` mypy, `tests/unit/test_export_json_md.py`, `api/import_templates.py`); die
  T-093-Dateien sind sauber. Shared-Files (`api/routes.py`, `tests/unit/test_api_contract.py`) wurden hunk-weise
  ueber den Git-Index gestaged, damit die fremden Export-Zeilen nicht mitgehen. `task-done` bewusst nicht
  aufgerufen: die unabhaengige `verifier`-Pruefung (medium) folgt; danach bei sauberem Baum den Verifikationsplan
  erneut fahren. Kein Push.

- **T-090 umgesetzt (M4 Welle 4b, Banken-Stammdaten und Icon-Upload, F-KTO-10/11, Risiko medium).** Konflikt-Check: Ja
  (F-KTO-10/11, F-CON-02, N-SEC-05; kein Web-Abruf F-KTO-08, keine Aenderung an Loeschung/Suche/Sync). Neu:
  `backend/finue/services/icons.py` (Signatur-Erkennung PNG/JPEG/WebP; 512-KB-Grenze **vor** dem Dekodieren;
  Pillow-`verify`; Pixelgrenze gegen Dekompressionsbomben; Skalierung 128x128 mit erhaltenem Seitenverhaeltnis als
  PNG; ETag = sha256 der gespeicherten Bytes; Speicherung in `account.icon`/`bank.icon` samt `icon_mime` mit
  Versionspruefung), `backend/finue/services/banks.py` (Banken-CRUD; BLZ acht Ziffern; FinTS-Endpunkt nur HTTPS;
  Website http(s); Loeschschutz ohne Bankzugaenge mit 409 Klartext; **keine Vorbelegung**), `backend/finue/api/icons.py`
  (PUT/DELETE/GET `/api/accounts/{id}/icon` und `/api/banks/{id}/icon`; GET liefert image/png mit ETag und 304),
  `backend/finue/api/banks.py` (GET/POST `/api/banks`, GET/PATCH/DELETE `/api/banks/{id}`). Pillow exakt gepinnt
  (`pillow==13.0.0` in pyproject und uv.lock). `api/routes.py` um banks/icons ergaenzt, `test_api_contract.py` um vier
  Eintraege, `docs/api.md` und `decisions.md` dokumentiert. Evidenz:
  `uv run pytest tests/unit/test_icons.py tests/unit/test_banks_api.py -q` = 35 passed; `ruff check`, `ruff format
  --check` und `mypy` der neuen Dateien gruen. **Offen:** die globalen Verifikationsbefehle waren zum Zeitpunkt des
  Commits durch parallele, noch nicht committete Wellen-4b-Arbeit rot (v. a. `services/search.py` mypy und
  `tests/unit/test_search.py` E501 aus T-092, `services/accounts.py` mypy aus T-089); `task-done` deshalb erst nach
  sauberem Baum. Kein Push.

- **T-089 umgesetzt (M4, Konto loeschen mit vollstaendiger Bereinigung, Risiko medium).** Konflikt-Check: Ja
  (F-KTO-07, F-CON-02, F-CON-04, F-SYN-11; keine Such-, Export-, Sync- oder Abstimmungslogik geaendert). Neu:
  `backend/finue/services/accounts.py` - `delete_account(session, account_id, *, expected_version, sync_running)`
  entfernt in EINER Transaktion Dublettenentscheidungen (beide FK-Richtungen), Buchungen samt Notizen und
  Vormerkungen, Termin-/Dauerauftraege, Import-Laeufe mit ImportRunErrors und kontogebundene Audit-Eintraege;
  kontogebundene Importvorlagen werden nur entkoppelt (`account_id` NULL); der Bankzugang bleibt in jedem Fall
  erhalten (kein stilles Loeschen von Zugangsdaten). `AccountSyncRunning` sperrt mit Klartext, wenn der Bankzugang
  gerade synchronisiert (`SyncRegistry.is_active` ueber die injizierte `sync_running`-Funktion, D10-tauglich). Das
  Konto selbst wird per ORM geloescht, damit die Versionsspalte als WHERE-Bedingung wirkt und `StaleDataError` die
  gesamte Bereinigung zurueckrollt (F-CON-02); `DeletionSummary` meldet die Zeilenzahlen. `backend/finue/api/accounts.py`:
  `DELETE /api/accounts/{id}?version=n` (`ge=1`) liefert 204 ohne Koerper (404/409/422 im Fehlerformat) und das
  Ereignis `account.deleted`. Doku: `docs/api.md` (Endpunkt + Abschnitt "Konto loeschen (F-KTO-07)"), `decisions.md`
  (Bereinigungsumfang/Audit/Vorlagen; Query-Parameter + Sync-Sperre). Tests: neues `tests/unit/test_account_delete.py`
  (11 Tests: volle Bereinigung mit Zeilenzaehlung, andere Konten unberuehrt, Vorlagen entkoppelt, archiviertes Konto,
  Bankzugang erhalten, laufender Sync 409, Versionskonflikt, 404, 422, Ereignis, DeletionSummary). Evidenz:
  `uv run pytest tests/unit/test_account_delete.py -q` = 11 passed; `ruff check`, `ruff format --check` und `mypy`
  der geaenderten Dateien gruen. **Offen/Blocker:** Die globalen Verifikationsbefehle sind wegen parallel unfertiger
  Dateien (T-090 banks/icons, T-092 search) rot und werden nach deren Commit erneut gefahren; die Vertragstabelle
  wurde nur um die DELETE-Zeile ergaenzt. Kein Push.

- **T-088 umgesetzt (M4 Welle 3, Konten und Gruppen - Service und API, Risiko medium).** Konflikt-Check: Ja
  (`routes.py` nur die Include-Zeilen; getrennt von Such-/Edit-/Session-Etappen T-087/T-108..T-110/T-111). Neu:
  `backend/finue/services/accounts.py` (IBAN normalisieren/Modulo-97 validieren, `mask_iban`, `list_accounts`,
  `latest_sync_times` aus ImportRun `kind=sync` mit Status `erfolgreich`/`teilweise`, `compute_total` mit
  Fremdwaehrung getrennt, Konto-/Gruppen-CRUD mit Versionspruefung ueber `StaleDataError` -> 409),
  `backend/finue/api/accounts.py` (GET/POST `/api/accounts`, GET/PATCH `/api/accounts/{id}`,
  POST `/api/accounts/{id}/archive|restore`; Liste mit `iban_masked`, Detail mit voller `iban`; Ereignis
  `account.changed`) und `backend/finue/api/groups.py` (GET/POST `/api/groups`, PUT `/api/groups/order` als
  ID-Liste, PATCH/DELETE `/api/groups/{id}`; Mitglieder/Kontenzahl/Summe; Loeschen setzt `group_id` NULL; Ereignis
  `group.changed`). Fremdwaehrungsregel: Gesamtsaldo und Gruppensumme addieren nur die Buchwaehrung (EUR), andere
  Waehrungen stehen getrennt im Feld `foreign` (`decisions.md`). Router-Struktur: jeder Fachrouter traegt
  `prefix="/api"` und `require_user` selbst (FastAPI 0.165 setzt das Elternpraefix nicht in `APIRoute.path`);
  Vertragstabelle um 7 Eintraege erweitert, die `limit`-Pflicht gilt nur der GET-Listenroute. Evidenz:
  `uv run pytest tests/unit/test_accounts_api.py tests/unit/test_groups_api.py -q` = 39 passed;
  `uv run pytest tests/unit -q` = 1214 passed, 1 skipped; `ruff check backend tests scripts` und
  `ruff format --check backend tests scripts` gruen; `mypy backend` = Success (70 Dateien). Doku: `docs/api.md`
  (IBAN-Maskenregel, Endpunkttabelle) und `decisions.md` (Fremdwaehrungsregel, API-Struktur). **Offen:** die
  unabhaengige verifier-Pruefung (medium) steht aus; kein Push.

- **T-111 umgesetzt (M4-Entlastung, DB-Session-Freigabe an der TAN-Pause, N-DB-04).** Konflikt-Check: Ja
  (plan.md M4-Entlastung; Aenderung ausschliesslich in `backend/finue/services/sync_service.py`, keine
  REST-/Runner-/Event-/Folgeaenderungen aus T-112). Umsetzung: `_RunContext` traegt die Session nur waehrend eines
  laufenden Schritts (`session=None` an der Pause) und zusaetzlich nur die Bankleitzahl als Wert; `_GatewayHolder`
  haelt jetzt Bankzugang-ID + PIN statt eines ORM-`BankAccess` und baut jeden Dialog ueber die offene Session
  (`ensure_open`, `resume_gateway` ohne `__enter__`, `close`/`restart`). `_run` traegt nur Konten-IDs und Werte
  (kein ORM) ueber die Pause; `_step` schliesst die Session unmittelbar vor jedem `yield` und verwirft den
  Dialog, oeffnet nach der Freigabe eine frische Session und setzt den eingefrorenen Dialog mit einem neuen
  Dialoghalter fort. Neue Helfer `_open_session`/`_close_session`/`_require_session`/`_account_iban`; Abbruch und
  Timeout oeffnen fuer die Abschlussprotokolle kurz eine frische Session und schliessen sie danach. Der 5-Minuten-
  Timeout (Registry-Uhr), `N-SEC-03` (Handoff nur im RAM) und die je-Konto-genau-eine-ImportRun-Zeile bleiben
  unveraendert. Neue Tests `tests/unit/test_sync_session_release.py` (5 Tests: keine Session/Connection offen,
  parallele Schreibvorgaenge ohne Sperrwartezeit, Resume-Paritaet gegen Lauf ohne Pause, Abbruch, Timeout;
  Pool-Assert `Engine.pool.checkedout()==0` plus `context.session is None`; zusaetzlich Feldpruefung, dass der
  Laufzustand kein Session-/ORM-Objekt traegt). Evidenz: `uv run pytest tests/unit/test_sync_session_release.py -q`
  = 5 passed; `uv run pytest tests/unit -q -k sync` = 104 passed; `uv run pytest tests/integration/test_sync_flow.py
  tests/integration/test_sync_orders.py -q` = 17 passed; `ruff check`/`ruff format --check` der geaenderten Dateien
  gruen, `mypy backend/finue/services/sync_service.py` = Success. Mutationstest: ohne `self._close_session(context)`
  in `_step` schlagen 4/5 Tests fehl (Session bleibt gesetzt), die Tests sind also wirksam. **Offen:** Der
  Medium-Task wird in dieser Entlastungswelle nicht separat durch `verifier` geprueft; die unabhaengige Pruefung
  erfolgt im Abschlusstask T-099. Wichtig fuer T-112: dieselben Pool-Asserts im Runner-Test wiederholen. Kein Push.

- **T-087 umgesetzt (M4 Welle 3, Suchspalte pflegen + Backfill-Migration 0004).** Konflikt-Check: Ja (F-SUC-01,
  F-SUC-06, F-ABS-05; Dateien getrennt von T-088/T-099/T-111). Neu: `backend/finue/services/search_text.py`
  (`build_search_text(counterparty, purpose, note, counterparty_iban, external_ref)`, `normalize_query_term`,
  `normalize_search_text`, `refresh_search_text`; NFC + `casefold` (schliesst scharfes s ein), Zeilenumbrueche und
  Mehrfachleerzeichen zu einem Leerzeichen; die IBAN wird zusaetzlich ohne Leerzeichen angehaengt). Die Spalte wird
  jetzt gesetzt beim Datei-Import (`import_run._to_duplicate_data`, Neubau in `_apply_restore_values` bei Notiz-/
  Referenzaenderung), beim FinTS-Sync (`sync_persist._to_transaction_data`), bei Vormerkungen
  (`pending_sync._insert_pending_rows`, Neubau in `_apply_flags` nach Notizuebertragung), im Dublettenpfad
  (`duplicate._build_transaction` als Fallback ohne explizite Vorgabe) und in den Demo-Daten (`demo.py`).
  `backend/finue/migrations/versions/0004_search_text_backfill.py` befuellt alle Bestandszeilen mit einer im
  Migrationsmodul **eingefrorenen** Normalisierungskopie (SQLAlchemy-Core, kein rohes SQL, batchweise 500). Tests:
  `tests/unit/test_search_text.py` (Normalisierung, Paritaet zur Migrationskopie, Import/Sync/Vormerkung/
  Dublettenpfad, Notizrefresh) und `tests/unit/test_migration_0004.py` (Backfill aus 0003 inkl. Paritaet,
  Multi-Batch, Schema-Drift, downgrade); `test_alembic_config` um die Kante 0003 -> 0004 erweitert. Evidenz:
  `uv run pytest tests/unit/test_search_text.py tests/unit/test_migration_0004.py -q` = 20 passed (jeweils gruen
  waehrend der Bearbeitung). **Offen/Blocker:** Die parallelen Welle-3-Builder T-088 und T-111 schreiben im selben
  Arbeitsbaum; ihre noch uncommitteten Dateien (`api/accounts.py`, `api/groups.py`, `services/accounts.py`,
  `services/sync_service.py`, `api/routes.py`) machen die **globalen** Verifikationsbefehle rot
  (`ruff check`: E501 in `api/accounts.py`/`api/groups.py`; `ruff format --check`: 3 Dateien; `mypy`: 4 Fehler in
  `services/accounts.py`/`api/accounts.py`; zwischenzeitlich ImportError `sqlalchemy.exc.StaleDataError`, inzwischen
  behoben). Die T-087-Dateien selbst sind `ruff`/`mypy`-sauber. `hx.py task-done` wurde deshalb nicht aufgerufen;
  nach dem Commit von T-088/T-111 (oder wenn der Baum sauber ist) den Verifikationsplan laufen lassen und
  abschliessen. Kein Push.

- **T-086 abgeschlossen (M4 Welle 2, API-Grundgeruest).** Konflikt-Check: Ja (plan.md M4 T-086; keine
  SPEC-Aenderung, keine Migration, kein Fachtask). Neu: `backend/finue/api/routes.py` (zentrale Registrierung:
  Health ohne Auth, geschuetzter `/api`-Router mit `require_user`-Dependency), `errors.py` (einheitliches Format
  `{code,message,details}` plus `current` bei 409 (F-CON-02); deutsche 422-Uebersetzung; 404/405/413/401/500;
  500 ohne interne Details, N-SEC-02), `pagination.py` (`{items,next_cursor}`, limit Standard 50/hoechstens 200,
  SPEC F-MCP-11), `conventions.py` (`VersionedRequest`, Batch-Umschlag mit `geaendert|unveraendert|konflikt|fehler`
  (F-CON-03), REST-Herkunft `ui`, Batch-/Upload-Grenzen), `container.py` + `deps.py` (AppServices,
  `get_event_bus`/`get_clock`/`get_sync_registry`, `require_user`). `services/events.py`: threadsicherer EventBus
  (publish/subscribe/unsubscribe plus Queue-Abonnement fuer SSE T-101); Ereignis = Typ plus IDs, nie Geheimnisse
  (F-CON-04). `config.py`: Seitengroesse 50/200, Batch 1000, Upload 50 MiB, `auth_required`. `main.py`: Container
  (Session-Factory, EventBus, SyncRegistry-Singleton, injizierbare Uhr) in `app.state`, Fehlerhandler registriert.
  `tests/conftest.py` (Datei-SQLite WAL + TestClient), `tests/unit/test_api_foundation.py`, `test_events.py`,
  `test_api_contract.py` (Guardrail: Routen registriert, schreibende Routen mit Art/Begruendung, Listenrouten mit
  limit, jede /api-Route an require_user; synthetische Positiv-/Negativfaelle). `docs/api.md` und `decisions.md`
  (Auth-Naht). Evidenz: `uv run pytest tests/unit/test_api_foundation.py tests/unit/test_events.py
  tests/unit/test_api_contract.py -q` = 44 passed; `uv run ruff check backend tests scripts` = All checks passed;
  `uv run ruff format --check backend tests scripts` = 145 files already formatted; `uv run mypy backend` = Success
  (65 Dateien). **Parallelitaet:** T-085 lief gleichzeitig im selben Arbeitsbaum; der Commit umfasst nur
  T-086-Dateien. Naechster Schritt: unabhaengige verifier-Pruefung (medium), danach T-088/T-092/T-099/T-101/T-102/T-111.

- **T-085 umgesetzt (M4 Welle 2, Migration 0003 Schema-Ergaenzungen).** Konflikt-Check: Ja (F-ABS-01, F-KTO-10,
  F-EXP-03, F-CON-02, N-DB-03; getrennte Dateien von T-086). `backend/finue/migrations/versions/0003_*.py`:
  `transaction.checked_origin`/`reconciled_origin` (nullable, CHECK ui/mcp/import), `bank.version` (server_default 1),
  `bank.icon`/`bank.icon_mime`, neue Tabelle `export_profile` (Name eindeutig, is_default, options/columns JSON,
  Version, Zeitstempel) und drei Listen-/Suchindizes auf `transaction`. Modelle angepasst: `Transaction` (+Herkunft)
  mit Indizes, `Bank` mit `VersionedMixin` + Icon, neu `models/export_profile.py`, Sammelimport in
  `models/__init__.py`. F-IMP-07: `duplicate.TransactionData` und `import_run` (`_to_duplicate_data`,
  `_RestoreValues`) speichern `checked_origin`/`reconciled_origin`; die "keine Spalte"-Hinweise entfernt.
  **SQLite-Umbau:** CHECK-Constraints erfordern einen Tabellen-Neubau; mit `foreign_keys=ON` haette das implizite
  `DROP TABLE transaction` die Kindzeilen in `duplicate_decision` geloescht. Die Migration schaltet die
  Fremdschluesselpruefung mit `PRAGMA foreign_keys=OFF`/`ON` nur fuer den Umbau ab (Alembic SQLite:
  `transactional_ddl=False`); `test_migration_0003` belegt mit einer Kindzeile, dass nichts verloren geht.
  Entscheidungen (Bank-Spalte heisst wie ueberall `version`, nicht `version_id`; PRAGMA-Weg) in `decisions.md`.
  Tests: neues `tests/unit/test_migration_0003.py` (Schema via `compare_metadata`, Daten ab 0002 inkl.
  Kindzeile, CHECK-Erzwingung, Profil-Eindeutigkeit, downgrade), neues `tests/unit/test_model_export_profile.py`,
  Bank-Version/Icon- und Transaction-Herkunftstests, angepasst `test_migration_0001` (17 Tabellen),
  `test_alembic_config` (Kette bis 0003), `test_models_registry` (export_profile; Bank nicht mehr unversioniert),
  `test_migration_data` (Beispieltabelle auf `account_group` umgestellt, weil `bank` in 0003 eine Spalte erhaelt),
  `test_import_restore_flags` (prueft gespeicherte Herkunft). Evidenz (Verifikationsplan, alle gruen):
  `uv run pytest tests/unit/test_migration_0003.py -q` 5 passed; `uv run pytest tests/unit -q` 1149 passed/1 skipped;
  `uv run ruff check backend tests scripts` All checks passed; `uv run ruff format --check backend tests scripts`
  145 files already formatted; `uv run mypy backend` Success (ohne Fehler). **Offen:** unabhaengige
  verifier-Pruefung (medium) steht aus -> task-done noch nicht aufgerufen; danach entsperren T-087, T-088, T-095,
  T-097, T-100 (T-085 als Abhaengigkeit). Beobachtung: `test_events.py` (T-086) war im Sammlauf einmal
  ordnungsabhaengig rot, isoliert und im zweiten Sammlauf gruen (nicht durch T-085 verursacht). Kein Push.
- **T-084 abgeschlossen (M4 Welle 1, validate.py-Geruest).** Konflikt-Check: Ja (plan.md M4 "validate.py aendern
  T-084"; keine SPEC-Aenderung, keine Fachlogik). `scripts/validate.py`: `STAGE_M4` leer in `IMPLEMENTED`
  registriert (kumulativ, `validate.py m4` faehrt m0..m4), `check_lint` prueft jetzt zusaetzlich
  `uv run ruff format --check backend tests scripts` und eine neue Regel `check_type_ignore_reasons`
  (jedes `# type: ignore[...]` unter `backend/` braucht eine nachgestellte Begruendung, sonst lint rot);
  `unjustified_type_ignore_lines` als testbarer Helfer. Die 29 driftenden Dateien mit `uv run ruff format`
  formatiert, alle **AST-identisch zu HEAD** (`ast.dump`-Vergleich, 29/29; nur `validate.py` weicht bewusst ab).
  Die vier bestehenden ignores in `fints_client.py`, `fints_orders.py` und `sync_service.py` um die Begruendung
  "python-fints liefert keine Typstubs (D2)" ergaenzt. Der eingebettete `ROUNDTRIP_SCRIPT` nach
  `scripts/roundtrip_check.py` ausgelagert (Aufruf `uv run python scripts/roundtrip_check.py`), Ausgabe und
  Pruefumfang unveraendert: `ROUNDTRIP_OK erster_lauf=5 zweiter_lauf=0 bestand=5`; `validate.py` bleibt reine
  Standardbibliothek (per AST-Test belegt). Neu: `tests/unit/test_validate_stage_m4.py` (5 Tests: m4-Registrierung,
  type-ignore-Regel positiv/negativ, Roundtrip-Skript verdrahtet, nur Standardbibliothek). Evidenz:
  `python scripts/validate.py m4` Exit 0 (m0 7/7; m1 lint "All checks passed!; 131 files already formatted; alle
  type: ignore begruendet", types 57 Dateien, unit 1094 passed/1 skipped, smoke 1 passed; m2 Roundtrip; m3
  Read-only-Guard und Integration 17 passed), `python scripts/validate.py roundtrip` Exit 0,
  `uv run ruff format --check backend tests scripts` = 131 files already formatted. Improve-Eintraege
  Formatcheck, ruff-format-Drift, type-ignore-Regel und ROUNDTRIP_SCRIPT-Auslagerung als erledigt markiert.
  Naechster Schritt: T-085 und T-086 (Welle 2) parallel. Keine Commits, kein Push.

- **M4-Taskplan angelegt (G-005, T-084 bis T-107, 24 Tasks, Branch `m4/develop`).** Wellen, Befunde und
  Abgrenzung in `plan.md` Abschnitt "M4 Taskplan". Abnahme T-107: `validate.py m4` mit roundtrip (erweitert), perf
  (N-PERF-01, 100.000 Buchungen), concurrency und API-E2E (T-106: echter Uvicorn-Prozess, Fake-Bank, SSE, ein
  zusammenhaengendes Szenario von Leerzustand bis Konto loeschen). Planungsbefunde: `search_text` bleibt bei Import
  und Sync leer (T-087), Herkunft der Kennzeichen ohne Spalte (T-085), Bank ohne Icon/Version und keine
  Exportprofile (T-085), DB-Session waehrend TAN-Pause offen (T-099), keine Anmeldung bis M10 (Auth-Naht in T-086).
  Naechster Schritt: T-084 claimen (Welle 1, solo), danach T-085 und T-086 parallel.

- **T-083 abgeschlossen (M3-Abnahme und Aufraeumen).** Konflikt-Check: **Ja** (SPEC F-SYN-01..11,
  F-AUF-04/05/07, F-CON-05, N-SEC-01..03 decken sich mit `plan.md` M3 "`validate.py m3` (+ integration)";
  positiver F-AUF-07-Zweig, keine SPEC-Aenderung). `scripts/validate.py`: `STAGE_M3` enthaelt jetzt
  Read-only-Guard und `check_integration` (`uv run pytest -m integration tests/integration -q`, Sammellauf
  muss mindestens einen Test liefern); `IMPLEMENTED["m1"]` auf `STAGE_M1` gekuerzt, damit die m0-Kette bei
  m1..m3 nicht doppelt laeuft; alle `subprocess`-Aufrufe mit `errors="replace"`, weil uv/hx unter Windows
  cp1252-Umlaute ausgeben und der Reader-Thread sonst mit `UnicodeDecodeError` abbrach.
  `tests/unit/test_requirement_refs.py` um F-SYN-01..11, F-AUF-04/05/07, F-CON-05 und N-SEC-01..03
  erweitert. Zweigpruefung: genau ein F-AUF-07-Zweig, positiv und vollstaendig (T-075 done, T-079 done,
  T-077 cancelled; kein Zweig-Task pending oder running). Keine Kategorien-/Ueberweisungsreste (Treffer nur
  als abgelehnte Zielfelder, CSV-Ignorier-Header oder Buchungstext in M2-Tests), keine Secrets
  (Scan auf Private-Key-/JWT-/Cloud-Key-Muster ohne Treffer, nur Test-Fingerprints), keine schreibenden
  FinTS-Aufrufe (Guard: 57 Dateien, 14 verbotene Namen). Evidenz: `python scripts/validate.py m3` Exit 0
  (m0 7/7; m1 lint, types 57 Dateien, unit 1089 passed/1 skipped, smoke 1 passed; m2 Roundtrip
  `erster_lauf=5 zweiter_lauf=0 bestand=5`; m3 Read-only-Guard und Integration `17 gesammelt; 17 passed,
  1 deselected`), `uv run pytest -m integration tests/integration -q` = 17 passed/1 deselected,
  `uv run pytest tests/unit/test_requirement_refs.py -q` = 1 passed, `ruff check` und
  `ruff format --check` der geaenderten Dateien gruen. **Owner-Live-Test** steht aus: `FINTS_PRODUCT_ID`
  setzen, Protokoll `docs/live-test-fints.md` gegen die echte Bank ausfuehren, ING-90-Tage-Code und
  HKCDB2/HICDB2 belegen. **Risiko ruff format:** `uv run ruff format --check backend tests scripts` meldet
  29 Dateien (M3-Sync-/FinTS-Dateien plus zwei M2-Testdateien); T-083 hat bewusst nur die eigenen Dateien
  formatiert und den Rest in `improve.md` dokumentiert, weil `validate.py m3` keinen Formatcheck verlangt.
  **M4-Anschlusspunkte:** `SyncService` als Fassade fuer die REST-Schicht (`start_sync`, `submit_tan`,
  `poll_app_approval`, `cancel`, Job-Status) und SSE `/api/events` ueber den vorhandenen Hook
  `SyncRegistry.add_listener`/`SyncEvent` (D12). Keine Commits, kein Push.

- **T-080 abgeschlossen (M3, N-SEC-02 Leckagetests Sync-Pfad).** Konflikt-Check: Ja (N-SEC-01..03,
  F-SYN-04/05/07/10 konsistent; F-SYN-05 "unbekannte Codes zeigen Originaltext" bleibt erfuellt, weil nur
  labelbare Geheimnisse im Originaltext maskiert werden). Neu: `tests/unit/test_sync_secrets.py` (9 Tests:
  Erfolg, falsche PIN, falsche TAN, Bankfehler-Fremdtext, Abbruch, Timeout, Bankzugang mit gespeicherter PIN,
  Registry-Dialogdaten). Geprueft werden je Szenario JSON-Logs, ImportRun/ImportRunError, DB-Spaltendump und
  rohe DB-/WAL-Datei, Exception-Strings/`repr`, SyncResult/SyncJob/Handoff; zusaetzlich gelangen die
  Dialogmarker in keine Datei. `log_filter.py` um passphrase, otp, jwt, csrf/xsrf, Pluralformen und
  mehrzeilige Container erweitert (bis 32 Zeilen, danach wird der Rest maskiert), Testfaelle in
  `test_log_filter.py`. Echter Leak behoben: Unbekannte Bankcodes gaben den rohen Originaltext in
  `ImportRun.message`/`ImportRunError` aus; `resolve_error` und `sync_persist._failure_outcome` maskieren
  jetzt labelbare Geheimnisse. Evidenz: `uv run pytest tests/unit/test_sync_secrets.py -q` 9 passed,
  `tests/unit/test_log_filter.py` 235 passed, gesamte Unit-Suite 1089 passed/1 skipped,
  `ruff check backend tests scripts` und `mypy backend` gruen. Offen/Risiko: unmarkierte Geheimniswerte in
  Bank-Fremdtext bleiben unerkannt; deutsche Umlaut-Plurale wie "Passwörter" erfasst der ASCII-Wortregex
  nicht. Keine echten Zugangsdaten, kein Bankzugriff, keine Commits.

- **T-082 abgeschlossen (M3, Owner-Live-Test-Doku).** Konflikt-Check: Ja (Doku deckt F-SYN-01..08/10,
  F-AUF-05/07 und N-SEC-01..03; SPEC und plan.md unveraendert). Neu: `docs/live-test-fints.md` mit
  Voraussetzungen (FINTS_PRODUCT_ID, Fernet-Schluesseldatei per Umgebungsvariable, Bank-Endpunkt
  `Bank.fints_url`, PIN-Option `store_pin`), Vorbereitung, Testfaellen (Erstsync, TAN, App-Freigabe,
  Batch, Abbruch, Timeout, ING-90-Tage, falsche PIN nur einmal, PIN-Option, beobachtbare 9800/9010),
  Erwartungsergebnissen, Nachweisen, Sicherheitsregeln, offenen Owner-Eingaben und F-AUF-07-Stand.
  `tests/unit/test_live_test_doc.py` prueft Existenz, Pflichtabschnitte, Szenarien, Pflichtbegriffe und
  das Fehlen langer Hex-Tokens. Evidenz: 6 Task-Tests und `ruff check backend tests scripts` gruen.
  **Owner-Aufgabe Live-Test:** Protokoll ausfuehren, sobald der Sync ueber die Oberflaeche oder eine
  bereitgestellte Bedienung startbar ist; dabei Produkt-ID, echte Bank-Endpunkte, ING-90-Tage-Code und
  weitere Fehlercodes mit Beleg erfassen. Offen/Risiko: `FINUE_KEY_FILE` aus SPEC N-OPS-07 ist noch
  nicht verdrahtet, aktuell liest `Settings.key_file` die Variable `KEY_FILE` (Umbenennung im M10-Task);
  der ING-90-Tage-Code ist weiterhin der unbelegte Platzhalter `ING90`; HKCDB2/HICDB2 fehlen, Banken
  mit ausschliesslich Version 2 liefern F-AUF-05. Keine echten Zugangsdaten verwendet, kein Bankzugriff,
  keine Produktionslogik, keine Commits.
- **T-078 abgeschlossen (M3, TAN-Pause/Resume/Abbruch/Timeout).** Konflikt-Check: Ja (F-SYN-04/07/10,
  N-SEC-02/03 sind mit dem Generator-/Registry-Pfad konsistent; SPEC unveraendert). `sync_service.py` fuehrt den
  Lauf als resumierbaren Generator: `NeedTANResponse` pausiert ueber den bestehenden Adapterpfad
  (`pause_dialog`/`deconstruct`), der `TanHandoff` liegt nur im Registry-/Service-RAM, und der Job wechselt auf
  `AWAITING_TAN`. `submit_tan` setzt per `from_data`/`resume_dialog`/`send_tan` fort, `poll_app_approval` fragt
  App-Freigaben wiederholt ab und zaehlt `approval_polls`; die erste Antwort gewinnt, weitere Aufrufe liefern
  `SyncTanConflictError`. `cancel` beendet den Dialog und protokolliert das laufende und die restlichen Konten als
  `abgebrochen`; vollstaendig uebernommene Konten bleiben. Der 5-Minuten-Timeout nutzt die injizierbare
  Registry-Uhr und setzt `zeitueberschreitung`. Je Konto entsteht genau eine ImportRun-Zeile: waehrend der Pause
  `wartet_auf_tan`, nach der Freigabe dieselbe Zeile `erfolgreich`/`fehler`/`abgebrochen`/`zeitueberschreitung`.
  Dazu minimale Erweiterungen in `sync_persist.py` (`persist_waiting_for_tan`, Wiederverwendung der Zeile) und
  `sync_registry.py` (Challenge-Text/Art in Job und Event). Neue Tests:
  `tests/unit/test_sync_tan.py` (11 Tests: Erfolg, falsche TAN, App-Polls, Abbruch im Batch, Timeout, parallele
  Antworten, zwei TANs im Batch, Capture-Test, Sperrfreigabe, Abbruch waehrend laufendem `send_tan` und
  waehrend laufendem App-Poll). Evidenz:
  `projects/finanzuebersicht/artifacts/T-078-verify-1795714703.json` (19 Task-Tests, Ruff, Mypy strict gruen;
  zusaetzlich 1008 Unit-Tests gruen, 1 uebersprungen). Offen/Risiko: Die DB-Session bleibt waehrend der TAN-Pause
  bis zu fuenf Minuten offen (Improve-Queue); `cancel` waehrend eines laufenden Dialogs setzt weiterhin nur die
  Abbruchmarke, der laufende Netzaufruf wird nicht unterbrochen. Seit dem Verifier-Fix gibt der Lauf danach aber
  die Registry-Sperre idempotent frei (`abort`/`expire`/`fail` im Abschluss- und BaseException-Pfad), sodass ein
  erneuter `start_sync` `reused=False` liefert. Keine Commits.

- **T-075 abgeschlossen (M3, lesender Dauer-/Terminauftragsabruf).** Konflikt-Check: Ja (T-060 positiv/
  tragfaehig; T-073 abgeschlossen; T-076 fasst `fints_client.py` nicht mehr an). Neu:
  `backend/finue/services/fints_orders.py` mit den lesenden Segmenten HKCDB1/HICDB1/HICDBS1 und
  HKCSB1/HICSB1/HICSBS1 (Feldbelegung aus der FinTS-Protokollvorlage `hbci-300.xml`), pain.001-Parser
  (Empfaenger, IBAN, Zweck, Cent, Waehrung, Ausfuehrungsdatum), Intervall-Klartext und Ableitung der naechsten
  Dauerauftrags-Ausfuehrung aus Turnus/Ausfuehrungstag (F-AUF-02). `FinTSGateway.fetch_standing_orders` ist auf
  die echte Funktion verdrahtet; TAN nutzt den bestehenden Pause-/Resume-Pfad und kombiniert bereits gelesene
  Auftraege mit der Antwort nach der Freigabe. Keine schreibenden Segmente, Read-only-Guard gruen.
  Fixtures: `tests/fixtures/fints_fake_orders.py` (vorhanden, leer, nicht unterstuetzt, Teilantwort, TAN,
  unbekannte Version); `tests/fixtures/fints_fake.py` liefert die internen Abrufhelfer fuer T-076.
  Evidenz: `projects/finanzuebersicht/artifacts/T-075-verify-1795713109.json` (40 Task-Tests, Read-only-Guard,
  Ruff und Mypy gruen; zusaetzlich 996 Unit-Tests gesamt gruen). Offen: nur Segmentversion 1 umgesetzt; Banken,
  die ausschliesslich HKCDB2/HICDB2 anbieten, fallen auf `unsupported` zurueck. Loest HKCDB selbst eine TAN aus,
  liefert die Fortsetzung nur die Dauerauftraege; HKCSB wird im naechsten Sync gelesen. `account=None` fasst
  mehrere Konten ohne Kontozuordnung zusammen; die Sync-Orchestrierung uebergibt ein Einzelkonto. Keine Commits.
  Naechster Schritt: T-079 (Persistenz) nach T-078 (TAN-Pause im Sync).

- **T-076 abgeschlossen (M3, Sync-Orchestrierung Batch/Einzel).** Konflikt-Check: Ja (F-SYN-01/02/05/06/07/11,
  F-AUF-05 und F-SYN-10 sind mit `sync_persist`, `sync_retry`, `sync_registry` und dem FinTS-Gateway konsistent;
  SPEC unveraendert). Neu: `backend/finue/services/sync_service.py` mit `SyncService.start_sync`, `SyncResult` und
  `AccountSyncResult`. Batch waehlt alle aktiven FinTS-Konten und nutzt genau einen Dialog; Einzel-Sync uebergibt
  Konten-IDs. Doppelstart liefert denselben Registry-Job (`SyncJob.wait_for_abort` ergaenzt, Event.wait statt
  blockierendem sleep). Je Konto: `compute_fetch_range`, FinTS-Abruf, `persist_account_sync`; Fehler werden ueber
  `persist_sync_failure` mit gemeinsamem `job_id` protokolliert. 9800 wird trotz Adapter-Einstufung als
  `PinWrongError` wiederholt: `_fetch_with_retry` wandelt nur dann in `FinTSBankError` um, wenn das Mapping den Code
  als wiederholbar fuehrt; echte PIN-Codes bleiben hart. `TanRequired` wird bewusst nicht behandelt (T-078).
  Evidenz: `projects/finanzuebersicht/artifacts/T-076-verify-1795711273.json` (8 Task-Tests, Ruff und Mypy gruen;
  zusaetzlich 978 Unit-Tests gesamt gruen). Offen: T-078 muss die TAN-Pause in derselben Datei ergaenzen; der
  9800-Shim sollte mit der Adapter-/T-060-Beleglage langfristig in `decisions.md` bereinigt werden. Keine Commits.

- **T-074 Fachfehler behoben (Vormerkung mit gleichem Fingerprint verdeckte gebuchte Zeile).** Konflikt-Check: Ja
  (F-DUP-02 Multimengen-Regel und F-DUP-04 "Vormerkungen nehmen nicht am Fingerprint-Abgleich teil" sind mit dem
  Statusfilter im Sync-Pfad konsistent; SPEC unveraendert). Ursache: `replace_pending` legt die Vormerkungen der
  Lieferung vor `reconcile_multiset` an; ohne Statusfilter zaehlten sie in `n_db` und die gebuchte Zeile mit
  identischem Fingerprint wurde uebersprungen. Fix: `duplicate.reconcile_multiset` hat den Keyword-Parameter
  `include_pending_in_existing` (Standard `True`); `sync_persist.persist_account_sync` setzt `False`, und
  `sync_persist._load_existing_counts` (Verdachtszaehlung) filtert `status == 'gebucht'`. Der `occurrence_index`
  bleibt ueber alle Zeilen berechnet, damit der Unique-Constraint haelt. Der Datei-Import bleibt beim Standard
  `True`, damit eine Datei mit Vormerkungen beim Re-Import weiterhin 0 neue Buchungen erzeugt (F-DUP-02);
  Beleg: `python scripts/validate.py roundtrip` = `ROUNDTRIP_OK erster_lauf=5 zweiter_lauf=0 bestand=5`.
  Regressionstest `test_gebuchte_zeile_neben_vormerkung_gleichen_fingerprints_wird_angelegt` (vor Fix rot:
  `new_count == 0`), plus zwei Tests in `test_duplicate_multiset.py` (Ausschluss + Import-Standard).
  Evidenz: 56 Tests in den vier Zieldateien, 970 Unit-Tests gesamt, ruff/mypy gruen. Offen und nicht durch T-074
  verursacht: `ruff format --check` meldet weiterhin Formatdrift an vorbestehenden Stellen in `sync_persist.py`
  und `test_sync_persist.py` (Improve-Queue). Keine Commits.
- **T-060 abgeschlossen (M3, F-AUF-07 tragfaehig).** Konflikt-Check: Ja (SPEC F-AUF-07 verlangt die Pruefung in
  der eingesetzten Version; D2 = python-fints, N-SEC-01/D11 = cryptography; keine SPEC-Aenderung noetig).
  Ergebnis: python-fints 5.0.1 hat **keine eingebaute** HKCDB-/HKCSB-Unterstuetzung (0 Treffer im gesamten Paket),
  die lesende Erweiterung ist aber **tragfaehig** - eigene `FinTS3Segment`-Unterklassen, BPD-Versionswahl ueber
  `_find_highest_supported_command` (`HICDBS`/`HICSBS`) und Antwortauswertung ueber `_fetch_with_touchdowns`.
  Abhaengigkeiten exakt gepinnt: `fints==5.0.1`, `cryptography==50.0.2` in `pyproject.toml` und `uv.lock`.
  `tests/unit/test_fints_capabilities.py` (17 Tests, offline) belegt Signaturen/Existenz von `FinTS3PinTanClient`,
  `NeedTANResponse`, `pause_dialog`/`resume_dialog`/`deconstruct`/`from_data`, `get_sepa_accounts`, `get_balance`,
  `get_transactions_xml`, `get_transactions(include_pending=False)`, `init_tan_response` als Attribut (kein
  Methodenname, gesetzt in `dialog.py:90`), Fehlercodes (9340/9910/9930/9931/9942 -> `FinTSClientPINError`, 3938 ->
  `FinTSClientTemporaryAuthError`, 9010 -> `FinTSClientError`), fehlendes HKCDB/HKCSB und die Erweiterbarkeit.
  Evidenz: `uv run pytest tests/unit/test_fints_capabilities.py -q` = 17 passed; `uv run ruff check backend tests
  scripts` = All checks passed; `uv run mypy backend` = Success (41 Dateien). Zweigentscheidung positiv:
  T-075/T-079 bleiben aktiv, T-077 per `hx.py task-set` auf `cancelled`. Details und Quellen:
  `decisions.md` (F-AUF-07-Eintrag), Skill-Referenz `references/fints.md` aktualisiert. Keine Commits.
  Naechster Schritt: Welle 1 des M3-Plans ist damit frei; T-061, T-062, T-063, T-064 und T-065 koennen parallel
  starten (T-060 als Abhaengigkeit erfuellt).
- **M3-Taskplan angelegt (G-004, T-060 bis T-083).** Der erste Task T-060 prueft F-AUF-07 und pinnt die
  Abhaengigkeiten. Positives Ergebnis aktiviert T-075/T-079 fuer den lesenden Auftragsabruf; negatives Ergebnis
  aktiviert nur T-077 nach Owner-Freigabe fuer die SPEC-Zurueckstellung. Builder laufen mit DeepSeek V4.1 Flash,
  Effort max.; medium/high Tasks werden mit Claude Sonnet 5.5, Effort medium, verifiziert. Naechster Schritt:
  T-060 auf `develop-m3` claimen. Die 24 Task-Dateien liegen unter `projects/finanzuebersicht/tasks/`.
- **M2-Dokumentation nach Merge geprueft.** `docs/export-format.md` beschreibt Schema, Versionierung, Roundtrip und
  jetzt auch Re-Import-/Wiederherstellungssemantik. Einen falschen F-DUP-Verweis bei `merged` (F-DUP-04 statt F-DUP-05)
  korrigiert. M2-Dokumentation ist damit vollstaendig; offene Owner-CSV-Beispiele und der bekannte `ruff format`-Drift
  bleiben als nicht blockierende Punkte bestehen.
- **T-059 abgeschlossen (M2-Abnahme).** Konflikt-Check: Ja (M2-Abnahme nach `plan.md` "validate.py m2 (+ roundtrip
  Import-Teil)" deckt die SPEC-Anforderungen F-DUP-01..04, F-IMP-01..08, F-EXP-04/07; die unbelegten Seed-Vorlagen
  N26/Easybank/PayPal sind als bewusste, dokumentierte Annahme in `decisions.md` festgehalten). `python scripts/validate.py m2`
  lief mit Exit 0: m0/m1 weiter gruen, lint (ruff check) und mypy gruen, 652 Unit-Tests und 1 Smoke-Test bestanden,
  Roundtrip `ROUNDTRIP_OK erster_lauf=5 zweiter_lauf=0 bestand=5`. `tests/unit/test_requirement_refs.py` prueft die
  M2-IDs jetzt zusaetzlich automatisch. Offen bleibt die Owner-Eingabe echter CSV-Beispieldateien (N26/Easybank/PayPal/
  Finanzblick) unter "Open questions"; die Seed-Vorlagen sind bis dahin unbelegt. Restrisiko: `ruff format --check`
  wuerde `export_schema.py`, `import_run.py` und zwei Testdateien umformatieren; `validate.py m2` prueft nur
  `ruff check`, daher wurde wegen "nur notwendige Fehler" nichts umformatiert. Evidenz:
  `projects/finanzuebersicht/artifacts/T-059-verify-1795700646.json` (Exit 0).
- **T-058 abgeschlossen (roundtrip-Import in validate.py).** `check_roundtrip_import()` importiert
  `tests/fixtures/export_v1_sample.json` zweimal ueber `execute_import` in einer isolierten temporaeren SQLite-Datei und
  verlangt im zweiten Lauf `new_count == 0`; zusaetzlich werden erster Lauf (5 neu), Status, Fehlerzaehler und
  Bestandsgleichheit geprueft. Registriert als eigener BLOCKS-Check `IMPLEMENTED['roundtrip']` und zusaetzlich in
  `STAGE_M2` (SPEC/plan.md: M2-Abnahme mit Roundtrip-Import-Teil). Konflikt-Check war Ja. Evidenz:
  `python scripts/validate.py roundtrip` und `python scripts/validate.py m2` beide Exit 0, m2 mit 646 Unit-Tests,
  1 Smoke-Test, ruff/mypy gruen. Naechster Schritt: Welle 8, T-059 (M2-Abnahme) nach T-055.
- **M2-Taskplan angelegt (G-003, T-044 bis T-059, 16 Tasks).** Noch keine Task gestartet. Ausfuehrungswellen,
  Parallelitaet und Modellrouting stehen in `plan.md` Abschnitt "M2 Taskplan". Kurzfassung: Welle 1 (T-044 solo)
  -> Welle 2 (T-045/T-048/T-050, 3x parallel) -> Welle 3 (T-046/T-049, 2x parallel) -> Welle 4 (T-047/T-051/T-052/
  T-056/T-057, bis 5x parallel) -> Welle 5 (T-053 solo, Synchronisationspunkt) -> Welle 6 (T-054 solo) -> Welle 7
  (T-055/T-058, 2x parallel) -> Welle 8 (T-059 Abnahme solo). Jeder `builder`-Task laeuft mit DeepSeek V4.1 Flash,
  Reasoning-Effort max. 13 von 16 Tasks sind `risk_level: medium` (Dubletten-/Import-/Versionslogik) und brauchen
  vor `task-done` eine unabhaengige `verifier`-Pruefung; nur T-044, T-056, T-057 sind `low`. Naechster Schritt:
  Welle 1 (T-044) claimen und per `builder` umsetzen, danach Welle 2 mit bis zu drei parallelen Builder-Sessions.
  Owner-Eingabe weiterhin offen: echte CSV-Beispieldateien N26/Easybank/PayPal/Finanzblick-Export fehlen -- T-056/
  T-057 bauen deshalb "nach bestem Wissen, unbelegt" und sind spaeter mit echten Dateien nachzuschaerfen.
- **T-044 abgeschlossen und unabhaengig verifiziert (PASS).** `scripts/validate.py` registriert `STAGE_M2` leer in
  `IMPLEMENTED`, waehrend `roundtrip` als eigener, noch nicht implementierter Block erhalten bleibt. Der explizite
  Konflikt-Check gegen SPEC.md und plan.md war **Ja**; `python scripts/validate.py m2` lief mit Exit 0. Naechster
  Schritt: Welle 2 mit T-045, T-048 und T-050 ueber getrennte Builder routen; T-045 und T-050 danach jeweils mit
  unabhaengigem Verifier pruefen, T-048 ist laut Taskplan low-risk.
- **M1 (Fundament) VOLLSTAENDIG ABGENOMMEN.** 41 done + 2 cancelled = 43/43 Tasks, status.md/queues.md leer (now/next/blocked). T-042 (Anforderungs-IDs) und T-043 (M1-Abnahme) in dieser Session abgeschlossen:
  - T-042: Alle 8 Pflicht-IDs (N-DB-01..04, N-OPS-01/04/05/06) waren bereits in echten Fachtests belegt, keine Nachtraege noetig. Neue `tests/unit/test_requirement_refs.py` erzwingt das dauerhaft per grep-aehnlicher Pruefung ueber alle Dateien in `tests/` (schliesst sich selbst aus). Evidenz: artifacts/T-042-verify-1795637499.json.
  - T-043: KRITISCHER FIX in `scripts/validate.py` noetig, sonst waere die Abnahme nie gruen geworden: `check_lint/check_types/check_unit/check_smoke` riefen `[sys.executable, "-m", "uv", "run", ...]` auf - `uv` ist ein eigenstaendiges Executable, kein Python-Modul, das schlaegt mit JEDEM System-Python fehl ("No module named uv"). Fix: direkter Aufruf `["uv", "run", ...]` ueber PATH. Zusaetzlich `check_lint` um `scripts` ergaenzt (T-040-DoD verlangte `backend tests scripts`, das Skript prueft vorher nur `backend tests`). `python scripts/validate.py m1` lief dadurch zum ERSTEN MAL end-to-end gruen. Von verifier (Claude Sonnet 5.5) unabhaengig als PASS bestaetigt, inkl. Review-Checkliste (keine Secrets, kein rohes SQL, keine Kategorie/Budget/Ueberweisung-Reste, Anforderungs-IDs referenziert, git status unauffaellig). Evidenz: artifacts/T-043-verify-1795637851.json.
  - Kleiner, nicht blockierender Kosmetik-Hinweis vom Verifier: `IMPLEMENTED["m1"]` kombiniert `STAGE_M0 + STAGE_M1`, wodurch die m0-Checks bei `validate.py m1` doppelt ausgegeben werden (keine Fehlinformation, nur redundante Ausgabe) - bei Gelegenheit auf `IMPLEMENTED["m1"] = STAGE_M1` umstellen.
   - **HISTORISCHER M1-HINWEIS:** M1 wurde noch nach der damaligen Einzel-Commit-Regel abgeschlossen. Fuer M2 und folgende Milestones gilt die aktualisierte DEV-Branch-Regel aus `AGENTS.md`: eigener DEV-Branch, unmittelbare Worker-/Verifier-Commits und Pushes in den DEV-Pfad ohne erneute Freigabe; Merge nach `main` nur nach `task-approve`.
  - Danach: Goal G-002 (M1) ist fachlich fertig. Naechstes Ziel laut plan.md waere M2 - Planer-Lauf fuer M2-Tasks noetig, sobald der Owner den Commit freigegeben hat und "weiter mit M2" sagt.
- T-041 (M1 mypy strict) abgeschlossen und verifiziert. `uv run mypy backend` = Success, 0 Fehler in 30 Dateien (strict). Der Code war bereits typ-sauber; einziger DoD-Verstoss war ein undokumentierter `# type: ignore[no-untyped-def]` in models/audit.py:110 (SQLAlchemy-Event-Listener) - ersetzt durch konkrete Annotation (Mapper[AuditLog], Connection, target: AuditLog), ignore entfernt. Backend enthaelt jetzt 0 `type: ignore`. ruff check backend = All checks passed; pytest tests/unit = 400 passed. Evidenz: projects/finanzuebersicht/artifacts/T-041-verify-1795637243.json. Naechster freier Task: T-042 (Anforderungs-IDs in Tests), danach T-043 (M1-Abnahme).
- T-040 (M1 Lint, ruff) abgeschlossen und verifiziert. Ist-Zustand: `ruff check` meldete nur 3 E501 in scripts/validate.py, `ruff format --check` wollte 19 Dateien reformatieren (u. a. die nachbearbeitete Migration 0001_initial.py, 688 Zeilen). `ruff check --fix` (nur sichere Fixes) + `ruff format` angewandt; danach `All checks passed!` und `62 files already formatted` (Exit 0). Keine noqa, keine Logikaenderung: alle 19 geaenderten Dateien sind per ast.dump-Vergleich AST-identisch zu HEAD. pytest tests: 401 passed. Evidenz: projects/finanzuebersicht/artifacts/T-040-verify-1795636920.json. Naechster freier Task: T-041 (M1 mypy strict) -> danach T-042, T-043 (M1-Abnahme).
- T-038 nachkorrigiert (Defekt aus unabhaengiger Pruefung): launch.json lief mit cwd=backend, tasks.json "Demo-Daten" im Repo-Root ohne DATABASE_URL - der relative Pfad `sqlite:///data/dev.db` loeste daher auf zwei verschiedene Dateien auf (backend/data/dev.db vs. data/finue.db), Demo-Daten fehlten im per F5 gestarteten Server. Fix: launch.json cwd=${workspaceFolder} + PYTHONPATH=${workspaceFolder}/backend, tasks.json options.cwd=${workspaceFolder} + options.env.DATABASE_URL=sqlite:///data/dev.db, docs/entwicklung.md angeglichen. tests/unit/test_vscode_config.py prueft jetzt cwd- und URL-Gleichheit beider Kontexte plus aufgeloesten Absolutpfad. Evidenz: projects/finanzuebersicht/artifacts/T-038-verify-1795635686.json. Naechster freier Task: T-039 (M1 Smoke-Test).
- T-021 bis T-029 und T-036 abgeschlossen (alle PASS ueber builder DeepSeek V4.1 Flash effort max + verifier Claude Sonnet 5.5, T-024/T-025/T-027/T-029 mit effort high wegen Sicherheits-/Strukturrelevanz). T-028+T-036 wurden ECHT PARALLEL bearbeitet (zwei Builder, zwei Verifier gleichzeitig, unabhaengige Dateien, keine Race Condition). T-029 (Erste Migration 0001) per autogenerate erzeugt, alle 15 Tabellen/Indizes/FKs per compare_metadata bestaetigt, downgrade() korrekt ohne Logik (N-DB-03). Naechster freier Task: T-030 (Migrations-Runner) - lineare Kette, keine Parallelisierung aktuell moeglich.
- WICHTIG vor T-031/T-032: env.py braucht einen render_item-Hook fuer finue.db.mixins.UtcDateTime, sonst muss der Import bei jeder kuenftigen Migration manuell nachgetragen werden (T-029 mmusste das einmalig von Hand fixen). Ausserdem test_alembic_config.py::test_initial_migration_is_single_head haertet den Head-Wert "0001" hart - bricht bei der naechsten Migration, vor T-031/T-032 generisch umstellen.
- T-029 bis T-038 abgeschlossen. T-031+T-032 (Migrationstests) und T-033+T-036/T-028 sowie T-034+T-037 und T-035+T-038 jeweils ECHT PARALLEL mit mehreren gleichzeitigen Buildern/Verifiern bearbeitet (insgesamt 3x Dreifach- bzw. Doppel-Parallelisierung in dieser Session), keine Race Conditions, keine Dateikonflikte.
- ZWEI ERSTPRUEFUNGEN FAIL, nach Nachbesserung PASS: (1) T-033 - upgrade_to_head im App-Lifespan loeste Alembics fileConfig aus und ersetzte das Projekt-Logging inkl. N-SEC-02-Secret-Filter durch einen nackten Alembic-Handler; gefixt in migrate.py/env.py (configure_logger-Attribut), zentral fuer alle Aufrufer. (2) T-038 - launch.json (cwd=backend) und der Demo-Daten-Task (cwd=Root, keine DATABASE_URL) zeigten auf zwei verschiedene SQLite-Dateien, Demo-Daten waeren im F5-Server nie sichtbar gewesen; gefixt durch einheitliches cwd=Workspace-Root plus expliziter DATABASE_URL in beiden Konfigurationen. Beide Faelle zeigen: die automatisierten pytest-Verifikationsplaene allein haetten die Mangel nicht gefunden, erst die gezielte Review-Checkliste des Verifiers.
- Status: 36/43 Tasks verifiziert erledigt. Naechster freier Task: T-039 (Smoke-Test). Danach lineare Schlusskette T-040 (Lint) -> T-041 (mypy strict) -> T-042 (Anforderungs-IDs) -> T-043 (M1-Abnahme).
- T-027 kritische, gut begruendete DoD-Korrektur in decisions.md dokumentiert: 6 statt 3 Tabellen ohne version-Spalte (zusaetzlich bank, user_session, mcp_token), bestaetigt gegen SPEC F-CON-02 und bereits abgenommene T-017/T-024.
- SPEC-Planungsluecken aus T-025-Verifikation (kein FAIL, DoD war korrekt umgesetzt, aber SPEC F-BAK-02/05/06 verlangt mehr): smb_domain-Feld bei BackupJob fehlt, encrypt_enabled-Flag + backup_password_encrypted (LargeBinary) fuer das Backup-Verschluesselungspasswort fehlt komplett, optional target_label an BackupRun fuer Ziel-Anzeige nach Job-Loeschung. Vor T-027/Migration (T-028/T-029) durch Planer klaeren, sonst folgt spaeter eine Nachtragsmigration.
- Weitere nicht-blockierende Hinweise aus T-025: backup.py size_bytes sollte vor erster Migration auf BigInteger (PostgreSQL/MySQL-Limit bei Integer ~2 GiB), CheckConstraint retention_count >= 1 fehlt.
- Hinweis aus T-028-Verifikation: env.py ist bisher nur ueber ScriptDirectory/Config-Ladbarkeit getestet, kein echter Migrationslauf (upgrade/downgrade) gegen eine Temp-DB. Bei T-029 oder T-031 nachholen.
- Kleine, nicht blockierende Anmerkung aus T-023-Verifikation: Docstring in import_template.py erwaehnt "passive_deletes", das weder gesetzt noch bei der viewonly-Relation noetig ist - bei Gelegenheit korrigieren.
- WICHTIG fuer Owner: Verifier von T-024 fand im Arbeitsverzeichnis zwei Aenderungen, die NICHT aus einem Harness-Task stammen: `AGENTS.md` (Kilo-Code-Abschnitt, bereits zu Sessionbeginn vorhanden) und `SPEC.md` (83 Zeilen, reine Markdown-Tabellen-Neuformatierung, inhaltlich unveraendert soweit geprueft). Keine Scope-Verletzung durch T-021..T-024, aber `SPEC.md`-Aenderungen sind laut AGENTS.md freigabepflichtig - bitte `git diff SPEC.md` pruefen und bewusst committen oder verwerfen (`git checkout -- SPEC.md`), bevor weitere Tasks laufen.
- Hinweis aus der Verifikation: vor T-027/T-028 `backend/finue/models/__init__.py` um Sammelimport aller Modellmodule ergaenzen, sonst fehlen Tabellen bei Alembic-Autogenerate (Improve-Queue).
- Auf Anweisung des Owners: "weiter mit M1" (Goal G-002). Ablauf siehe `.claude/agents/finanzuebersicht-lead.md`.
- M1 Task 1: Stufe `m1` in `scripts/validate.py` implementieren (lint, types, unit, smoke), Python 3.12 per `uv python install 3.12`.
- M3 Task 1: Machbarkeitspruefung F-AUF-07. `init_tan_response` und Codes fuer falsche PIN/Sperre belegen.
- M5: `uv run playwright install chromium`.

## Blockers

- Keine fuer M1. Docker-Engine laeuft nicht, erst fuer M10 noetig.

## Open questions (Owner-Eingaben, SPEC Abschnitt 7)

- FinTS-Produkt-ID (Live-Test FinTS, M3): Protokoll in `docs/live-test-fints.md`, Ausfuehrung durch den Owner.
- Anonymisierte CSV-Beispiele N26, Easybank, PayPal, Finanzblick-Export in `data/samples/` (M2, M4).
- Bank-Icons bzw. Websites je Bank (M7).
- SMB-Ziel fuer Backups (Live-Test Backup, `docs/live-test-backup.md`, M8).
- Docker-Hub-Repository und `docker login` (M10).
- Bank-Endpunkte (nur ING als Owner-Vorgabe, unbelegt) und Fehlercodes (u. a. echter ING-90-Tage-Code): Quelle oder Beleg aus `docs/live-test-fints.md`.

## Planer-Update 2026-11-26 (M4-Entlastung)

- Neue Etappentasks: T-108, T-109, T-110 (zu T-094), T-111, T-112 (zu T-099), T-113, T-115 (zu T-106). T-114 cancelled (Fehlanlage).
- T-094, T-099, T-106 stehen auf `blocked` als Abschlusstasks (nicht claimbar). Nach Abschluss aller Etappen: `task-set <T> pending`, dann claimen und `task-done`.
- T-085/T-086 unveraendert. Naechste freie Tasks nach T-086: T-111; nach T-087: T-108.
- T-097/T-098 nur Beobachtung, Schnitt in plan.md vorbereitet.
- Details und Risiken: plan.md Abschnitt "M4-Entlastung".

- **T-085 verifiziert (verifier, unabhaengig): PASS.** Selbst ausgefuehrt: Zielttests 49 passed, `tests/unit` 1149 passed/1 skipped (4 Laeufe), ruff check/format, mypy backend gruen; `hx.py task-done` 0 (Evidenz `artifacts/T-085-verify-1795722769.json`). Mutationstest: ohne `PRAGMA foreign_keys=OFF` schlaegt `test_migration_0003_preserves_existing_data_from_0002` fehl (Kindzeile `duplicate_decision` ginge verloren), der Test ist also wirksam. Downgrade = NotImplementedError entspricht SPEC N-DB-03 und 0002. CHECK-Werte identisch mit `FlagOrigin`. Abweichung `bank.version` statt `version_id` ist in decisions.md begruendet. `test_events.py::test_warteschlange_verwirft_aeltere_bei_ueberlauf`: deterministisch (eigener Bus, kein globaler Zustand, kein Zufalls-/xdist-Plugin), in 4 Gesamtlaeufen plus Kombinationslauf gruen; der einmalige Rotlauf stammt vermutlich vom gleichzeitig im selben Arbeitsbaum bearbeiteten T-086 (Parallelstand). Kein Code geaendert, kein T-086-Befund. Naechster Schritt: T-087, T-088, T-095, T-097, T-100 entsperrt.
- **T-095 (builder) umgesetzt: Abstimmungsuebersicht je Konto und Gruppe (F-ABS-03).** Neuer Service `backend/finue/services/reconciliation.py` (MCP-faehig, D10) und Router `backend/finue/api/reconciliation.py` mit `GET /api/accounts/{id}/reconciliation` und `GET /api/groups/{id}/reconciliation`. Regeln und Herleitung des Anfangssaldos in `decisions.md` (2026-11-27), Endpunkte/Beispiel in `docs/api.md`. Kern: abgestimmter Saldo = Anfangssaldo + abgestimmte, gebuchte, nicht zusammengefuehrte Buchungen; Anzahl/Summe der nicht abgestimmten gebuchten Buchungen; Differenz = Banksaldo - abgestimmter Saldo (`None` ohne Banksaldo); vorgemerkte (F-ABS-01) und zusammengefuehrte Buchungen (F-DUP-04) zaehlen nicht; Gruppe nur aktive Mitglieder, Fremdwaehrung je Waehrung getrennt (nicht addiert). Tests `tests/unit/test_reconciliation.py` (Handrechnung mit zusammenfuehrter und vorgemerkter Buchung) gruen; Vertragstabelle um beide GET-Routen erweitert. Fund (Race): `backend/finue/api/routes.py` und `tests/unit/test_api_contract.py` tragen neben meinen Zeilen Aenderungen paralleler Builder (T-089/T-090/T-092); der T-095-Commit laesst diese beiden Shared-Files bewusst aus, damit kein Commit mit Verweisen auf noch ungetrackte Module entsteht. Naechster Schritt: sobald der kombinierte Arbeitsbaum (ruff/mypy) wieder gruen ist, `hx.py task-done T-095` und die beiden Shared-Files mitcommitten.
  Nachtrag T-095: `hx.py task-done T-095` = done (Evidenz `artifacts/T-095-verify-1795782843.json`). Die Registrierung in `backend/finue/api/routes.py` und die Vertragseintraege in `tests/unit/test_api_contract.py` sind mit dem T-092-Commit `ef80658` (Shared-Files) mitgegangen; T-095 selbst steckt in `0c88e91` (Service/Router/Tests/Doku/Decisions/Handoff/Improve/Task) und `763dd1c` (Task done, Evidenz, Lesson). Verifikation am Tip gruen: `tests/unit/test_reconciliation.py` 25 passed, ruff check/format und mypy grün, volle Unit-Suite 1339 passed/1 skipped.

- **T-097 (builder) umgesetzt: Export-Service fuer JSON und Markdown (F-EXP-01/02/04/05).** Neuer Service `backend/finue/services/export.py` und Router `backend/finue/api/export.py` (`POST /api/export`). Umfang Konto/Gruppe/Suchtreffer (gemeinsamer `TransactionQuery` aus T-092) oder Auswahl (`transaction_ids`); Zeitraum und Filter der Ansicht gelten, Grenze `export_max_rows` (Verletzung = 413 `payload_too_large`). Dateiname `slug_von_bis.ext` in ASCII (Umlaute transkribiert; bei Zeitraum `alle` aus Min/Max der Daten). JSON nach Schema v1 (`docs/schemas/export-v1.schema.json`), nur Kontostammdaten ohne Zugangsdaten, verlustfreier Re-Import in eine frische DB belegt; der Vorkommen-Index wird 1-basiert ausgegeben (Bestand fuehrt ihn 0-basiert, Fund als Improve-Eintrag). Markdown mit Kopf (Konto/Gruppe, Zeitraum, Anzahl, Summe), Tabelle (Datum, optional Konto in der Gruppenansicht, Gegenpartei, Verwendungszweck, Betrag, Notiz), Summenzeile, deutschem Zahlen-/Datumsformat und Maskierung von `|`, Backslash und Zeilenumbruch; `?download=false` liefert Markdown als `text/plain` fuer die Zwischenablage. Zusammengefuehrte Buchungen ausgeschlossen (F-DUP-04). Tests `tests/unit/test_export_json_md.py` (14 passed), Vertragstabelle um `POST /api/export` erweitert, `docs/api.md` und `docs/export-format.md` ergaenzt. Verifikationsplan am Tip gruen (Zieltest, `ruff check`, `ruff format --check`, `mypy backend`). Risiko medium: unabhaengige Verifier-Pruefung folgt, `task-done` bewusst dem Verifier ueberlassen. Shared-Files `routes.py`/`config.py`/`test_api_contract.py` nur hunkweise gestaged (parallele Builder im selben Baum).

- **T-100 (builder) umgesetzt: REST fuer Datei-Import, Importvorlagen, Lauf-Protokoll und lesende Auftraege (F-IMP-01..08, F-AUF-02/04/05, F-CON-02).** Neue Services `backend/finue/services/import_uploads.py` (UploadStore, nur RAM, Token+TTL 300 s, max 32) und `backend/finue/services/import_detect.py` (CSV/fremdes JSON/eigenes JSON, Formatvorschlag mit Korrekturwerten F-IMP-02); neue Router `backend/finue/api/imports.py` (`POST /api/import/detect|preview|execute`, `GET /api/import-runs`), `backend/finue/api/import_templates.py` (CRUD + Vorschlag je Konto, Versionspruefung) und `backend/finue/api/standing_orders.py` (`GET /api/accounts/{id}/standing-orders`, `GET /api/standing-orders`, rein lesend). `services/import_templates.py` um `TemplateVersionConflict`/`expected_version` erweitert, Container/Main um `upload_store`, Config um `api_import_upload_ttl_seconds`. Ereignisse `import.finished` und `transaction.changed`. Tests `tests/unit/test_import_api.py` (16) und `tests/unit/test_orders_api.py` (7) gruen; Vertragstabelle um 8 Routen erweitert, `docs/api.md`/`decisions.md` ergaenzt. Verifikationsplan am Tip gruen (Zieltests 23, `ruff check`, `ruff format --check`, `mypy backend`). Risiko medium: unabhaengige Verifier-Pruefung folgt, `task-done` bewusst dem Verifier ueberlassen (Status bleibt running). Shared-Files `routes.py`/`test_api_contract.py`/`docs/api.md`/`decisions.md`/`config.py` tragen neben meinen Zeilen bereits committete Zeilen von T-093/T-097; im T-100-Commit integriert. Naechster Schritt: verifier-Pruefung T-100 gegen Definition of Done und Review-Checkliste.

