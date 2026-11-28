# T-107 M4-Abnahme und Aufraeumen (M4, G-005)

Stand: 2026-11-28. Risiko medium; unabhaengige `verifier`-Pruefung folgt auf dem
committeten Stand. Kein Neubau, keine echten Bankzugriffe, keine Secrets, keine
externen Schreibaktionen, kein Push.

## Ausgangslage

Letzter M4-Task (Welle 8, solo). Abhaengigkeiten T-084..T-106 alle `done`. Claim
gezielt per `python hx.py task-set finanzuebersicht T-107 running` (der generische
`task-claim` traf in dieser Welle wiederholt die falsche ID).

## Umfang (nur Abnahme und Aufraeumen)

- `tests/unit/test_requirement_refs.py` um die M4-Abnahme-IDs erweitert
  (F-KTO-01..07, F-KTO-10, F-KTO-11, F-BUC-01..06, F-DUP-05, F-ABS-01..06,
  F-SUC-01..07, F-EXP-01..07, F-CON-01..06, N-PERF-01). Jede ID ist bereits in
  mindestens einer echten Testdatei unter `tests/` referenziert (Vorpruefung), der
  erweiterte Meta-Test ist gruen.
- Keine Aenderung an `scripts/validate.py`, `docs/api.md`, `decisions.md` noetig
  (bereits vollstaendig bzw. unveraendert gruen).
- Harness-Aufraeumen: ausstehende Status-/Evidenzdateien der Welle committet;
  die auf `m4/develop` noch nicht getrackte T-093-Testdatei
  `tests/unit/test_transaction_list.py` wiederhergestellt.

## Gesamtabnahme: `python scripts/validate.py m4` (Exit 0, bestanden)

Manueller Einzellauf: `python scripts/validate.py m4` = Exit 0, `Ergebnis: bestanden`
(Gesamtlaufzeit gemessen 311.3 s). Der Lauf fuehrt die m0..m3-Kette plus die vier
M4-Bloecke roundtrip, perf, concurrency und api-e2e aus.

### Harness-Timeout und Zerlegung des Verifikationsplans

`python hx.py task-done` fuehrt den Verifikationsplan je Befehl mit
`VERIFY_TIMEOUT = 300 s` (hart, im zentralen Harness `harness/hx.py`) aus. Der
monolithische Befehl `python scripts/validate.py m4` dauert ~311 s und passt damit
nicht in das Limit; `task-done` wuerde mit Exit 124 (timeout) fehlschlagen und den
Task zurueckstufen. Der Verifikationsplan von T-107 wurde deshalb auf die
**aequivalente Blockfolge** gesetzt (identische Abdeckung, jeder Befehl < 300 s),
dasselbe Muster wie der T-106-Plan:

```
python scripts/validate.py m3          # m0..m3 (lint, types, unit, smoke, roundtrip, readonly, integration)
python scripts/validate.py roundtrip   # M4-Roundtrip (erweitert)
python scripts/validate.py perf        # M4-Perf 100.000 Buchungen
python scripts/validate.py concurrency # M4-Concurrency 3x
python scripts/validate.py api-e2e     # M4-API-E2E 3x
```

Der Einzellauf `validate.py m4` bleibt als direkte Evidenz oben dokumentiert; die
Zerlegung aendert die Abdeckung nicht.

| Stufe | Ergebnis |
|---|---|
| m0 | Pflichtdateien, Skill-Frontmatter, AGENTS-Abschnitte, settings-Rechte, plan.md, Git, hx.py status - alle OK |
| m1 lint | `All checks passed!`; `218 files already formatted`; alle `type: ignore` begruendet |
| m1 types | `Success: no issues found in 101 source files` |
| m1 unit | `1606 passed, 1 skipped` (155.16 s) |
| m1 smoke | `1 passed, 17 deselected` |
| m2/m4 roundtrip | `ROUNDTRIP_OK fixture=5 mehrkonto_reimport=5 mehrkonto_frisch=5 csv=2 schema=1` |
| m3 Read-only-Guard | `FINTS_READONLY_OK dateien=101 verbotene_namen=14` |
| m3 integration | `17 gesammelt; 17 passed, 1 deselected` |
| m4 perf (N-PERF-01, F-BUC-06) | suche_1=195.4 ms, suche_2=193.0 ms, betrag=27.5 ms, liste_1=5.6 ms, liste_50=4.0 ms, gruppe=201.0 ms, http_suche=203.5 ms, http_liste=10.7 ms (alle Median < 500 ms) |
| m4 concurrency (F-CON-01..05, N-DB-04) | `3 Laeufe gruen (11 passed)` |
| m4 api-e2e (T-106) | `2 gesammelt; 3 Laeufe gruen in 74.4s` |

## Review-Checkliste AGENTS.md

- Kein rohes SQL (N-DB-01): nur ORM/Core-SQLAlchemy; `PRAGMA ...` in `db/engine.py`
  und in Migrationsdateien ist Verbindungs-/DDL-Konfiguration, kein Daten-SQL.
- Vertragstest `tests/unit/test_api_contract.py`: jede registrierte `/api`-Route
  (`test_alle_api_routen_sind_registriert`), jede schreibende Route mit Art
  (version|setzen|anlegen|aktion) und Begruendung
  (`test_schreibende_routen_haben_art_und_begruendung`), jede Listenroute `limit`,
  jede Route an `require_user`. Alle gruen im m4-Sammlauf.
- Keine Secrets: kein hartkodiertes Geheimnis; `secrets` nur als stdlib fuer
  Upload-Token; PIN Fernel-verschluesselt (`PinCipher`) bzw. nur fluechtig,
  `token_hash` verlaesst das Backend nicht (N-SEC-01..03).
- Keine Kategorien-/Ueberweisungsreste: CSV-Erkennung fuehrt `kategorie`/`category`
  nur als Erkennungs-Stichwort (`_HEADER_KEYWORDS`), `services/mapping.py` lehnt
  `category` als Zielfeld ab; `parse_pain_transfer` liest nur Dauerauftraege
  (rein lesend); Frontend ohne Kategorie/Budget/Schlagwort.
- Keine schreibenden FinTS-Aufrufe (Read-only-Guard gruen, D2).
- OpenAPI-Schema generierbar ohne Serverstart: `create_app().openapi()` = 55 Pfade.
- `docs/api.md`: Routen, Fehlerformat (404/409 mit `current`/422 mit `details.field`/
  413), Grenzen und Ereignisse dokumentiert.
- `decisions.md` traegt die M4-Entscheidungen (T-085..T-098, T-099, T-100, T-101,
  T-103, T-104, T-105, T-110); T-084 (validate-Geruest) und T-106 (api-e2e-Budget)
  sind Werkzeug-Tasks, ihre Punkte stehen als erledigte Improve-Eintraege in
  `improve.md`.
- Keine offenen `running` Tasks ausser T-107 selbst; `pending=0`, `blocked=0`.

## Harness-Konsistenz

- 17 Task-Dateien (`status: running` -> `done`, `attempts`, `evidence`, Log),
  `status.md`, `queues.md` und `runs/events.jsonl` entsprechen dem Laufzeitstand
  und werden committet.
- Jede in einer Task-Datei referenzierte Evidenzdatei existiert auf der Platte
  (Pruefung ueber alle `evidence: [...]`-Zeilen).
- `tests/unit/test_transaction_list.py` (T-093, F-BUC-01..06) fehlte auf
  `m4/develop` (nur im Verifier-Branch `m4/t093-verify`, Commit `89c866c`);
  die Arbeitsbaumdatei ist inhaltlich identisch (439 Zeilen, nur Zeilenenden
  CRLF vs. LF) und wird committet, damit die Abnahme reproduzierbar ist.

## Offen

- Unabhaengige `verifier`-Pruefung (Review-Checkliste AGENTS.md, Integration und
  Querschnitt) auf dem committeten Stand.
- Owner-Eingaben unveraendert: Finanzblick-CSV-Beispiel fuers Standardprofil,
  Datei-Beispiele N26/Easybank/PayPal, FinTS-Produkt-ID/Live-Test.
- Merge nach `main` und `docker push` bleiben eigene approvalspflichtige Schritte.
