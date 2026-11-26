# JSON-Exportformat der Finanzuebersicht

Dieses Dokument beschreibt das eigene JSON-Exportformat nach **F-EXP-04** und die
Versionierungsregeln nach **F-EXP-07**. Das Format dient dem verlustfreien Export
und Re-Import von Konten und Buchungen. Es ist unabhaengig von CSV-Profilen und
vom Markdown-Export.

- Formatname: `finanzuebersicht`
- Aktuelle Version: `1`
- Zeichenkodierung: UTF-8
- Dateiendung: `.json`
- Schema: [`docs/schemas/export-v1.schema.json`](schemas/export-v1.schema.json)
  (JSON Schema Draft 2020-12)
- Beispiel: [`docs/schemas/export-v1.example.json`](schemas/export-v1.example.json)

## Aufbau

Die Wurzel ist ein JSON-Objekt mit genau diesen Feldern. Unbekannte Felder sind
nicht erlaubt.

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `format` | String | ja | Immer `"finanzuebersicht"` (F-EXP-04). |
| `version` | Ganzzahl | ja | Formatversion, beginnt bei `1` (F-EXP-04, F-EXP-07). |
| `created_at` | String | ja | Erstellungszeitpunkt als ISO-8601-Zeitstempel mit Zeitzone. |
| `filter` | Objekt | ja | Beschreibung der Exportauswahl. |
| `period` | Objekt | ja | Inklusiver Zeitraum des Exports. |
| `accounts` | Array | ja | Kontostammdaten der enthaltenen Konten, ohne Zugangsdaten. |
| `transactions` | Array | ja | Alle Buchungen des Exports. |

### `filter`

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `scope` | String | ja | `all`, `account`, `group`, `search` oder `selection`. |
| `account_ids` | Array aus Ganzzahlen oder `null` | nein | Betroffene Konten, ohne Doppelte. |
| `group_ids` | Array aus Ganzzahlen oder `null` | nein | Betroffene Gruppen, ohne Doppelte. |
| `transaction_ids` | Array aus Ganzzahlen oder `null` | nein | Betroffene Buchungen bei einer Auswahl. |
| `search` | String oder `null` | nein | Suchbegriff der aktuellen Ansicht. |
| `checked` | Boolean oder `null` | nein | Filter auf das Pruef-Kennzeichen. |
| `reconciled` | Boolean oder `null` | nein | Filter auf das Abstimmungs-Kennzeichen. |
| `with_note` | Boolean oder `null` | nein | Filter auf Buchungen mit Notiz. |

### `period`

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `start` | Datum oder `null` | ja | Erste Tag des Zeitraums, inklusiv. |
| `end` | Datum oder `null` | ja | Letzter Tag des Zeitraums, inklusiv. |

Beide Grenzen duerfen `null` sein. Sind beide gesetzt, muss `start` kleiner oder
gleich `end` sein.

### Konten

`accounts` enthaelt je Konto genau diese Felder. **Zugangsdaten sind bewusst
nicht Teil des Formats** (N-SEC-01, N-SEC-02): kein PIN, kein Loginname, kein
TAN-Verfahren, keine Bankzugangs-ID, keine Verschluesselungsdaten.

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `id` | Ganzzahl | ja | Konto-ID im exportierenden Datenstand. |
| `name` | String | ja | Kontoname. |
| `iban` | String oder `null` | ja | IBAN, falls bekannt. |
| `currency` | String | ja | ISO-4217-Waehrung, drei Grossbuchstaben. |
| `account_type` | String oder `null` | ja | Kontotyp, z. B. `giro` oder `sparen`. |

### Buchungen

`transactions` enthaelt je Buchung alle exportrelevanten Felder. Die Felder
`checked_at`/`checked_origin` sowie `reconciled_at`/`reconciled_origin` sind
genau dann gesetzt, wenn das jeweilige Kennzeichen `true` ist (F-ABS-01).

| Feld | Typ | Pflicht | Bedeutung |
|---|---|---|---|
| `id` | Ganzzahl | ja | Buchungs-ID im exportierenden Datenstand. |
| `account_id` | Ganzzahl | ja | Verweist auf ein Konto in `accounts`. |
| `status` | String | ja | `gebucht` oder `vorgemerkt`. |
| `booking_date` | Datum | ja | Buchungstag. |
| `value_date` | Datum oder `null` | ja | Wertstellung. |
| `counterparty` | String oder `null` | ja | Gegenpartei. |
| `counterparty_iban` | String oder `null` | ja | IBAN der Gegenpartei. |
| `purpose` | String oder `null` | ja | Verwendungszweck. |
| `note` | String oder `null` | ja | Notiz des Nutzers. |
| `checked` | Boolean | ja | Geprueft-Kennzeichen. |
| `checked_at` | Zeitstempel oder `null` | ja | Zeitpunkt der letzten Aenderung des Pruef-Kennzeichens. |
| `checked_origin` | String oder `null` | ja | Herkunft der Aenderung: `ui`, `mcp` oder `import`. |
| `reconciled` | Boolean | ja | Abgestimmt-Kennzeichen. |
| `reconciled_at` | Zeitstempel oder `null` | ja | Zeitpunkt der letzten Aenderung des Abstimmungs-Kennzeichens. |
| `reconciled_origin` | String oder `null` | ja | Herkunft der Aenderung: `ui`, `mcp` oder `import`. |
| `origin` | String | ja | Herkunft der Buchung: `fints`, `csv` oder `json`. |
| `external_ref` | String oder `null` | ja | Externe Referenz (F-ABS-05). |
| `fingerprint` | String | ja | SHA-256-Fingerprint als Hex fuer Dublettenkennung und Re-Import. |
| `occurrence_index` | Ganzzahl | ja | Vorkommen-Index gleicher Fingerprints je Konto. |
| `merged` | Boolean | ja | Markierung einer zusammengefuehrten Buchung (F-DUP-04). |
| `amount` | String | ja | Betrag als Dezimal-String mit Punkt, z. B. `"-48.83"`. |
| `currency` | String | ja | ISO-4217-Waehrung, drei Grossbuchstaben. |

## Datentypen und Konventionen

- **Betraege**: immer Strings mit Punkt als Dezimaltrennzeichen und genau zwei
  Nachkommastellen, optional mit fuehrendem Minus. Beispiele: `"-48.83"`,
  `"2500.00"`. Zahlen wie `12.34` (JSON-Zahl), `"12,34"` oder `"1234"` sind
  ungueltig.
- **Datum**: ISO `JJJJ-MM-TT`, z. B. `"2026-01-15"`. Andere Formate wie
  `"15.01.2026"` sind ungueltig.
- **Zeitstempel**: ISO 8601 mit Zeitzone, z. B. `"2026-11-26T10:41:13Z"` oder
  `"2026-11-26T11:41:13+01:00"`. Zeitstempel ohne Zeitzone sind ungueltig.
- **Waehrung**: ISO-4217-Code aus drei Grossbuchstaben, z. B. `"EUR"`.
- **IDs**: positive Ganzzahlen. Ein Export darf keine doppelten Konto- oder
  Buchungs-IDs enthalten; jede `account_id` einer Buchung muss in `accounts`
  vorkommen.
- **Abgeleitete Felder**: Der normalisierte Suchtext (`search_text`) und die
  optimistische Versionsspalte werden nicht exportiert. Der Suchtext wird beim
  Import neu berechnet, die Version verwaltet die Anwendung.
- **Verlustfreiheit**: `fingerprint`, `occurrence_index`, Notiz, Pruef- und
  Abstimmungskennzeichen sowie die externe Referenz sind enthalten, damit
  Export und Re-Import denselben Datenstand ergeben (F-EXP-04, F-EXP-06).

## Beispiel

Das vollstaendige Beispiel liegt in
[`docs/schemas/export-v1.example.json`](schemas/export-v1.example.json). Auszug:

```json
{
  "format": "finanzuebersicht",
  "version": 1,
  "created_at": "2026-11-26T10:41:13Z",
  "filter": { "scope": "selection", "transaction_ids": [101, 102] },
  "period": { "start": "2026-01-01", "end": "2026-11-25" },
  "accounts": [
    {
      "id": 1,
      "name": "Girokonto",
      "iban": "DE89370400440532013000",
      "currency": "EUR",
      "account_type": "giro"
    }
  ],
  "transactions": [
    {
      "id": 101,
      "account_id": 1,
      "status": "gebucht",
      "booking_date": "2026-01-15",
      "amount": "-48.83",
      "currency": "EUR"
    }
  ]
}
```

Der Auszug ist gekuerzt und nicht schema-valide; massgeblich ist die verlinkte
Beispieldatei mit allen Pflichtfeldern.

## Versionierung (F-EXP-07)

- `version` ist eine Ganzzahl und beginnt bei `1`. Formataenderungen, die nicht
  abwaertskompatibel sind, erhoehen die Version.
- Jede Formatversion bleibt als eigenes JSON-Schema unter `docs/schemas/`
  erhalten, z. B. `export-v1.schema.json`, spaeter `export-v2.schema.json`.
- Zu jeder Version gehoert eine Testdatei, die bei jedem Release importiert
  wird. Fuer Version 1 ist das
  `tests/unit/test_export_schema_v1.py` mit
  `docs/schemas/export-v1.example.json`.
- Der Import liest jede fruehere Version und konvertiert schrittweise auf die
  aktuelle Version.
- Eine **neuere Version als die der App** wird mit klarer Meldung abgelehnt.
  Die Meldung nennt die Dateiversion und die hoechste unterstuetzte Version.
- Dieses Dokument wird bei jeder Formataenderung zusammen mit dem neuen Schema
  fortgeschrieben.
