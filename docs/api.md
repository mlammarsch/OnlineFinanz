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
  Der Helfer steht in `backend/finue/api/pagination.py`. Die Buchungsliste
  (T-093) nutzt denselben Umschlag und dieselbe `limit`-Pruefung
  (`resolve_limit`), traegt aber einen Keyset-Cursor aus Buchungstag und ID
  (siehe Abschnitt "Buchungsliste").

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
| DELETE | `/api/accounts/{id}?version=n` | Konto loeschen mit vollstaendiger Bereinigung (F-KTO-07) |
| GET | `/api/groups` | Gruppen mit Mitgliedern, Kontenzahl, Summe (F-KTO-03) |
| POST | `/api/groups` | Gruppe anlegen (F-KTO-03) |
| PUT | `/api/groups/order` | Neuordnung als ID-Liste `{"ids": [...]}` (F-KTO-03) |
| PATCH | `/api/groups/{id}` | Umbenennen mit `version` (F-KTO-03, F-CON-02) |
| DELETE | `/api/groups/{id}?version=n` | Loeschen; Konten bleiben mit `group_id=null` (F-KTO-03) |
| GET | `/api/accounts/{id}/reconciliation` | Abstimmungsuebersicht eines Kontos (F-ABS-03) |
| GET | `/api/groups/{id}/reconciliation` | Abstimmungsuebersicht einer Gruppe, Fremdwaehrung getrennt (F-ABS-03) |
| GET | `/api/accounts/{id}/transactions` | Buchungsliste eines Kontos mit Vormerkungen und Tagessalden (F-BUC-01..04) |
| GET | `/api/groups/{id}/transactions` | Buchungsliste einer Gruppe mit Kontokennzeichnung je Zeile (F-BUC-05) |
| GET | `/api/banks` | Banken-Stammdaten auflisten (F-KTO-10) |
| POST | `/api/banks` | Bank anlegen; keine Vorbelegung ohne belegte Quelle (F-KTO-10) |
| GET | `/api/banks/{id}` | Banken-Stammdaten ansehen (F-KTO-10) |
| PATCH | `/api/banks/{id}` | Stammdaten pflegen mit `version` (F-KTO-10, F-CON-02) |
| DELETE | `/api/banks/{id}?version=n` | Bank loeschen; nur ohne Bankzugaenge (F-KTO-10) |
| GET | `/api/accounts/{id}/icon` | Konto-Icon als PNG mit `ETag` (F-KTO-11) |
| PUT | `/api/accounts/{id}/icon?version=n` | Konto-Icon hochladen (PNG/JPEG/WebP) (F-KTO-11) |
| DELETE | `/api/accounts/{id}/icon?version=n` | Konto-Icon entfernen (F-KTO-11) |
| GET | `/api/banks/{id}/icon` | Bank-Icon als PNG mit `ETag` (F-KTO-10/11) |
| PUT | `/api/banks/{id}/icon?version=n` | Bank-Icon hochladen (PNG/JPEG/WebP) (F-KTO-11) |
| DELETE | `/api/banks/{id}/icon?version=n` | Bank-Icon entfernen (F-KTO-11) |

`iban` wird beim Anlegen und Pflegen per Modulo-97-Pruefziffer validiert
(F-KTO-04); `source` ist beim Offline-Konto fest `datei`. Icon-Verweise
(`icon_url`) zeigen auf `/api/accounts/{id}/icon` bzw. `/api/banks/{id}/icon`
(F-KTO-11).

### Abstimmungsuebersicht (F-ABS-03)

Die Fachlogik liegt in `backend/finue/services/reconciliation.py` (MCP nutzt
dieselbe Schicht, D10); der Router `backend/finue/api/reconciliation.py`
uebersetzt nur HTTP. `GET /api/accounts/{id}/reconciliation` liefert:

```json
{
  "account_id": 1,
  "currency": "EUR",
  "opening_balance_cents": 4000,
  "opening_balance_derived": true,
  "reconciled_balance_cents": 8000,
  "unreconciled_count": 1,
  "unreconciled_sum_cents": 2000,
  "balance_cents": 10000,
  "difference_cents": 2000
}
```

Regeln (Herleitung in `projects/finanzuebersicht/decisions.md`):

- `reconciled_balance_cents` = massgeblicher Anfangssaldo plus Summe der
  abgestimmten, gebuchten und **nicht** zusammengefuehrten Buchungen.
- `unreconciled_count`/`unreconciled_sum_cents` = Anzahl und Summe der
  gebuchten, nicht zusammengefuehrten, nicht abgestimmten Buchungen.
- Vorgemerkte Buchungen (`status='vorgemerkt'`) und zusammengefuehrte
  Buchungen (`merged=true`, F-DUP-04) zaehlen in **keiner** Kennzahl mit
  (F-ABS-01/F-ABS-03).
- `difference_cents` = `balance_cents` minus `reconciled_balance_cents`; fehlt
  der Banksaldo (`balance_cents=null`), ist die Differenz `null`.
- `opening_balance_derived` zeigt an, ob der Anfangssaldo aus dem Banksaldo
  abgeleitet wurde (FinTS-Konto ohne Ankersaldo).

`GET /api/groups/{id}/reconciliation` liefert dieselben Felder plus
`group_id`, `member_count` und `foreign` (je Fremdwaehrung ein Aggregat wie
oben). Es zaehlen nur die aktiven Mitglieder; die Summe fuehrt nur die
Buchwaehrung, andere Waehrungen werden unter `foreign` getrennt ausgewiesen und
nicht addiert (T-088-Regel). Der Banksaldo eines Aggregats ist `null`, wenn
mindestens ein Mitglied keinen Banksaldo fuehrt.

### Konto loeschen (F-KTO-07)

`DELETE /api/accounts/{id}?version=n` entfernt das Konto mit vollstaendiger
Bereinigung in **einer** Transaktion: Buchungen samt Notizen und Vormerkungen,
Import-Laeufe mit ihren Fehlern, Termin-/Dauerauftraege, Dublettenentscheidungen
des Kontos und die kontogebundenen Audit-Eintraege. Kontogebundene
Importvorlagen (F-IMP-05) bleiben erhalten und verlieren nur `account_id`
(`null` → kontounabhaengig). Der Bankzugang bleibt bestehen, auch wenn er danach
kein Konto mehr hat: Zugangsdaten werden nie still geloescht. Erfolg ist
`204 No Content` ohne Koerper; das Ereignis `account.deleted` traegt die
Konto-ID. Den vorherigen, verlustfreien JSON-Export (F-EXP-04) bietet die UI im
Bestaetigungsdialog an und prueft ihn im API-E2E (T-106).

Ein veralteter `version`-Wert liefert 409 mit dem aktuellen Konto (`current`).
Laeuft fuer den Bankzugang des Kontos gerade ein Sync, antwortet der Server
ebenfalls 409 mit deutschem Klartext (F-SYN-11); das Konto bleibt unveraendert.
Die Entscheidung zu Audit-Eintraegen und Bankzugang steht in
`projects/finanzuebersicht/decisions.md`.

## Banken-Stammdaten und Icons (F-KTO-10/11)

Die Fachlogik liegt in `backend/finue/services/banks.py` (Stammdaten) und
`backend/finue/services/icons.py` (Verarbeitung und Speicherung); die Router
`backend/finue/api/banks.py` und `backend/finue/api/icons.py` uebersetzen nur
HTTP. Beide bringen ihr `prefix="/api"` und die Auth-Naht selbst mit.

### Banken-Stammdaten (F-KTO-10)

`blz` besteht aus genau acht Ziffern. `fints_url` ist optional und **nur**
per HTTPS erlaubt (N-SEC-05); `website` ist optional und muss ein http(s)-Ziel
mit Host sein. Aenderungen tragen `version` und liefern bei veraltetem Stand 409
mit `current` (F-CON-02). **Es werden keine Banken vorbelegt**: laut SPEC sind nur
Endpunkte mit belegter Quelle zulaessig, und eine solche liegt nicht vor
(`projects/finanzuebersicht/decisions.md`). `DELETE /api/banks/{id}?version=n`
loescht nur, wenn die Bank **keinen Bankzugang** hat; sonst 409 mit deutschem
Klartext und `details.access_count`. Ereignis: `bank.changed` (F-CON-04).

```json
{ "id": 1, "blz": "10000000", "name": "Beispielbank", "website": null,
  "fints_url": "https://fints.example", "has_icon": false, "icon_url": null, "version": 1 }
```

### Icon-Upload (F-KTO-11)

`PUT /api/accounts/{id}/icon?version=n` bzw. `/api/banks/{id}/icon?version=n`
liest die rohen Bildbytes aus dem Anfragekoerper (der deklarierte Content-Type
ist unerheblich). Regeln:

- erlaubt sind PNG, JPEG und WebP; **SVG ist ausgeschlossen** (Skript-Risiko);
- hoechstens **512 KB**, geprueft **vor** dem Dekodieren → sonst 413
  `payload_too_large`;
- Typpruefung am **Inhalt** (Signatur plus Pillow-`verify`); Fremdinhalt → 422
  `domain_error` im Klartext;
- Schutz gegen Dekompressionsbomben ueber eine Pixelgrenze (`MAX_PIXELS`);
- Skalierung auf **128 x 128 px** mit erhaltenem Seitenverhaeltnis, Speicherung
  als **PNG** in `account.icon`/`bank.icon` samt `icon_mime` (also im Backup, kein
  Zusatzverzeichnis im Volume).

`GET` liefert das gespeicherte PNG (`image/png`) mit `ETag`; passt ein
`If-None-Match`, antwortet der Server `304 Not Modified`. Ohne gespeichertes Icon
liefert `GET` 404. Upload und Loeschen pruefen `version` (F-CON-02) und melden
`account.changed` bzw. `bank.changed` (F-CON-04). Der Web-Abruf von Icons
(F-KTO-08) gehoert zu M7 und findet hier nicht statt (N-SEC-05).

## Buchungsliste (F-BUC-01..06)

Die Fachlogik liegt in `backend/finue/services/transaction_list.py` (MCP nutzt
dieselbe Schicht, D10); der Router `backend/finue/api/transaction_list.py`
uebersetzt nur HTTP. Filter, Zeitraum und Trefferlogik stammen aus
`services/search.py` (T-092); die Tagesgruppen sind immer nach Buchungstag
absteigend (F-BUC-03). Parameter wie bei der Suche: `q`, `period`
(Standard `365`), `from`/`to`, `checked`, `reconciled`, `with_note`, `limit`,
`cursor`. Sortierparameter gibt es nicht, weil die Tagesgruppierung den
Buchungstag absteigend vorgibt.

```json
{
  "head": {
    "scope": "account",
    "id": 1,
    "name": "Girokonto",
    "currency": "EUR",
    "balance_cents": 109500,
    "icon_url": "/api/accounts/1/icon",
    "account_count": 1
  },
  "pending": {
    "items": [],
    "sum_cents": -700,
    "total_count": 1,
    "balance_with_pending_cents": 108800
  },
  "days": [
    {
      "day": "2026-01-12",
      "sum_cents": 2000,
      "balance_end_cents": 109500,
      "transactions": [
        {
          "id": 42,
          "account_id": 1,
          "account_name": "Girokonto",
          "status": "gebucht",
          "origin": "csv",
          "counterparty": "Max Mustermann",
          "purpose": "Miete Januar",
          "amount_cents": 2000,
          "currency": "EUR",
          "has_note": false,
          "checked": false,
          "reconciled": false,
          "version": 1
        }
      ]
    }
  ],
  "next_cursor": "kMjAyNi0wMS0xMjo0Mg"
}
```

Regeln (Herleitung in `projects/finanzuebersicht/decisions.md`):

- `head.balance_cents` ist der aktuelle Saldo (F-BUC-01). FinTS-Konten fuehren
  den uebernommenen Banksaldo; Datei-Konten bilden ihn aus dem Ankersaldo plus
  den gebuchten Betraegen (Ankerregel T-095).
- `pending` (F-BUC-02) enthaelt die vorgemerkten Buchungen mit `sum_cents` und
  dem `balance_with_pending_cents`; der Bereich erscheint nur auf der ersten
  Seite (ohne `cursor`). `total_count` zaehlt alle (gefilterten) Vormerkungen,
  die Liste ist auf 200 Zeilen begrenzt.
- `days` (F-BUC-03) sind nach Buchungstag absteigend gruppiert. `sum_cents` ist
  die Tagessumme der auf der Seite gezeigten Zeilen; `balance_end_cents` ist der
  Saldo am Tagesende und wird rueckwaerts vom aktuellen Saldo berechnet.
  Tagessalden beruecksichtigen **alle** gebuchten, nicht zusammengefuehrten
  Buchungen (auch von einem Filter ausgeblendete) und bleiben damit bei aktivem
  Filter korrekt; zusammengefuehrte Buchungen fehlen immer (F-DUP-04).
- Jede Zeile traegt `id`, `status`, `origin` (Quelle), `counterparty`,
  vollstaendigen `purpose`, `amount_cents`, `has_note` (Notiz-Marker),
  `checked`, `reconciled` und `version` (F-BUC-04).
- Gruppenansicht (F-BUC-05): Zeilen aller aktiven Mitglieder mit
  `account_id`/`account_name` als Kontokennzeichnung. Der Tagessaldo summiert
  nur die Buchwaehrung (Standard EUR, T-088-Regel); Konten anderer Waehrung
  erscheinen als Zeilen, zaehlen aber nicht in den Tagessaldo.
- Paginierung (F-BUC-06) ist Keyset: `next_cursor` traegt Buchungstag und ID;
  der Einstiegs-Tagessaldo einer Folgeseite kommt aus einem SQL-Aggregat ueber
  die juengeren Buchungen, nicht aus einem Python-Vollscan. Ein Buchungstag
  kann sich ueber Seiten erstrecken; der Tagessaldo bleibt dann gleich.
  Ungueltige Cursor und `limit`-Werte liefern 422 (F-CON-02, SPEC F-MCP-11).

## Einzelbuchung: Detail, Kennzeichen, Notiz und Referenz (F-DET-01, F-ABS-01/02/05, F-CON-01/02)

Die Fachlogik liegt in `backend/finue/services/transaction_edit.py` (MCP nutzt
dieselbe Schicht ueber den Parameter `origin`, D10); der Router
`backend/finue/api/transactions.py` uebersetzt nur HTTP.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/transactions/{id}` | Einzelbuchung mit allen Feldern fuer den Detailbereich (F-DET-01) |
| PUT | `/api/transactions/{id}/checked` | "geprueft" setzen (F-ABS-01/02) |
| PUT | `/api/transactions/{id}/reconciled` | "abgestimmt" setzen (F-ABS-01/02) |
| PUT | `/api/transactions/{id}/note` | Notiz ersetzen mit Versionspruefung (F-CON-02, F-ABS-05) |
| POST | `/api/transactions/{id}/note/append` | Notiz atomar anhaengen (F-CON-01, F-ABS-05) |
| PUT | `/api/transactions/{id}/external-ref` | externe Referenz setzen mit Versionspruefung (F-ABS-05, F-CON-02) |
| POST | `/api/transactions/batch` | Batch-Aktion auf eine Auswahl mit Ergebnis je Buchung (F-CON-03, F-ABS-02) |

```json
{
  "id": 42,
  "account_id": 1,
  "account_name": "Girokonto",
  "status": "gebucht",
  "origin": "csv",
  "counterparty": "REWE Markt",
  "counterparty_iban": "DE02120300000000202051",
  "purpose": "Wocheneinkauf",
  "amount_cents": -4883,
  "currency": "EUR",
  "booking_date": "2026-01-10",
  "value_date": "2026-01-09",
  "note": null,
  "checked": true,
  "checked_at": "2026-01-15T09:00:00Z",
  "checked_origin": "ui",
  "reconciled": false,
  "reconciled_at": null,
  "reconciled_origin": null,
  "external_ref": null,
  "merged": false,
  "version": 2,
  "import_run": {
    "id": 7,
    "kind": "csv",
    "status": "erfolgreich",
    "started_at": "2026-01-10T18:00:00Z",
    "finished_at": "2026-01-10T18:00:01Z"
  }
}
```

Regeln (Herleitung in `projects/finanzuebersicht/decisions.md`):

- `origin` ist die Quelle der Buchung (FinTS `fints`, CSV `csv`, JSON `json`);
  `import_run` ist der juengste Import-Lauf des Kontos mit passender Art
  (FinTS -> `sync`, CSV -> `csv`, JSON -> `json`) oder `null`. Der Bestand
  fuehrt (Stand T-108) noch keine direkte Verknuepfung Buchung -> Lauf.
- `checked_at`/`checked_origin` und `reconciled_at`/`reconciled_origin` sind
  Zeitpunkt und Herkunft der letzten Aenderung des jeweiligen Kennzeichens
  (F-ABS-01); die Herkunft ist `ui` (REST), `mcp` oder `import`.
- Der Body der Kennzeichen-Routen ist `{ "value": true|false, "version": n }`.
  `version` ist optional: mitgesendet prueft der Server optimistisch und liefert
  bei einem veralteten Stand 409 mit dem aktuellen Stand im Feld `current`
  (F-CON-02); ohne `version` gilt reine Setz-Semantik.
- Die Kennzeichen werden **gesetzt**, nicht umgeschaltet (F-CON-01): Ein
  bereits gesetzter Wert liefert denselben Stand ohne Versionssprung, ohne
  neuen Zeitstempel, ohne Audit-Eintrag und ohne Ereignis. Eine echte Aenderung
  hebt `version` um 1, schreibt einen Audit-Eintrag (`source` = Herkunft,
  F-MCP-09) und meldet `transaction.changed`.
- Vorgemerkte Buchungen lassen sich **nicht** abstimmen: `PUT .../reconciled`
  mit `value=true` liefert 422 `domain_error` mit Klartext (F-ABS-01). Das
  "geprueft"-Kennzeichen ist auch bei Vormerkungen setzbar.
- Notiz **ersetzen** (`PUT .../note`): Body `{ "text": "..." | null,
  "version": n }`. `version` ist **Pflicht** (ersetzende Aenderung, F-CON-02);
  ein veralteter Stand liefert 409 mit `current`. `null` oder `""` loeschen die
  Notiz. Derselbe Text aendert nichts (kein Versionssprung, kein Audit).
- Notiz **anhaengen** (`POST .../note/append`): Body `{ "text": "..." }`, ohne
  `version`. Das Anhaengen ist eine **atomare** Operation: der Server haengt per
  **einzelner** UPDATE-Anweisung an, nicht per Read-Modify-Write (F-CON-01);
  zwei gleichzeitige Aufrufe verlieren dadurch keinen Text. Trennzeichen ist ein
  **Zeilenumbruch**; ein leerer oder reiner Leerraum-Text liefert 422
  `validation_error`.
- Externe **Referenz** setzen (`PUT .../external-ref`): Body `{ "value": "..."
  | null, "version": n }`, `version` Pflicht (F-CON-02). `null`/`""` loeschen;
  mehr als 255 Zeichen liefern 422 `domain_error` (F-ABS-05).
- Notiz und externe Referenz sind Teil der normalisierten Suchspalte
  (F-SUC-01, F-ABS-05): jede Aenderung baut `search_text` neu. Jede echte
  Aenderung meldet `transaction.changed` (IDs `account`, `transaction`) und
  schreibt einen Audit-Eintrag (`source` = Herkunft, F-MCP-09).

### Batch-Aktion auf eine Auswahl (F-CON-03, F-ABS-02)

`POST /api/transactions/batch` wendet **eine** Aktion auf eine Buchungsauswahl an
und liefert **je Buchung** ein Ergebnis; ein Konflikt oder Fehler eines Eintrags
bricht den Rest nicht ab (F-CON-03). Der Aufruf nutzt je Eintrag genau den Kern
der Einzelrouten (keine zweite Schreibpfad-Logik).

Body:

- `ids`: Liste der Buchungs-IDs (mindestens eine, keine Duplikate; die
  Ergebnisfolge entspricht der Reihenfolge der Anfrage). Hoechstens
  `api_max_batch_size` (Standard 1000) Eintraege.
- `action`: `checked`, `reconciled`, `note_set` oder `note_append`
  (F-ABS-02, F-SEL-03).
- `value`: Zielwert passend zur Aktion — `checked`/`reconciled`: `true|false`;
  `note_set`: Text oder `null` (loescht die Notiz); `note_append`: nichtleerer
  Text.
- `versions` (optional): Abbildung Buchungs-ID → bekannte Version. Wo eine
  Version angegeben ist, prueft der Server optimistisch; ein veralteter Stand
  wird **je Eintrag** als `konflikt` mit dem aktuellen Stand in `current`
  gemeldet (F-CON-02). Ohne Version gilt reine Setz-Semantik.

```json
{ "ids": [42, 43, 44], "action": "checked", "value": true, "versions": { "43": 5 } }
```

Beispielantwort:

```json
{
  "results": [
    { "id": 42, "outcome": "geaendert" },
    { "id": 43, "outcome": "konflikt", "current": { "id": 43, "version": 2, "checked": false },
      "message": "Die Buchung wurde zwischenzeitlich geaendert." },
    { "id": 44, "outcome": "unveraendert" }
  ]
}
```

`outcome` ist `geaendert` (tatsaechlich geaendert), `unveraendert`
(Setz-Semantik, kein Versionssprung), `konflikt` (veraltete Version; `current`
traegt den aktuellen Stand) oder `fehler` (unbekannte Buchung, Abstimmung einer
Vormerkung, Fachfehler; `message` traegt Klartext). Die Aktionen folgen dem Kern
aus T-108/T-109: `checked`/`reconciled` sind Setz-Semantik (idempotent,
F-CON-01), `note_set` ersetzt, `note_append` haengt atomar an (F-CON-01); je
tatsaechlicher Aenderung entstehen ein Audit-Eintrag und `transaction.changed`
(F-MCP-09, F-CON-04).

## Suche (F-SUC-01..07, F-ABS-04/05)

`GET /api/search` sucht Buchungen ueber den gemeinsamen Baustein
`backend/finue/services/search.py`, den auch die Buchungsliste (T-093) und der
Export (T-097) nutzen (D10: MCP verwendet dieselbe Schicht).

| Parameter | Bedeutung |
|---|---|
| `q` | Suchtext; mehrere Begriffe werden UND-verknuepft, Gross-/Kleinschreibung egal |
| `scope` | `all` (Standard), `account` (dann `account_id`) oder `group` (dann `group_id`) |
| `account_id` / `group_id` | Bereich bei `scope=account` bzw. `group` (F-SUC-03) |
| `period` | `30`, `90`, `365` (Standard), `current_year`, `previous_year`, `all` oder `custom` (F-SUC-04) |
| `from` / `to` | Von-/Bis-Datum (`JJJJ-MM-TT`) fuer `period=custom` |
| `checked` / `reconciled` | `true`/`false`; ohne Angabe "alle" (F-SUC-07, F-ABS-04) |
| `with_note` | nur Buchungen mit Notiz (F-SUC-07) |
| `status` | `gebucht`/`vorgemerkt`; ohne Angabe beide |
| `sort` | `date` (Standard), `amount` oder `counterparty` (F-SUC-05) |
| `direction` | `asc` oder `desc` (Standard) |
| `limit` / `cursor` | Paginierung wie bei allen Listen (SPEC F-MCP-11) |

Betragsbegriffe wie `48,83`, `48.83`, `-48,83` oder `1.234,56` treffen den
Betrag: mit Vorzeichen exakt, ohne Vorzeichen den Absolutwert (F-SUC-02); sie
lassen sich mit Textbegriffen kombinieren. `%` und `_` im Text sind Literale.
Zusammengefuehrte Buchungen (`merged`) fehlen immer (F-DUP-04); `scope=all` und
`scope=group` zaehlen nur aktive Konten (F-SUC-03). Der "heute"-Bezug der
relativen Zeitraeume kommt aus der injizierbaren Uhr in Europe/Berlin (F-SUC-04).
Die externe Referenz (F-ABS-05) liegt in der Suchspalte und ist damit auffindbar.

```json
{
  "items": [
    {
      "id": 12,
      "account_id": 1,
      "booking_date": "2026-05-01",
      "amount_cents": -4883,
      "counterparty": "Max Mustermann",
      "purpose": "Miete",
      "checked": false,
      "reconciled": false,
      "note": null
    }
  ],
  "next_cursor": "o50",
  "total_count": 123,
  "total_cents": -123456
}
```

`total_count` und `total_cents` beziehen sich auf die gesamte gefilterte Menge,
nicht nur die Seite; `total_cents` ist die vorzeichenbehaftete Summe der Treffer
(bei gemischten Waehrungen nur je Konto/Gruppe aussagekraeftig). Ungueltige
Bereiche oder Zeitraeume liefern 422 mit deutschem Klartext.

## Export (F-EXP-01..06)

Die Fachlogik liegt in `backend/finue/services/export.py`, der Router
`backend/finue/api/export.py` uebersetzt nur HTTP. Der Endpunkt aendert keinen
Datenbestand; er nutzt denselben Filterbaustein wie die Suche
(`services/search.py`) und dasselbe JSON-Schema wie der Import (M2,
`docs/schemas/export-v1.schema.json`). CSV-Optionen und Exportprofile kommen aus
`services/export.py` und `services/export_profiles.py` (T-098, F-EXP-03).

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/export` | Buchungen als JSON, Markdown oder CSV exportieren (F-EXP-01..06) |
| GET | `/api/export-profiles` | Gespeicherte CSV-Exportprofile auflisten, Standard zuerst (F-EXP-03) |
| POST | `/api/export-profiles` | CSV-Exportprofil anlegen (F-EXP-03) |
| GET | `/api/export-profiles/{id}` | CSV-Exportprofil ansehen (F-EXP-03) |
| PATCH | `/api/export-profiles/{id}` | CSV-Exportprofil aendern, mit `version` (F-EXP-03, F-CON-02) |
| DELETE | `/api/export-profiles/{id}?version=` | CSV-Exportprofil loeschen, mit `version` (F-EXP-03, F-CON-02) |

Anfragekoerper (die Filterfelder entsprechen `GET /api/search`):

```json
{
  "format": "csv",
  "scope": "account",
  "account_id": 1,
  "period": "365",
  "checked": null,
  "reconciled": null,
  "with_note": false,
  "sort": "date",
  "direction": "asc",
  "profile_id": 1,
  "csv": {"delimiter": ";", "decimal_separator": "comma", "date_format": "TT.MM.JJJJ", "encoding": "utf-8"}
}
```

- `format`: `json` (Standard), `md` oder `csv`.
- `scope`: `all`, `account` (dann `account_id`), `group` (dann `group_id`),
  `search` (freie Suchtreffer ueber `q`) oder `selection` (explizite
  `transaction_ids`, gegen die Grenze gezaehlt).
- Zeitraum und Filter (`q`, `period`, `from`, `to`, `checked`, `reconciled`,
  `with_note`, `status`, `sort`, `direction`) gelten wie bei der Suche
  (F-EXP-02). Zusammengefuehrte Buchungen fehlen immer (F-DUP-04).
- Bei `format="csv"` waehlt `profile_id` ein gespeichertes Profil; `csv`
  ueberschreibt einzelne Optionen. Erlaubt sind Trennzeichen `;`/`,`/Tabulator,
  Dezimalformat `comma`/`point`, Datumsformat `TT.MM.JJJJ`/`JJJJ-MM-TT`,
  Kodierung `utf-8`/`utf-8-sig`/`cp1252`, `columns` (Auswahl und Reihenfolge)
  und `guard_formulas`. Das Standardprofil ist an den Finanzblick-Export
  angelehnt und **unbelegt** (Owner-Beispieldatei fehlt, SPEC Abschnitt 7).

`?download=false` liefert den Koerper ohne `Content-Disposition`; Markdown kommt
dann als `text/plain` fuer die Zwischenablage (sonst `text/markdown`), CSV als
`text/csv`. Im Standard liefert der Server die Datei mit
`Content-Disposition: attachment; filename="<slug>_<von>_<bis>.<ext>"`; der Name
ist ASCII (Umlaute transkribiert), bei Zeitraum `alle` stammt der Zeitraum aus
Minimum/Maximum der exportierten Buchungen (F-EXP-02). Eine Buchungsmenge ueber
`export_max_rows` wird als 413 mit Klartext abgewiesen. Das JSON folgt
`docs/schemas/export-v1.schema.json` (F-EXP-04); der Vorkommen-Index wird darin
1-basiert ausgegeben (der Bestand fuehrt ihn 0-basiert, F-DUP-02).

### CSV-Details (F-EXP-03, F-EXP-06)

- **Spalten** (Auswahl und Reihenfolge ueber `columns`): `booking_date`,
  `value_date`, `counterparty`, `counterparty_iban`, `purpose`, `amount`,
  `currency`, `note`, `checked`, `reconciled`, `external_ref`, `status`,
  `account`. Die deutschen Kopfzeilen stehen in `CSV_COLUMN_LABELS`. In der
  Gruppenansicht wird `account` vorangestellt, wenn nicht schon gewaehlt.
- **Kodierung**: `utf-8` (Standard), `utf-8-sig` (mit BOM) oder `cp1252`. In
  cp1252 nicht darstellbare Zeichen werden mit `?` ersetzt (Regel in
  `decisions.md`).
- **Formel-Injection**: Freitextzellen mit fuehrenden Zeichen `= + - @`, Tabulator
  oder CR werden mit einem Apostroph entschaerft; Betrag und Datum bleiben
  unveraendert. Die Importvorlage "Eigener CSV-Export" macht die Entschaerfung
  beim Re-Import rueckgaengig, sodass der Roundtrip 0 neue Buchungen ergibt
  (F-EXP-06).

### Importvorlage "Eigener CSV-Export" (F-IMP-05, F-EXP-06)

`seed_default_templates` legt vier Standardvorlagen an: N26, Easybank, PayPal
und "Eigener CSV-Export". Letztere passt zum Standardprofil: Semikolon,
`TT.MM.JJJJ`, Dezimalkomma, UTF-8, und mappt die Fingerprint-Felder
(Buchungstag, Wertstellung, Betrag, Waehrung, Gegenpartei, IBAN Gegenpartei,
Verwendungszweck). Ein CSV-Export mit Standardprofil ergibt beim Re-Import ueber
diese Vorlage 0 neue Buchungen.

## Datei-Import und Auftraege (F-IMP-01..08, F-AUF-02/04/05)

Der Import-Assistent nutzt die M2-Services (`services/import_detect.py` fuer die
Erkennung, `import_preview.build_preview`, `import_run.execute_import`,
`import_templates`); die Router `backend/finue/api/imports.py` und
`backend/finue/api/import_templates.py` uebersetzen nur HTTP.

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/import/detect` | Datei hochladen, Format erkennen (CSV/fremdes JSON/eigenes Format), Vorschlaege (F-IMP-01/02/04) |
| POST | `/api/import/preview` | Vorschau je Zeile (neu/vorhanden/Verdachtsdublette) ohne Schreiben (F-IMP-03/06/07) |
| POST | `/api/import/execute` | Import als Lauf mit Protokoll, Herkunft `import` (F-IMP-06) |
| GET | `/api/import-runs` | Sync- und Datei-Laeufe mit Fehlerzeilen, `?account_id=` und `?kind=sync|csv|json` (F-SYN-07) |
| GET | `/api/import-templates` | Vorlagen auflisten; `?account_id=` schlaegt konto-spezifisch vor (F-IMP-05) |
| POST | `/api/import-templates` | Vorlage anlegen (F-IMP-05) |
| GET/PATCH/DELETE | `/api/import-templates/{id}` | Vorlage ansehen/pflegen/loeschen, Aenderung mit `version` (F-IMP-05, F-CON-02) |
| GET | `/api/accounts/{id}/standing-orders` | Auftragsbereich eines Kontos (F-AUF-02/04/05) |
| GET | `/api/standing-orders` | Auftragsbereiche aller FinTS-Konten, paginiert (F-AUF-04/05) |

### Ablauf

`POST /api/import/detect` nimmt die **rohe Datei** als Anfragekoerper und
Korrekturwerte als Query (`encoding`, `delimiter`, `decimal_separator`,
`date_format`, `has_header`, `skip_rows`). Die Antwort enthaelt `kind`
(`csv`/`json`/`own_json`), den `csv`-Vorschlag und einen `token`. Die Bytes
liegen danach nur im Arbeitsspeicher unter diesem `token` (TTL
`api_import_upload_ttl_seconds`); Vorschau und Lauf beziehen sie ueber `token`
statt eines erneuten Uploads.

```json
{ "token": "<token>", "account_id": 1, "mapping": {"Datum": "booking_date", "...": "..."} }
```

- `mapping` ist fuer CSV und fremdes JSON Pflicht (F-IMP-03), entfaellt beim
  eigenen Format (F-IMP-04). `template_id` ersetzt `mapping` und `kind` durch
  eine Vorlage; `format` ueberschreibt Kodierung/Trennzeichen/Kopfzeile.
- Eigenes JSON wird ohne Mapping importiert und behaelt Notizen und Kennzeichen
  (F-IMP-07).
- `restore_flags_for_existing` stellt geprueft/abgestimmt vorhandener Zeilen
  wieder her (F-IMP-07).
- Der Lauf traegt den Status `erfolgreich`/`teilweise`/`fehler` mit Zaehlern
  neu/uebersprungen/Verdacht/fehlerhaft (F-IMP-06, F-SYN-07).

### Upload-Speicher

Der Zwischenspeicher (`services/import_uploads.py`) liegt nur im
Arbeitsspeicher, ist auf `DEFAULT_MAX_ENTRIES` Eintraege begrenzt und laeuft
nach der TTL ab (Entscheidung in `decisions.md`). Ein unbekannter oder
abgelaufener Token liefert 422 `domain_error` mit der Bitte um erneuten Upload.

### Auftraege

`GET /api/accounts/{id}/standing-orders` liefert je Konto `available` (mit
`fetched_at` und Zeilen), `unavailable` (mit dem festen Hinweis "Keine
Auftragsdaten von der Bank verfuegbar", F-AUF-05) oder `none` fuer ein
Datei-Konto ohne Bereich. `GET /api/standing-orders` fuehrt nur FinTS-Konten.
Es gibt **keine** schreibende Route unter `standing-orders` (F-AUF-03).

## Sync (F-SYN-01/02/04/07/10/11)

Die Fachlogik liegt im `backend/finue/services/sync_service.py` und im
`backend/finue/services/sync_runner.py`; der Router
`backend/finue/api/sync.py` uebersetzt nur HTTP. Der `SyncService` ist ein
Singleton im Service-Container (`app.state.services.sync_service`), der Runner
haelt die Job-Momentaufnahmen und den Waechter (`app.state.services.sync_runner`).

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/sync` | Batch-Sync starten, kehrt sofort mit den Job-IDs zurueck (F-SYN-01/11) |
| POST | `/api/accounts/{id}/sync` | Einzel-Sync eines Kontos starten (F-SYN-02) |
| GET | `/api/sync/jobs/{job_id}` | Status, Kontoergebnisse und offene TAN-Challenge (F-SYN-04/07) |
| POST | `/api/sync/jobs/{job_id}/tan` | TAN annehmen, Dialog fortsetzen; zweite Antwort 409 (F-SYN-04) |
| POST | `/api/sync/jobs/{job_id}/approve-poll` | App-Freigabe erneut abfragen (F-SYN-04) |
| POST | `/api/sync/jobs/{job_id}/cancel` | Lauf abbrechen (F-SYN-10) |
| GET | `/api/sync/runs` | Sync-Protokoll, paginiert und je Konto filterbar (F-SYN-07) |

### Anstoss

`POST /api/sync` nimmt `bank_access_id` (optional), `account_ids` (optional) und
`pin` (optional). Ohne `bank_access_id` startet die Route **je Bankzugang mit
aktiven FinTS-Konten** einen Job; `account_ids` und `pin` sind dann nur bei genau
einem Zugang zulaessig (sonst 422). Mit `bank_access_id` startet genau ein Job;
`account_ids` grenzt ihn auf einzelne Konten ein, `account_ids = null` ist der
Batch aller aktiven FinTS-Konten des Zugangs. Die Antwort ist `202 Accepted`:

```json
{ "jobs": [ { "job_id": "3f2c...", "bank_access_id": 1, "status": "laeuft" } ] }
```

Der Lauf laeuft in einem Hintergrund-Thread des einen Worker-Prozesses (D3); die
Antwort kehrt sofort zurueck. Laeuft fuer einen Zugang bereits ein Job, liefert
der Doppelstart denselben Job (F-SYN-11), ohne einen zweiten Thread zu starten.
`POST /api/accounts/{id}/sync` traegt nur `pin` und antwortet mit genau einem Job;
ein Konto ohne aktives FinTS-Ziel liefert 422 `domain_error`.

### TAN-Ablauf und Zustand (F-SYN-04)

`GET /api/sync/jobs/{job_id}` liefert eine Momentaufnahme aus dem
Arbeitsspeicher des Runners:

```json
{
  "job_id": "3f2c...",
  "bank_access_id": 1,
  "status": "wartet_auf_tan",
  "account_ids": [1],
  "account_results": [
    { "account_id": 1, "status": "laeuft", "import_run_id": 7, "attempts": 0, "message": null }
  ],
  "challenge": "Bitte TAN eingeben: 123456",
  "tan_kind": "text",
  "approval_polls": 0,
  "message": null,
  "started_at": "2026-11-27T14:40:00Z"
}
```

- `status` fuehrt dieselben Werte wie das Sync-Protokoll (`laeuft`,
  `wartet_auf_tan`, `erfolgreich`, `teilweise`, `fehler`, `abgebrochen`,
  `zeitueberschreitung`).
- `challenge` ist der anzuzeigende Text der offenen Abfrage, `tan_kind` ihre Art
  (`text` oder `app`). **Dialogdaten, TAN und PIN sind nie Teil der Antwort**
  (N-SEC-02/03); die eingefrorenen Dialogdaten bleiben im `SyncService`.
- `POST .../tan` nimmt `{"tan": "..."}` und setzt den Text-Dialog fort. Die
  **erste** Antwort gewinnt; ein weiterer Aufruf liefert `409` im Standard-
  Fehlerformat mit dem aktuellen Job als `current` (F-SYN-04, F-CON-02).
- `POST .../approve-poll` fragt eine App-Freigabe erneut ab und zaehlt
  `approval_polls`. Verlangt die Bank eine weitere Freigabe, bleibt der Job
  `wartet_auf_tan`.
- `POST .../cancel` bricht ab (F-SYN-10); ein bereits beendeter Job bleibt
  unveraendert (idempotent).

### Protokoll (F-SYN-07)

`GET /api/sync/runs` listet die Import-Laeufe mit `kind = "sync"`, nach Start
absteigend, paginiert (`limit`, `cursor`) und je Konto filterbar
(`?account_id=`). Die Darstellung entspricht `GET /api/import-runs`, welche
dieselbe Tabelle fuehrt.

### PIN und Leckage (N-SEC-01..03)

Bei `store_pin = false` kommt die PIN im Anfragekoerper (`pin`) und wird nur im
Arbeitsspeicher des Laufs gehalten; bei `store_pin = true` wird der uebergebene
Wert ignoriert. Fehlt bei `store_pin = false` die PIN, antwortet die Route 422
`domain_error` mit dem Klartext "PIN bitte eingeben." (F-SYN-08). PIN, TAN und
Dialogdaten erscheinen in keiner Antwort, keinem Log und keinem Ereignis
(N-SEC-02/03); der Vertragstest und ein eigener Leckagetest sichern das.

### Waechter (5-Minuten-Timeout)

Der Runner startet mit der App einen periodischen Waechter-Thread, der offene
TAN-Dialoge nach fuenf Minuten verwirft (`SyncRunner.check_timeouts` ueber
`SyncService.expire_tan_timeout`); der Lauf wechselt dann auf
`zeitueberschreitung`. Waehrend `wartet_auf_tan` haelt der Lauf weder eine
DB-Session noch eine Verbindung (N-DB-04), damit parallele Schreibvorgaenge
nicht warten.

## Grenzen (config)

Zentral in `backend/finue/config.py`:

- `api_default_page_size` = 50, `api_max_page_size` = 200 (Listen).
- `api_max_batch_size` = 1000 (Batch-Aktionen).
- `api_max_upload_bytes` = 50 MiB (Import-Datei; der Icon-Upload mit 512 KB
  aus F-KTO-11 wird in T-090 separat begrenzt).
- `api_import_upload_ttl_seconds` = 300 (Lebensdauer des Import-Upload-Zwischenspeichers).
- `export_max_rows` = 100000 (Export und Auswahl-ID-Liste, F-EXP-01/02).

## Ereignisse (F-CON-04)

Aenderungen meldet der In-Prozess-Ereignisbus
(`backend/finue/services/events.py`). Ein Ereignis besteht aus Typ und IDs
(z. B. `transaction.updated` mit `transaction=42`) und traegt **keine**
Geheimnisse und keine Freitextnutzlast (N-SEC-02). Der Bus liegt im
Service-Container (`app.state.services`); die SSE-Route `/api/events` liefert
ihn in T-101 aus. Der Datei-Import meldet `import.finished` (IDs `account` und
`run`) und `transaction.changed` (ID `account`). Der Edit-Kern der Einzelbuchung
(T-108/T-109) meldet `transaction.changed` mit den IDs `account` und
`transaction`, sobald ein Kennzeichen, die Notiz oder die externe Referenz
tatsaechlich wechselt; ein idempotenter Aufruf meldet nichts. Der Sync (T-112)
meldet `sync.status` (IDs `job`, `bank_access`,
`status`) bei jedem Statuswechsel, `sync.tan_required` (IDs `job`,
`bank_access`) beim Uebergang nach `wartet_auf_tan` und `sync.tan_answered`
(IDs `job`, `bank_access`) beim Uebergang von `wartet_auf_tan` nach `laeuft`.
Die Ereignisse tragen den Challenge-Text nicht; die UI holt ihn ueber
`GET /api/sync/jobs/{job_id}`.

## Sitzungen (F-CON-06)

Die Fachlogik liegt in `backend/finue/services/sessions.py`, der Router
`backend/finue/api/sessions.py` uebersetzt nur HTTP. Beide Routen haengen an der
Auth-Naht. Die **Anlage** einer Sitzung gehoert zur Anmeldung (N-SEC-04, M10);
bis dahin werden Sitzungszeilen in Tests direkt erzeugt.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/sessions` | Aktive Sitzungen mit Geraet/Browser und letztem Zugriff, paginiert (F-CON-06) |
| DELETE | `/api/sessions/{id}` | Einzelne Sitzung abmelden, idempotent (F-CON-06) |

```json
{
  "items": [
    {
      "id": 2,
      "device": "Firefox auf Windows",
      "last_seen_at": "2026-05-01T09:15:00Z",
      "expires_at": "2026-05-01T21:15:00Z",
      "current": false
    }
  ],
  "next_cursor": null
}
```

- "Aktiv" heisst: `expires_at` liegt nach dem aktuellen Zeitpunkt (N-SEC-04);
  abgelaufene Sitzungen erscheinen nicht. Die Liste ist nach `last_seen_at`
  absteigend sortiert (zuletzt genutzte zuerst) und paginiert wie alle Listen
  (SPEC F-MCP-11).
- `device` ist der aus dem User-Agent vereinfachte Text `Browser auf System`
  (F-CON-06); ein fehlender oder unbekannter User-Agent ergibt
  `"Unbekanntes Geraet"`.
- `current` kennzeichnet die Sitzung der laufenden Anfrage. Sobald eine
  Anmeldung existiert (N-SEC-04, M10), traegt sie `true`; in M4 (keine
  Anmeldung) ist das Kennzeichen ueberall `false`.
- `DELETE /api/sessions/{id}` entfernt die Sitzung und ist **idempotent**: ein
  unbekanntes oder bereits abgemeldetes `id` antwortet ebenfalls `204 No
  Content`.
- `token_hash` verlaesst das Backend nie: die DTOs tragen nur ID, Geraetetext
  und Zeitpunkte; es wird nichts protokolliert (N-SEC-02).

## Vertragstest

`tests/unit/test_api_contract.py` ist die Guardrail: jede schreibende Route
braucht einen Eintrag in der Vertragstabelle mit Art
(`version`/`setzen`/`anlegen`/`aktion`) und Begruendung, jede lesende
Listenroute den Parameter `limit` (die Pflicht gilt nur fuer die GET-Route, ein
POST auf demselben Pfad nicht), jede `/api`-Route die Auth-Naht. Unbekannte
Routen und veraltete Eintraege machen den Test rot. Jeder API-Task erweitert die
Tabelle.
