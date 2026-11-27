# T-110 Evidenz (builder): Batch mit Ergebnis je Buchung und Regression Sync/Re-Import

Datum: 2026-11-27, Branch `m4/develop`, Worker builder. Risiko **medium**: die
unabhaengige Pruefung durch `verifier` folgt; `task-done` erst danach.

## Umfang (Etappe T-094-C)

- **`backend/finue/api/transactions.py`** (Router um die Batch-Route erweitert,
  Kern unveraendert):
  - `POST /api/transactions/batch` mit Body
    `{ids, action checked|reconciled|note_set|note_append, value, versions?}`.
  - Resultat je Buchung als `BatchItemResult` (`geaendert`/`unveraendert`/
    `konflikt`/`fehler`); bei `konflikt` traegt `current` den aktuellen Stand
    (F-CON-03). Ergebnisfolge = Reihenfolge der Anfrage.
  - Der Batch ruft **je Eintrag genau den Kern aus T-108/T-109** auf
    (`set_flag`/`set_note`/`append_note`, `origin="ui"`); **keine** zweite
    Schreibpfad-Logik. Die Schleife liegt im Router (Ergebnis-Vokabel und
    HTTP-Uebersetzung an einer Stelle).
  - Konflikt/Fehler eines Eintrags rollt nur dessen Session-Transaktion zurueck
    (je Eintrag eigener Commit wie die Einzelrouten); der Rest laeuft weiter
    (F-CON-03).
  - `versions` ist **optional** (Abbildung Buchungs-ID -> bekannte Version): wo
    mitgegeben, prueft der Server optimistisch je Eintrag (F-CON-02); ohne
    Version gilt reine Setz-Semantik.
  - Grenze ueber den vorhandenen zentralen `check_batch_size`
    (`api_max_batch_size`, F-CON-03); Validierung von Wert je Aktion, keine
    doppelten IDs, mindestens ein Eintrag.
  - Neue DTOs/Typen: `BatchEditAction`, `TransactionBatchRequest`.
- **`tests/unit/test_api_contract.py`**: Vertragseintrag `/api/transactions/batch`
  (POST, Art `setzen`).
- **`tests/unit/test_transaction_batch.py`** (neu, 23 Tests): Setzen/Zuruecksetzen
  auf Auswahl, Reihenfolge, Idempotenz, Konflikt bricht nicht ab, Versionsangabe
  je Eintrag, Fehler je Eintrag (unbekannte Buchung, vorgemerkte Abstimmung),
  Notiz ersetzen/anhaengen (Suchspalte), Grenze genau/an der Grenze,
  Validierung (leer/doppelt/Wert/Aktion/Version<1), Audit und Ereignis je
  Aenderung sowie die **F-ABS-06-Regression** (Batch-Aenderungen bleiben bei
  FinTS-Sync und CSV-Re-Import erhalten, F-CON-05).
- **`docs/api.md`**: Routentabelle um die Batch-Zeile und ein Abschnitt
  "Batch-Aktion auf eine Auswahl" (Body, Beispielantwort, Regeln) ergaenzt.
- **`decisions.md`**: T-110-Eintrag (REST-Schleife ueber den T-108/T-109-Kern,
  optionales `versions`, per-Eintrag-Commit/Rollback, Grenze, Abgrenzung
  externe Referenz).
- **`improve.md`**: zwei Kandidaten (M9-Dispatcher mit neutralem Outcome;
  optionales `versions` vs. F-CON-02 in der UI pruefen).

Die Batch-Aktionen folgen F-SEL-03 (Notiz setzen/anhaengen, geprueft/abgestimmt);
die externe Referenz ist bewusst **nicht** Teil des Batches.

## DoD-Abgleich

- `POST /api/transactions/batch {ids, action ..., value, versions?}` mit Ergebnis
  je Buchung: **erfuellt** (`test_batch_setzt_kennzeichen_auf_auswahl`,
  `test_batch_erhaelt_die_reihenfolge_der_ids`, `test_batch_idempotent_liefert_unveraendert`).
- Konflikte (Stand je Eintrag) brechen den Batch nicht ab: **erfuellt**
  (`test_konflikt_bricht_den_batch_nicht_ab`, `test_versionsangabe_wird_je_eintrag_geprueft`).
- Batchgrenze zentral konfiguriert und getestet: **erfuellt**
  (`test_batch_ueber_grenze_ist_422`, `test_batch_genau_an_der_grenze_ist_erlaubt`;
  Grenze ueber `check_batch_size`/`api_max_batch_size`).
- Nutzt ausschliesslich den Kern aus T-094-A/B: **erfuellt** (Router ruft
  `set_flag`/`set_note`/`append_note`; keine zweite Schreiblogik).
- Regressionstest Sync und Re-Import aendern Kennzeichen, Notiz und externe
  Referenz nicht (F-ABS-06, F-CON-05): **erfuellt**
  (`test_batch_aenderungen_ueberleben_sync_und_reimport`).
- Eintrag in `test_api_contract.py`; Tests in `tests/unit/test_transaction_batch.py`:
  **erfuellt**.

## Evidenz

- Etappen-Verifikationsplan
  `uv run pytest tests/unit/test_transaction_batch.py tests/unit/test_transaction_edit.py tests/unit/test_api_contract.py -q`
  = **71 passed**.
- `uv run pytest tests/unit -q` = **1538 passed, 1 skipped**.
- `uv run ruff check backend tests scripts` = All checks passed;
  `uv run ruff format --check backend tests scripts` = 202 files already
  formatted; `uv run mypy backend` = Success (96 Dateien).

## Hinweis fuer den Verifier

- Die Batch-Schleife ruft die unveraenderten Kernfunktionen auf; geprueft wird
  damit zugleich, dass der Kern aus T-108/T-109 nicht umgangen wurde. Ein
  Konflikt-Eintrag (`konflikt`) traegt `current` mit dem aktuellen Stand.
- `versions` ist im Batch optional; ohne Version gilt Setz-Semantik. Das ist
  bewusst (DoD `versions?`; Auswahl-Aktion) und in `decisions.md` festgehalten.
- Der F-ABS-06-Regressionstest erzeugt eine Bestandsbuchung mit gueltigem
  Fingerprint und fuehrt sowohl `persist_account_sync` (F-CON-05) als auch
  `execute_import` (CSV-Re-Import) mit derselben Buchung aus; Kennzeichen,
  Notiz, externe Referenz und Version bleiben gleich.
