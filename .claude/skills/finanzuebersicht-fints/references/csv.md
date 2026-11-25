# CSV, JSON und Export (SPEC F-IMP, F-EXP)

## CSV-Erkennung (F-IMP-02), alles manuell korrigierbar

| Merkmal | Kandidaten | Erkennung |
|---|---|---|
| Kodierung | UTF-8, UTF-8 mit BOM, Windows-1252 | BOM pruefen, dann UTF-8 strikt dekodieren, sonst Windows-1252 |
| Trennzeichen | `;` `,` Tab | Haeufigkeit je Zeile, konsistente Spaltenzahl |
| Dezimalformat | deutsch `1.234,56`, englisch `1,234.56` | Muster der Betragsspalte |
| Datum | `TT.MM.JJJJ`, `JJJJ-MM-TT`, `MM/TT/JJJJ` u. a. | Muster, bei Mehrdeutigkeit (TT/MM) Nutzer fragen |
| Kopfzeile, Vorspann | Zeilen ueberspringen | Nutzerangabe, Vorschlag aus Spaltenzahl |

Betraege intern als Ganzzahl in Cent plus Waehrung (N-DB-02). Nie Gleitkomma fuer Geld.

## Mapping (F-IMP-03)

- Quellfeld -> Buchungsfeld. Bei CSV sind Quellfelder Spalten, bei fremdem JSON die Schluessel in einer Liste von Objekten,
  verschachtelt per Punkt-Pfad (`amount.value`).
- Betrag aus getrennten Soll/Haben-Spalten, Vorzeichenumkehr (Kreditkarte).
- Eigenes JSON-Format (F-EXP-04) wird ohne Mapping erkannt (`format: "finanzuebersicht"`).

## Vorlagen (F-IMP-05)

Format: JSON-Objekt mit Name, Bank, Kodierung, Trennzeichen, Dezimalformat, Datumsformat, Kopfzeile/Vorspann, Mapping
(Feld -> Quelle), Betragsregel (Vorzeichen, Soll/Haben), Zeilenfilter (z. B. PayPal), Version. Je Konto vorschlagen.
Mitgeliefert: eigener CSV-Export sofort, N26, Easybank, PayPal erst mit Beispieldateien des Owners (offen). Vorlagen sind
editierbar und haben eine Versionsspalte (F-CON-02).

## PayPal (F-IMP-08)

Nur abgeschlossene Zahlungen uebernehmen. Hilfszeilen (Waehrungsumrechnung, Einbehalte, Bankeinzug zur Deckung) per
Vorlagenfilter ausschliessen. Konkrete Spaltennamen erst mit Beispieldatei (**unbelegt**).

## Notiz, geprueft, abgestimmt (F-IMP-07)

Eigene Formate koennen diese Felder tragen. Standard: nur fuer neue Buchungen uebernehmen. Optional fuer vorhandene
Buchungen (Wiederherstellung). Nicht Teil des Fingerprints (F-ABS-06).

## Export (F-EXP)

- Dateiname: `<konto-oder-gruppe>_<von>_<bis>.<endung>`, z. B. `ing-giro_2026-01-01_2026-11-25.csv`. ASCII.
- CSV-Profile: Trennzeichen, Dezimal, Datum, Kodierung, Spaltenauswahl und -reihenfolge, Zusatzspalten Notiz/geprueft/
  abgestimmt/externe Referenz. Standardprofil angelehnt an Finanzblick, **Spalten erst mit Beispieldatei des Owners**
  (offen, bis dahin generisches Profil).
- JSON (F-EXP-04): Kopf (`format`, `version` ganzzahlig ab 1, Zeitpunkt, Filter, Zeitraum), `accounts` ohne Zugangsdaten,
  `transactions` mit Betrag als Dezimal-String mit Punkt (`"-48.83"`), Waehrung, Datum ISO, UTF-8. Verlustfrei.
  Aeltere Versionen schrittweise konvertieren, neuere ablehnen (F-EXP-07). Schema je Version unter `docs/schemas/`.
- Markdown (F-EXP-05): Kopf (Konto/Gruppe, Zeitraum, Anzahl, Summe), Tabelle Datum/Gegenpartei/Zweck/Betrag/Notiz
  (Gruppe mit Kontospalte), Summenzeile, deutsches Zahlen- und Datumsformat, `|` und Zeilenumbrueche maskieren.
- Roundtrip (F-EXP-06): CSV (mit Vorlage) und JSON erneut importiert ergeben 0 neue Buchungen. Markdown nur Export.
