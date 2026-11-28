# T-091 Verifier-Bericht (unabhaengig, Claude Sonnet 5.5, medium)

Ergebnis: **PASS**. Pruefung auf sauberem Detached-Worktree bei b9e5fff (enthaelt ed8ed54, 41150dd; Shared-Wiring unveraendert gegenueber HEAD 38c85ce, dessen Diff nur T-101-Dateien beruehrt).

## Vertrag
Die fuenf ergaenzten Eintraege (GET /api/banks/lookup, POST /api/bank-accesses, PATCH /api/bank-accesses/{access_id}, POST .../discover, POST .../accounts) entsprechen exakt den Routen in `backend/finue/api/bank_accesses.py` (Zeilen 234/248/283/303/324, Methoden und Pfade identisch). T-096-Eintraege (`/api/duplicates*`) sind unveraendert vorhanden. `/api/banks/lookup` wird vor `/api/banks/{bank_id}` eingebunden (routes.py Z. 65f).

## Ausgefuehrte Pruefungen (alle gruen)
- test_fints_wizard.py + test_api_contract.py: 26 passed
- `python scripts/validate.py m3`: bestanden (smoke, Roundtrip, FINTS_READONLY_OK dateien=101, integration 17 passed)
- tests/unit: 1573 passed, 1 skipped; tests/integration (-m integration): 17 passed
- ruff check: ok; ruff format --check: 209 Dateien ok; mypy backend: Success (101 Dateien)

## Fachliche Pruefung
- BLZ: genau 8 Ziffern (422 sonst), keine Vorbelegung, `fints_configured` aus fints_url.
- SEPA/HIUPD/HITANS: build_discovery_result uebersetzt Konten, Kontoinformationen (Name/Typ), TAN-Verfahren; Test mit Fake-Client.
- already_exists: normalisierte IBAN gegen Bestand (auch Offline-Konten); Anlage ueberspringt vorhandene und doppelte IBANs, leere Auswahl -> 422.
- Discovery: Job ueber `claim_sync`/SyncRegistry (Sperre je Zugang), TAN-Pause/Resume ueber /api/sync/jobs/{id}/tan; falsche PIN -> Klartext "Die Bank hat die PIN abgelehnt." (F-SYN-05).
- PIN-Cipher: `_cipher_for` baut Cipher nur bei store_pin=true; sonst pin_encrypted NULL; bei true Fernet, Klartext nicht im Chiffretext.
- Read-only: Methodenlog des Fake-Clients nur FAKE_READ_OPERATIONS|DIALOG_OPERATIONS, Schreib-Menge disjunkt; Guard gruen.
- Leckage: PIN nicht in Antwort, Job-Antwort, Log (DEBUG), Ereignissen.
- Keine ImportRuns: Discovery-Pfad schreibt keine ImportRun-Zeilen (Code `_run_discovery`). Ad-hoc-Pruefung im Scratch-Worktree (nicht committet): nach zwei Discovery-Laeufen (store_pin=true) ImportRun-Count 0, zweiter Lauf erhaelt neue job_id (Sperre freigegeben), PIN-Marker nicht in der SQLite-Datei: 14 passed.

## Hinweis (nicht blockierend)
Die bestehenden Tests asserten "keine ImportRuns" und "DB-Klartext" nicht explizit. Der Ad-hoc-Test liegt als Patch vor (Temp\kilo\t091-extra-test.patch) und kann als Haertung uebernommen werden. Kein Defekt, daher keine Codeaenderung und kein Commit durch den Verifier.
