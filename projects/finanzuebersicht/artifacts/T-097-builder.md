# T-097 Builder-Evidenz: Export-Service JSON/Markdown (F-EXP-01/02/04/05)

- Branch: `m4/develop`, Commit: `b63d57a` (M4 T-097: Export-Service fuer JSON und Markdown).
- Umfang: ausschliesslich Export (JSON und Markdown). Keine CSV/Profile aus T-098.
- Risiko: medium. `task-done` bewusst nicht vorweggenommen; unabhaengige Verifier-Pruefung folgt.

## Artefakte

- `backend/finue/services/export.py` - Export-Service (Umfang, Filter, Dateiname, JSON, Markdown, Grenze).
- `backend/finue/api/export.py` - `POST /api/export` mit `Content-Disposition`, Markdown als `text/markdown`
  und `text/plain` fuer die Zwischenablage.
- `backend/finue/config.py` - neue Grenze `export_max_rows` (100000).
- `backend/finue/api/routes.py` - Registrierung des Export-Routers (nur eigener Hunk).
- `tests/unit/test_export_json_md.py` - 14 Tests (Dateiname, Schema, Re-Import, Markdown-Golden, API).
- `tests/unit/test_api_contract.py` - Vertragseintrag `POST /api/export` (nur eigener Hunk).
- `docs/api.md`, `docs/export-format.md` - Endpunkt- und Formatdoku.

## Verifikationsplan (am Arbeitsbaum-Tip, gruen)

| Befehl | Ergebnis |
|---|---|
| `uv run pytest tests/unit/test_export_json_md.py -q` | 14 passed |
| `uv run ruff check backend tests scripts` | All checks passed |
| `uv run ruff format --check backend tests scripts` | 180 files already formatted |
| `uv run mypy backend` | Success: no issues found in 87 source files |

Zusaetzlich: `pytest tests/unit/test_export_json_md.py tests/unit/test_api_contract.py tests/unit/test_search_api.py`
= 47 passed.

## Abdeckung je Anforderung

- **F-EXP-01**: Umfang `all`/`account`/`group`/`search` (ueber den gemeinsamen `TransactionQuery` aus T-092) und
  `selection` (`transaction_ids`); Grenze `export_max_rows` -> 413 `payload_too_large`.
- **F-EXP-02**: Zeitraum/Filter der Ansicht; Dateiname `slug_von_bis.ext` in ASCII (Umlaute transkribiert),
  bei `alle` Zeitraum aus Min/Max der exportierten Buchungen.
- **F-EXP-04**: JSON nach Schema v1; Feldmengen gegen `docs/schemas/export-v1.schema.json` geprueft; Kontostammdaten
  ohne Zugangsdaten; Re-Import in frische DB ergibt denselben Stand (Signaturvergleich inkl. Kennzeichen-Herkunft).
- **F-EXP-05**: Markdown-Kopf (Konto/Gruppe, Zeitraum, Anzahl, Summe), Tabelle (Datum, optional Konto,
  Gegenpartei, Verwendungszweck, Betrag, Notiz), Summenzeile, deutsches Zahlen-/Datumsformat, Maskierung von
  `|`, Backslash und Zeilenumbruch (Golden-File).
- **F-DUP-04**: zusammengefuehrte Buchungen sind in beiden Formaten ausgeschlossen.

## Hinweise

- **Shared-Files**: Parallele Builder (T-093/T-100/T-108) aendern denselben Arbeitsbaum. `routes.py`, `config.py`
  und `test_api_contract.py` wurden deshalb nur hunkweise gestaged (Backup -> eigene Fassung -> `git add` ->
  fremde Fassung zurueck). Der Commit enthaelt keine fremden Hunks.
- **Fund (Improve-Eintrag)**: Der Bestand fuehrt `occurrence_index` 0-basiert (`duplicate._load_existing_state`,
  Demo), das Schema v1 verlangt mindestens 1. Der Export gibt ihn 1-basiert aus; der Re-Import vergibt ihn ohnehin
  neu (Multimengen-Abgleich), der Datenstand bleibt verlustfrei. Aufloesung des Widerspruchs ist als Improve-Kandidat
  notiert (Owner/Migration).
