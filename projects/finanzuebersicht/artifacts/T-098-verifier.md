# T-098 Verifier-Bericht

Urteil: **PASS** (Commit 6247eb9 inkl. 1380cb2, sauberer Detached-Worktree, unabhaengig erneut ausgefuehrt, 2026-11-27).

## Umkehrbarkeit _guard_cell/_unguard_cell (F-EXP-03/06)
- Exhaustiv: alle 66441 Strings bis Laenge 5 ueber Alphabet {', =, +, -, @, Tab, CR, a, Leerzeichen} plus Benannte: `_unguard_cell(_guard_cell(v)) == v` fuer jeden Wert, 0 Abweichungen.
- Explizit: `=x`, `+x`, `-x`, `@x`, Tab, CR sowie `'=x` -> `''=x` -> `'=x`, `''-x` -> `'''-x` -> `''-x`, `'abc` unveraendert.
- Roundtrip-Kette (Export -> load_rows -> strip_export_guard) byteweise gleich fuer Gegenpartei/Verwendungszweck/Notiz, inkl. Tab, CR, CRLF-Wert.
- REST-Kette (/api/import/detect + execute mit Vorlage "Eigener CSV-Export", 12 Varianten): new_count 0, skipped_count 12, DB-Inhalt unveraendert.

## DoD
Optionen, Kodierungen byteweise, Spalten, Formel-Injection ohne Betrag/Datum, Profil-CRUD/Standard/409, Importvorlage, JSON/MD-Regressionsfreiheit, Auth/Vertrag/Shared-Wiring: durch Tests und Code-Review belegt (decisions.md-Eintraege vorhanden, Router mit require_user, Vertragstest gruen).

## Laeufe
- pytest test_export_csv.py + test_export_profiles_api.py: 27 passed
- pytest tests/unit: 1430 passed, 1 skipped
- ruff check: ok; ruff format --check: 188 files formatted; mypy backend: no issues in 91 files
