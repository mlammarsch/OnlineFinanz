# T-109 Evidenz (builder): Notiz ersetzen/anhaengen und externe Referenz (F-ABS-05, F-CON-01/02)

Datum: 2026-11-27, Branch `m4/develop`, Worker builder. Risiko **medium**: die
unabhaengige Pruefung durch `verifier` folgt; `task-done` erst danach.

## Umfang (Etappe T-094-B)

- **`backend/finue/services/transaction_edit.py`** (Edit-Kern aus T-108
  erweitert):
  - `set_note(session, id, *, text, origin, expected_version=None, events=None)`
    ersetzt die Notiz (F-CON-02, F-ABS-05). Ist der Wert gleich, bleibt die
    Buchung unveraendert (kein Versionssprung, kein Audit, kein Ereignis).
  - `append_note(session, id, *, text, origin, events=None)` haengt **atomar**
    an: **einzelner** UPDATE-Ausdruck
    `note = CASE WHEN note IS NULL OR note='' THEN :text ELSE note || :sep || :text END`
    (SQLAlchemy-Core, **kein** Read-Modify-Write). Trennzeichen
    `NOTE_APPEND_SEPARATOR = "\n"`.
  - `set_external_ref(session, id, *, value, origin, expected_version=None, events=None)`
    (F-ABS-05, F-CON-02); Grenze 255 Zeichen wie die Spalte
    `transaction.external_ref` (String(255)).
  - Beide ersetzenden Routen nutzen die Setz-/Versionslogik; nach jeder echten
    Aenderung wird `search_text` ueber `services/search_text.refresh_search_text`
    neu gebaut (F-SUC-01, F-ABS-05), ein Audit-Eintrag geschrieben
    (`source`=Herkunft, F-MCP-09) und `transaction.changed` gemeldet (F-CON-04).
  - `TransactionFieldResult` als Ergebnisdatensatz; `_write_audit` als
    gemeinsamer Audit-Schreiber; `_commit_or_conflict` fuer das optimistische
    Sperren.
- **`backend/finue/api/transactions.py`**: DTOs `NoteReplaceRequest`
  (`text`, `version` **Pflicht**), `NoteAppendRequest` (`text`, nicht leer, kein
  `version`), `ExternalRefRequest` (`value`, `version` **Pflicht**); Routen
  `PUT /api/transactions/{id}/note`, `POST .../note/append`,
  `PUT .../external-ref`; gemeinsamer Fehleruebersetzer `_run_edit`
  (404/409 mit `current`/422 Klartext).
- **`tests/unit/test_api_contract.py`**: drei Vertragseintraege (Notiz ersetzen
  `version`, Anhaengen `setzen`, Referenz `version`).
- **`docs/api.md`**: Abschnitt erweitert (Routentabelle, Regeln fuer Notiz /
  Anhang / Referenz, Ereignisabschnitt).
- **`decisions.md`**: T-109-Eintrag (atomarer Anhang vs. optimistisches Sperren,
  Trennzeichen, Suchspalten-Neubau).

## DoD-Abgleich

- `PUT /api/transactions/{id}/note {text, version}` ersetzt mit
  Versionspruefung und 409 inkl. aktuellem Stand: **erfuellt**
  (`test_notiz_ersetzen_setzt_text_und_version`,
  `test_notiz_ersetzen_versionskonflikt_ist_409_mit_stand`,
  `test_notiz_ersetzen_ohne_version_ist_422`).
- `POST .../note/append {text}` atomar per einzelnem UPDATE-Ausdruck ohne
  Read-Modify-Write, Trennzeichen dokumentiert; Test mit parallelem Anhaengen:
  **erfuellt** (`test_notiz_anhaengen_mit_trennzeichen`,
  `test_paralleles_anhaengen_verliert_keinen_text`,
  `test_anhaengen_ignoriert_veralteten_leseversuch`).
- `PUT .../external-ref {value, version}` (F-ABS-05): **erfuellt**
  (`test_externe_referenz_setzen_und_versionspruefung`,
  `test_externe_referenz_zu_lang_ist_422`).
- `search_text` bei Notiz und Referenz aktualisiert: **erfuellt**
  (`test_search_text_wird_bei_notiz_und_referenz_aktualisiert`,
  `test_search_text_nach_anhaengen`).
- Audit je Aenderung, Ereignis `transaction.changed` ueber den Kern:
  **erfuellt** (`test_audit_und_ereignis_bei_notizaenderung`,
  `test_service_notiz_und_referenz_herkunft_mcp`).
- Eintraege in `test_api_contract.py`; Tests in
  `tests/unit/test_transaction_edit.py`: **erfuellt**.
- NICHT in dieser Etappe: Batch, Regressionstest Sync/Re-Import — **nicht
  angefasst** (kein Import-/Sync-/Export-Code geaendert).

## Evidenz

- Etappen-Verifikationsplan `uv run pytest tests/unit/test_transaction_edit.py
  tests/unit/test_api_contract.py -q` = **48 passed**.
- `uv run pytest tests/unit -q` = **1515 passed, 1 skipped**.
- `uv run ruff check backend tests scripts` = All checks passed;
  `uv run ruff format --check backend tests scripts` = 201 files already
  formatted; `uv run mypy backend` = Success (96 Dateien).
- **Mutationstest (atomarer Anhang):** `append_note` durch eine naive
  Read-Modify-Write-Variante ersetzt -> `test_anhaengen_ignoriert_veralteten_leseversuch`
  **rot** (naiv: `StaleDataError` bzw. verlorener Text); mit dem einzelnen
  UPDATE-Ausdruck **gruen**. Damit ist die Atomaritaet nicht nur behauptet,
  sondern belegt.

## Hinweis fuer den Verifier

- Der Test `test_anhaengen_ignoriert_veralteten_leseversuch` haelt eine
  **starke** Referenz auf das ORM-Objekt: die SQLAlchemy-Identitaetskarte haelt
  Objekte nur schwach, sonst laedt die Sitzung still neu und die Mutation griffe
  nicht.
- Der Suchspalten-Neubau beim Anhaengen ist eine zweite Anweisung in derselben
  Transaktion; die Anhang-Anweisung haelt die Zeilensperre bis zum Commit, daher
  ist der Stand konsistent (Improve-Kandidat fuer eine echte Einzelanweisung).
