# API-Konventionen

Verbindliche Konventionen der REST-API der Finanzuebersicht. Grundgeruest und
Guardrail stammen aus T-086; jeder weitere API-Task haelt sich daran.

## Grundlagen

- Alle fachlichen Routen liegen unter `/api`. Die zentrale Registrierung ist
  `backend/finue/api/routes.py`; Folge-Tasks ergaenzen dort je eine Zeile.
- Auth-Naht: jede `/api`-Route ausser `/api/health*` haengt an der Dependency
  `require_user` (`backend/finue/api/deps.py`). In M4 prueft sie noch nichts
  (der Owner kommt ohne Nachweis durch); mit gesetztem `auth_required` liefert
  sie 401. Die echte Anmeldung folgt mit N-SEC-04 in M10 (siehe
  `projects/finanzuebersicht/decisions.md`).
- Betraege gehen als `amount_cents` (ganzzahlig, mit Vorzeichen) plus `currency`
  (ISO 4217, dreistellig gross) ueber die Leitung. Datumswerte sind ISO
  `JJJJ-MM-TT`, Zeitstempel ISO 8601 mit Zeitzone.
- Kein rohes Daten-SQL (N-DB-01); Zugriffe laufen ueber SQLAlchemy.

## Fehlerformat

Jede Fehlerantwort hat die Form:

```json
{ "code": "not_found", "message": "Die angeforderte Ressource wurde nicht gefunden.", "details": {} }
```

- `code`: stabile englische Kennung (Code-Bezeichner), z. B. `not_found`,
  `validation_error`, `conflict`, `payload_too_large`, `unauthorized`,
  `method_not_allowed`, `domain_error`, `internal_error`.
- `message`: deutscher Klartext.
- `details`: Objekt mit maschinenlesbaren Zusatzangaben (`{}` wenn leer).

Statuszuordnung:

| Status | code | Ausloeser |
|---|---|---|
| 401 | `unauthorized` | fehlende Anmeldung (aktiv ab N-SEC-04/M10) |
| 404 | `not_found` | unbekannte Ressource oder Route |
| 405 | `method_not_allowed` | Methode an der Route nicht zulaessig |
| 409 | `conflict` | optimistisches Sperren, Version passt nicht (F-CON-02) |
| 413 | `payload_too_large` | Upload ueber der konfigurierten Grenze |
| 422 | `validation_error` | Schema-/Parameterfehler, deutsch uebersetzt |
| 422 | `domain_error` | Fachfehler aus der Service-Schicht |
| 500 | `internal_error` | unerwarteter Fehler, ohne interne Details (N-SEC-02) |

### 409 mit aktuellem Stand (F-CON-02)

Aendernde Anfragen senden die bekannte Version mit (`VersionedRequest.version`).
Passt sie nicht mehr, antwortet der Server mit 409 und dem aktuellen Stand:

```json
{
  "code": "conflict",
  "message": "Der Datensatz wurde zwischenzeitlich auf einem anderen Geraet geaendert.",
  "details": {},
  "current": { "id": 1, "name": "Giro", "version": 7 }
}
```

`current` ist ein DTO des aktuellen Stands. Die UI zeigt beide Staende und
laesst den Nutzer waehlen.

### Validierungsfehler (422)

```json
{
  "code": "validation_error",
  "message": "Die Anfrage ist ungueltig.",
  "details": { "errors": [ { "field": "limit", "message": "Feld ist erforderlich" } ] }
}
```

Feldmeldungen sind deutsch; der Feldpfad laesst das Ortspraefix (`body`,
`query`, …) weg.

## Paginierung (SPEC F-MCP-11)

Listenantworten nutzen den Umschlag:

```json
{ "items": [ ... ], "next_cursor": "o50" }
```

- `limit`: Standard 50, hoechstens 200 (`api_default_page_size`,
  `api_max_page_size` in `finue/config.py`). Ungueltige Werte sind 422.
- `next_cursor`: undurchsichtiger Cursor der naechsten Seite; `null` am Ende.
  Der Helfer steht in `backend/finue/api/pagination.py`. Die
  Schluesselwort-Paginierung der Buchungsliste folgt in T-093, nutzt aber
  denselben Umschlag.

## Batch-Aktionen (F-CON-03)

Batch-Aufrufe liefern je Eintrag ein Ergebnis; Konflikte brechen den Rest nicht
ab:

```json
{
  "results": [
    { "id": 1, "outcome": "geaendert" },
    { "id": 2, "outcome": "unveraendert" },
    { "id": 3, "outcome": "konflikt", "current": { "id": 3, "version": 9 }, "message": "..." },
    { "id": 4, "outcome": "fehler", "message": "Unbekanntes Konto" }
  ]
}
```

`outcome` ist einer von `geaendert`, `unveraendert`, `konflikt`, `fehler`
(F-CON-03). Die Modelle stehen in `backend/finue/api/conventions.py`. Ein Batch
hat hoechstens `api_max_batch_size` (Standard 1000) Eintraege.

## Aenderungsherkunft

Aenderungen ueber die REST-API tragen die Herkunft `ui` (`REST_ORIGIN` in
`backend/finue/api/conventions.py`, F-ABS-01/02). MCP nutzt `mcp`, der
Datei-Import `import` (siehe `finue/services/export_schema.py`).

## Konten und Gruppen (F-KTO)

Die Fachlogik liegt in `backend/finue/services/accounts.py`; die Router
`backend/finue/api/accounts.py` und `backend/finue/api/groups.py` uebersetzen
nur HTTP. Beide Router bringen ihr `prefix="/api"` und die Auth-Naht selbst mit.

### IBAN-Maskierung (F-KTO-01)

Die **Kontenliste** zeigt eine maskierte IBAN (`iban_masked`): Die ersten vier
Zeichen (Laenderkennung und Pruefziffern) und die letzten vier Zeichen bleiben
sichtbar, alle Zeichen dazwischen werden durch je ein `*` ersetzt; Leerzeichen
werden vorher entfernt. Beispiele: `DE89370400440532013000` →
`DE89**************3000`, `DE12` → `****`. Die **Detailansicht**
(`GET /api/accounts/{id}`) liefert zum Pflegen die volle `iban` (F-KTO-04).

### Gesamtsaldo und Fremdwaehrung (F-KTO-02)

Die Liste liefert oben den Gesamtsaldo mit seiner Zusammensetzung:

```json
{
  "items": [ ... ],
  "total": {
    "currency": "EUR",
    "amount_cents": 100000,
    "included": [ { "id": 1, "name": "Giro", "currency": "EUR", "amount_cents": 100000 } ],
    "excluded": [ { "id": 2, "name": "Darlehen", "currency": "EUR", "amount_cents": 500000 } ],
    "foreign": [ { "currency": "USD", "amount_cents": 25000, "account_ids": [3] } ]
  },
  "next_cursor": null
}
```

`total.amount_cents` summiert nur die einfliessenden Konten in der Buchwaehrung;
`excluded` sind die Konten mit `include_in_total=false`. Konten in einer anderen
Waehrung werden unter `foreign` **getrennt ausgewiesen und nicht addiert**
(Regel in `projects/finanzuebersicht/decisions.md`). Archivierte Konten zaehlen
nicht mit (F-KTO-06). Dieselbe Regel gilt fuer die Gruppensumme.

### Endpunkte

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/accounts` | Kontenliste mit Gesamtsaldo; `?archived=true` liefert nur archivierte (F-KTO-01/02/06) |
| POST | `/api/accounts` | Offline-Konto anlegen, `source=datei`, optionaler Ankersaldo (F-KTO-05b) |
| GET | `/api/accounts/{id}` | Kontodetails mit voller IBAN (F-KTO-04) |
| PATCH | `/api/accounts/{id}` | Details pflegen mit `version` (F-KTO-04, F-CON-02) |
| POST | `/api/accounts/{id}/archive` | Archivieren mit `version` (F-KTO-06) |
| POST | `/api/accounts/{id}/restore` | Wiederherstellen mit `version` (F-KTO-06) |
| GET | `/api/groups` | Gruppen mit Mitgliedern, Kontenzahl, Summe (F-KTO-03) |
| POST | `/api/groups` | Gruppe anlegen (F-KTO-03) |
| PUT | `/api/groups/order` | Neuordnung als ID-Liste `{"ids": [...]}` (F-KTO-03) |
| PATCH | `/api/groups/{id}` | Umbenennen mit `version` (F-KTO-03, F-CON-02) |
| DELETE | `/api/groups/{id}?version=n` | Loeschen; Konten bleiben mit `group_id=null` (F-KTO-03) |

`iban` wird beim Anlegen und Pflegen per Modulo-97-Pruefziffer validiert
(F-KTO-04); `source` ist beim Offline-Konto fest `datei`. Icon-Verweise zeigen
auf den mit F-KTO-11/T-090 folgenden Endpunkt `/api/accounts/{id}/icon`.

## Grenzen (config)

Zentral in `backend/finue/config.py`:

- `api_default_page_size` = 50, `api_max_page_size` = 200 (Listen).
- `api_max_batch_size` = 1000 (Batch-Aktionen).
- `api_max_upload_bytes` = 50 MiB (Import-Datei; der Icon-Upload mit 512 KB
  aus F-KTO-11 wird in T-090 separat begrenzt).

## Ereignisse (F-CON-04)

Aenderungen meldet der In-Prozess-Ereignisbus
(`backend/finue/services/events.py`). Ein Ereignis besteht aus Typ und IDs
(z. B. `transaction.updated` mit `transaction=42`) und traegt **keine**
Geheimnisse und keine Freitextnutzlast (N-SEC-02). Der Bus liegt im
Service-Container (`app.state.services`); die SSE-Route `/api/events` liefert
ihn in T-101 aus.

## Vertragstest

`tests/unit/test_api_contract.py` ist die Guardrail: jede schreibende Route
braucht einen Eintrag in der Vertragstabelle mit Art
(`version`/`setzen`/`anlegen`/`aktion`) und Begruendung, jede lesende
Listenroute den Parameter `limit` (die Pflicht gilt nur fuer die GET-Route, ein
POST auf demselben Pfad nicht), jede `/api`-Route die Auth-Naht. Unbekannte
Routen und veraltete Eintraege machen den Test rot. Jeder API-Task erweitert die
Tabelle.
