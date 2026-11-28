# T-105 Build-Evidenz: Roundtrip-Block erweitert

Task: T-105 (M4 Welle 7, Risiko medium). Anforderungen: F-EXP-04, F-EXP-06,
F-EXP-07, F-ABS-06, F-DUP-04, F-DUP-05. Branch `m4/develop`.

## Aenderungen

- `scripts/roundtrip_check.py` (erweitert): vier Bloecke.
  - Block 0 (unveraendert): bisheriger Import-Roundtrip der JSON-v1-Fixture.
  - Block 1 (neu, Mehrkonto-JSON): Bestand mit 3 Konten (2 in einer Gruppe),
    Notizen, Kennzeichen samt Herkunft, externen Referenzen und einem
    zusammengefuehrten Paar -> `run_export` JSON v1; Re-Import je Konto = 0 neu
    und unveraenderter Stand (Multimengen-Feldvergleich vor/nach); Import in
    frische DB = gleicher Stand; Gruppen-Export enthaelt genau die Konten der
    Gruppe; Fake-Sync (`reconcile_multiset`) = 0 neu und Kennzeichen erhalten
    (F-ABS-06).
  - Block 2 (neu, CSV): Standardprofil-Export -> Import mit Vorlage
    "Eigener CSV-Export" (`strip_export_guard`) = 0 neu (F-EXP-06).
  - Block 3 (neu, Schema-Versionen): jede `docs/schemas/export-v<N>.schema.json`
    mit ihrer Testdatei `export-v<N>.example.json` importierbar; neuere Version
    klar abgelehnt (F-EXP-07).
- `scripts/validate.py`: `check_roundtrip_import` nutzt das erweiterte Skript;
  `ROUNDTRIP_CHECK` zusaetzlich in `STAGE_M4` eingetragen (m4 kumulativ).

## Verifikationsplan (alle Exit 0)

| Befehl | Ergebnis |
|---|---|
| `python scripts/validate.py roundtrip` | `ROUNDTRIP_OK fixture=5 mehrkonto_reimport=5 mehrkonto_frisch=5 csv=2 schema=1` |
| `uv run ruff check scripts/roundtrip_check.py scripts/validate.py` | All checks passed |
| `uv run ruff format --check scripts/roundtrip_check.py scripts/validate.py` | 2 files already formatted |
| `uv run mypy backend` | Success: no issues found in 101 source files |

Zusatz:

- `uv run pytest tests/unit/test_validate_stage_m4.py tests/unit/test_export_csv.py
  tests/unit/test_export_json_md.py tests/unit/test_import_json_own_format.py -q`
  = 60 passed.
- Mutationstest: im CSV-Block `strip_export_guard` uebersprungen ->
  `ROUNDTRIP_FEHLER: csv: CSV-Re-Import skipped_count=1, erwartet 2`, Exit 1
  (der Check ist gegen eine fehlende Formel-Ruecknahme empfindlich).

Planweiter `uv run ruff check backend tests scripts` / `ruff format --check` ist
rot **nur** durch fremde, noch nicht committete Parallelarbeit
(`tests/e2e_api/test_scenario_a.py`, T-106/T-113); die T-105-Dateien sind sauber.

## Bewusst nicht geaendert

Keine Import-/Export-/Batch-Fachlogik, keine Migration, keine neuen Routen,
keine echten Zugangsdaten, keine externen Schreibaktionen.
