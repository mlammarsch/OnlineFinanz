# SPEC - Finanzuebersicht

Funktionale Spezifikation. Quelle der Anforderungen fuer Planung, Umsetzung und Verifikation.
Anforderungen tragen IDs (`F-...`, `N-...`). Tasks und Tests referenzieren diese IDs.
Aenderungen an dieser Datei nur durch den Owner oder mit seiner Freigabe.

## 1. Zweck und Abgrenzung

Self-hosted, responsive Web-App (Desktop und Mobile) fuer einen einzelnen Nutzer. Sie zeigt Salden und
Umsaetze aller eigenen Bankkonten an einem Ort. Vorbild ist Buhl Finanzblick, bewusst reduziert.

Im Umfang: Salden, Umsaetze, Volltextsuche, Auswahl mit Summenanzeige, Notizen und Pruef-Flag,
Import (CSV, JSON) und Export (CSV, JSON, Markdown), Konten- und Gruppenverwaltung, Sync per FinTS (nur lesend), Uebersicht der
aktiven Termin- und Dauerauftraege (nur Anzeige), Abstimmungskennzeichen je Buchung fuer den Abgleich mit
einer externen Haushaltssoftware (Actual Budget), integrierter MCP-Server fuer den Zugriff durch KI-Clients,
Backup und Restore der Datenbank (manuell und zeitgesteuert, auch auf eine SMB-Freigabe), Betrieb mit
gleichzeitigen Sitzungen desselben Nutzers auf mehreren Geraeten und ueber MCP.

Nicht-Ziele (verbindlich, auch keine Reste im Code oder in der UI):
- keine Kategorisierung, keine Steuer-Kategorien, keine Schlagworte, keine Budgets
- keine Ueberweisungen; Termin- und Dauerauftraege werden nur angezeigt, nie angelegt, geaendert,
  ausgesetzt oder geloescht
- keine direkte Schnittstelle zu Actual Budget in der App; der Abgleich laeuft extern, z. B. durch eine KI
  ueber den MCP-Server
- kein Mehrbenutzerbetrieb, keine Cloud-Dienste, keine Telemetrie
- kein Screen-Scraping, keine PSD2-/TPP-Zertifikate, keine automatisierten Logins ausser FinTS auf Nutzeraktion

## 2. Begriffe

| Begriff | Bedeutung |
|---|---|
| Bankzugang | FinTS-Zugang: BLZ, Endpunkt, Anmeldename, optional verschluesselte PIN, gewaehltes TAN-Verfahren. Ein Zugang kann mehrere Konten liefern. |
| Konto | Girokonto, Tagesgeld, Kreditkarte, Darlehen o. ae. Quelle: `fints` (ueber Bankzugang) oder `datei` (offline, Import per CSV/JSON). |
| Kontengruppe | Benannte Zusammenfassung von Konten mit Summensaldo. Ein Konto gehoert zu hoechstens einer Gruppe. |
| Buchung | Gebuchter Umsatz mit Buchungstag, Wertstellung, Betrag, Gegenpartei, IBAN, Verwendungszweck. |
| Vorgemerkte Buchung | Von der Bank gemeldeter, noch nicht gebuchter Umsatz. |
| Import-Lauf | Ein Sync oder ein Datei-Import (CSV, JSON) fuer ein Konto. Wird mit Ergebnis protokolliert. |
| Dauerauftrag | Bei der Bank hinterlegte, wiederkehrende Ueberweisung (Intervall, naechste Ausfuehrung). |
| Terminauftrag | Bei der Bank hinterlegte, einmalige Ueberweisung mit kuenftigem Ausfuehrungsdatum. |
| Geprueft | Kennzeichen: Buchung wurde angesehen. Steuert den Badge (F-KTO-09). |
| Abgestimmt | Kennzeichen: Buchung stimmt mit der externen Haushaltssoftware ueberein. Unabhaengig von "geprueft". |
| MCP-Token | Zugangsschluessel eines KI-Clients fuer den MCP-Server, mit Berechtigung "lesen" oder "lesen und schreiben". |
| Sitzung | Eine Anmeldung des Nutzers in einem Browser bzw. auf einem Geraet. Mehrere Sitzungen gleichzeitig sind zulaessig. |
| Backup | ZIP-Datei mit vollstaendigem Datenstand und Manifest (F-BAK-01). |
| Backup-Job | Zeitgesteuerte Sicherung auf ein Ziel (lokales Verzeichnis oder SMB-Freigabe). |

## 3. Banken und Datenquellen

| Bank | Quelle | Besonderheiten (Owner-Vorgabe, im Skill mit Quelle zu belegen) |
|---|---|---|
| ING | FinTS | Endpunkt `https://fints.ing.de/fints/`. FinTS-Zugriff setzt einen Online-Banking-Login innerhalb der letzten 90 Tage voraus. Hinweis in Klartext: "Bitte einmal im ING Online-Banking einloggen." |
| Consorsbank | FinTS | Instabil. Fehler 9800 ("Dialog abgebrochen") und 9010 ("Verarbeitung nicht moeglich") kommen regelmaessig vor, wiederholte TAN-Anforderungen ebenso. |
| DKB | FinTS | Stabil, Push-TAN (App-Freigabe). |
| Sparkasse (Darlehen) | FinTS | Endpunkt nicht vorbelegt, der Nutzer traegt ihn ein. Umsaetze ggf. eingeschraenkt; ein Konto nur mit Saldo ist zulaessig. |
| N26 | CSV | Kein FinTS. |
| Easybank (AT, Kreditkarte) | CSV | Kein FinTS. |
| PayPal | CSV | Kein FinTS, Privat-APIs abgekuendigt. |

CSV ist eine vollwertige Datenquelle, nicht nur ein Notbehelf.

## 4. Funktionale Anforderungen

### Konten und Gruppen

- **F-KTO-01** Kontenliste mit Bank-Icon, Kontoname, maskierter IBAN, Zeitpunkt des letzten Syncs und Saldo.
- **F-KTO-02** Gesamtsaldo oben ueber alle aktiven Konten. Pro Konto einstellbar, ob es einfliesst (z. B. Darlehen ausschliessen). Ein Info-Symbol erklaert die Zusammensetzung.
- **F-KTO-03** Gruppen anlegen, umbenennen, sortieren, loeschen (Konten bleiben erhalten). Gruppe zeigt die Icons der Mitglieder, die Kontenzahl und die Summe; sie laesst sich auf- und zuklappen.
- **F-KTO-04** Kontodetails ansehen und pflegen: Name, Kontotyp, IBAN, Waehrung, Gruppe, Einfluss in den Gesamtsaldo, Icon.
- **F-KTO-05** Anlage-Assistent: (a) FinTS: Bank waehlen oder BLZ eingeben, Zugangsdaten, TAN-Verfahren, Konten des Zugangs auswaehlen. (b) Offline-Konto fuer Datei-Import (CSV/JSON).
- **F-KTO-06** Archivieren: Das Konto verschwindet aus Liste, Gesamtsaldo und Batch-Sync, die Daten bleiben erhalten und lassen sich wiederherstellen.
- **F-KTO-07** Loeschen: Ein Bestaetigungsdialog bietet vorher den Export aller Buchungen des Kontos an (Standard JSON, verlustfrei nach F-EXP-04). Loeschen entfernt Buchungen, Notizen und Protokolle des Kontos.
- **F-KTO-08** Bank-Icons, Reihenfolge der Quellen: (1) eigenes Icon des Kontos, (2) Icon der Bank aus den
  Stammdaten, (3) Initialen-Avatar in einer aus dem Banknamen abgeleiteten Farbe. Herkunft eines Bank-Icons:
  - mitgeliefert fuer die Banken aus Abschnitt 3 (Dateien vom Owner, siehe Abschnitt 7);
  - Web-Abruf nur auf ausdrueckliche Aktion "Icon aus dem Web laden" in den Bank-Stammdaten: Die App laedt einmalig
    das Favicon bzw. Logo der in den Stammdaten hinterlegten Website der Bank und speichert es lokal. Kein Abruf beim
    Anzeigen von Seiten, kein Drittanbieter-Dienst (Finanzblick nutzt dafuer einen Server des Herstellers; das
    entfaellt hier bewusst);
  - Upload durch den Nutzer.
- **F-KTO-11** Icon-Upload: PNG, JPEG oder WebP, hoechstens 512 KB. SVG ist ausgeschlossen (Skript-Risiko). Der Server
  prueft den Dateityp am Inhalt, skaliert auf 128 x 128 px, speichert als PNG in der Datenbank (damit im Backup
  enthalten, kein zusaetzliches Verzeichnis im Volume).
- **F-KTO-09** Badge am Konto und an der Gruppe zeigt die Anzahl ungepruefter Buchungen (siehe F-SEL-03).
- **F-KTO-10** Banken-Stammdaten (BLZ, Name, FinTS-Endpunkt, Website, Icon) als editierbare Tabelle. Vorbelegt sind nur Endpunkte mit belegter Quelle.

### Synchronisation (FinTS, nur lesend)

- **F-SYN-01** Batch-Sync aller aktiven FinTS-Konten ueber das Sync-Symbol in der Kontenliste. Ein Zugang mit mehreren Konten braucht nur einen Dialog.
- **F-SYN-02** Einzel-Sync pro Konto.
- **F-SYN-03** Abruf von Saldo, gebuchten und vorgemerkten Umsaetzen sowie aktiven Termin- und
  Dauerauftraegen (F-AUF-04). Zeitraum: ab dem letzten erfolgreichen Sync minus 14 Tage Ueberlappung, beim ersten Sync so weit, wie die Bank liefert.
- **F-SYN-04** TAN-Ablauf in der UI: Challenge-Text anzeigen, TAN eingeben bzw. bei App-Freigabe "In der App bestaetigen" mit Fortschritt. Abbruch ist moeglich. Timeout nach 5 Minuten. Die TAN-Abfrage erscheint in allen angemeldeten Sitzungen; die erste Antwort gilt, in den anderen Sitzungen schliesst sich der Dialog.
- **F-SYN-05** Fehler-Mapping: Bankcode -> Klartext und Handlungsempfehlung (u. a. ING 90-Tage-Regel, 9800, 9010, falsche PIN, gesperrter Zugang). Unbekannte Codes zeigen den Originaltext der Bank. Pflege nach F-SYN-09.
- **F-SYN-06** Automatische Wiederholung fuer Codes, die im Mapping als "wiederholbar" markiert sind (Standard: 9800, 9010): hoechstens 2 Versuche mit neuem Dialog und Wartezeit.
- **F-SYN-07** Sync-Protokoll pro Import-Lauf: Zeitpunkt, Konto, Status, Anzahl neu/unveraendert/Verdacht, Fehlercode und Klartext. Ohne PIN, TAN und Rohdaten von Zugangsdaten. Status: `erfolgreich`, `teilweise` (Batch mit Fehlern bei einzelnen Konten), `fehler`, `abgebrochen` (durch Nutzer), `zeitueberschreitung` (TAN nicht rechtzeitig).
- **F-SYN-08** Option je Bankzugang: PIN verschluesselt speichern oder bei jedem Sync abfragen.
- **F-SYN-09** Pflege des Fehler-Mappings in der UI (Einstellungen -> Bankfehler): Code, optional nur fuer eine BLZ, Klartext, Handlungsempfehlung, wiederholbar ja/nein. Mitgelieferte Standardeintraege sind gekennzeichnet und lassen sich auf den Standard zuruecksetzen; eigene Eintraege haben Vorrang.
- **F-SYN-10** Abbruch und Zeitueberschreitung: Der Dialog mit der Bank wird beendet bzw. verworfen. Jedes Konto wird in einer eigenen Transaktion uebernommen: Konten, deren Abruf vor dem Abbruch vollstaendig war, bleiben gespeichert; vom abgebrochenen Konto wird nichts gespeichert. Der Lauf erhaelt den Status nach F-SYN-07.
- **F-SYN-11** Pro Bankzugang laeuft hoechstens ein Sync gleichzeitig. Ein weiterer Anstoss (anderes Geraet, MCP, Batch) erhaelt den laufenden Job zurueck, statt einen zweiten zu starten.

### Dublettenerkennung (gilt fuer FinTS und Datei-Import)

- **F-DUP-01** Fingerprint je Buchung: SHA-256 ueber normalisierte Felder: Konto, Buchungstag, Wertstellung, Betrag in Cent, Waehrung, Gegenpartei, Gegenpartei-IBAN, Verwendungszweck. Normalisierung: Unicode NFC, Grossschreibung, Zeilenumbrueche entfernen (MT940 bricht nach 27/65 Zeichen um), Mehrfach-Leerzeichen zusammenfassen, trimmen.
- **F-DUP-02** Abgleich als Multimenge: Fuer jeden Fingerprint f gilt: n_neu = Anzahl in der Lieferung, n_db = Anzahl im Konto. Eingefuegt werden max(0, n_neu - n_db) Buchungen mit laufendem Vorkommens-Index. Eindeutigkeit ueber (Konto, Fingerprint, Index).
  - Zwei echte, gleiche Buchungen am selben Tag werden beide uebernommen.
  - Wiederholter Import derselben Daten erzeugt 0 neue Buchungen.
  - Kommt eine gleiche Buchung spaeter hinzu, wird genau sie ergaenzt.
  - Liefert die Bank weniger als gespeichert, wird nichts geloescht; das Protokoll warnt.
- **F-DUP-03** Verdachtsdubletten: gleiches Konto, gleicher Betrag, Buchungstag +/- 2 Tage, abweichender Text (z. B. nachtraeglich geaenderter Bank-Text, CSV neben FinTS). Werden importiert, markiert und in der UI als Paar zum Zusammenfuehren oder Ignorieren angeboten (F-DUP-05). Nie automatisch geloescht.
- **F-DUP-05** Aufloesung einer Verdachtsdublette, nur durch den Nutzer in der UI:
  - Zusammenfuehren: Der Nutzer waehlt die bleibende Buchung (Vorschlag: FinTS vor Datei, sonst die aeltere).
    Bankfelder der bleibenden Buchung bleiben unveraendert. Notizen werden zusammengefuehrt (beide Texte, getrennt,
    mit Herkunftsvermerk), geprueft und abgestimmt sind gesetzt, wenn sie bei einer der beiden gesetzt waren.
    Externe Referenzen: ist nur eine gesetzt, wird sie uebernommen; sind beide verschieden, waehlt der Nutzer.
  - Die andere Buchung wird nicht physisch geloescht, sondern als `zusammengefuehrt` markiert (unsichtbar, nicht in
    Summen und Exporten). Ihr Fingerprint und Index bleiben belegt, damit ein spaeterer Sync oder Import sie nicht
    erneut anlegt.
  - Ein Zusammenfuehren laesst sich ueber das Protokoll rueckgaengig machen.
  - Ignorieren: Das Paar wird als "keine Dublette" gespeichert und nicht erneut vorgeschlagen.
- **F-DUP-04** Vorgemerkte Buchungen nehmen nicht am Fingerprint-Abgleich teil. Jeder Sync ersetzt die vorgemerkten Buchungen des Kontos komplett. Notiz und Pruef-Flag einer vorgemerkten Buchung gehen auf die gebuchte ueber, wenn genau ein Kandidat passt (Betrag, Gegenpartei, +/- 10 Tage). Sonst bleiben sie als Hinweis im Protokoll erhalten.

### Buchungsliste

- **F-BUC-01** Kopf: Bank-Icon, Kontoname, Saldo.
- **F-BUC-02** Bereich "Vorgemerkte Buchungen" oben mit Summe und Saldo inklusive Vormerkungen.
- **F-BUC-03** Gebuchte Umsaetze gruppiert nach Buchungstag, absteigend. Tageskopf: Datum, Tagessumme in Klammern, Saldo am Tagesende (rueckwaerts vom aktuellen Saldo berechnet; bei Datei-Konten aus Saldo-Spalte oder Ankersaldo).
- **F-BUC-04** Zeile: Checkbox, Statusmarke (Vorgemerkt / Quelle), Gegenpartei fett, Verwendungszweck einzeilig gekuerzt, Betrag (negativ rot, positiv gruen), Marker fuer Notiz, "geprueft" und "abgestimmt". Beide Kennzeichen sind auf dem Desktop per Klick auf den Marker direkt in der Zeile umschaltbar, im Touch-Modus per Wisch-Aktion (F-MOB-08) und in der Detailansicht.
- **F-BUC-05** Gruppenansicht: Buchungen aller Konten einer Gruppe gemeinsam, mit Kontokennzeichnung je Zeile.
- **F-BUC-06** Fluessig bei 100.000 Buchungen (Paginierung oder virtuelles Scrollen).

### Termin- und Dauerauftraege (nur Anzeige)

Status: **bedingt**. F-AUF-01 bis F-AUF-04 und F-AUF-06 haengen vom Ergebnis der Machbarkeitspruefung F-AUF-07 ab.
Planung: Die Pruefung ist der erste Task in M3. Faellt sie negativ aus, gilt nur F-AUF-05, und die bedingten
Anforderungen werden in dieser Datei als "zurueckgestellt" markiert. Aufwandsschaetzungen fuer F-AUF erst nach der Pruefung.

- **F-AUF-01** (bedingt) Pro FinTS-Konto zeigt die Buchungsliste oberhalb der Vormerkungen einen aufklappbaren Bereich
  "Termin- und Dauerauftraege" (standardmaessig zugeklappt). Der Kopf zeigt "naechste 30 Tage", die Summe der
  Ausfuehrungen in diesem Zeitraum in Klammern und den daraus erwarteten Saldo (wie Mockup 1). Der Zeitraum ist in
  den Einstellungen waehlbar (30, 60, 90 Tage) und steht immer sichtbar im Kopf, damit er nicht mit dem Zeitraumfilter
  der Buchungsliste (F-SUC-04) verwechselt wird.
- **F-AUF-02** (bedingt) Aufgeklappt: Liste der aktiven Auftraege mit Empfaenger, IBAN, Verwendungszweck, Betrag, Art
  (Dauerauftrag/Terminauftrag), Intervall (monatlich, quartalsweise, ...), naechste Ausfuehrung, erste und
  letzte Ausfuehrung (falls befristet). Sortiert nach naechster Ausfuehrung.
- **F-AUF-03** (bedingt) Ein Klick zeigt die Details im Detailbereich, ohne Bearbeitungs- oder Loeschfunktion.
- **F-AUF-04** (bedingt) Abruf beim Sync zusammen mit Saldo und Umsaetzen. Jeder Abruf ersetzt den Auftragsbestand des
  Kontos komplett. Angezeigt wird der Stand des letzten Abrufs mit Zeitstempel.
- **F-AUF-05** Liefert die Bank keine Auftraege oder unterstuetzt sie den Abruf nicht, steht dort
  "Keine Auftragsdaten von der Bank verfuegbar". Der Sync gilt trotzdem als erfolgreich. Bei Datei-Konten
  entfaellt der Bereich.
- **F-AUF-06** (bedingt) Kontouebergreifende Uebersicht aller aktiven Auftraege (Menue der Kontenliste) mit Summe pro
  Monat, ebenfalls nur lesend.
- **F-AUF-07** Machbarkeitspruefung: python-fints dokumentiert keinen fertigen Abruf fuer Dauerauftraege
  (FinTS HKCDB) und Terminauftraege (HKCSB). Vorgehen: Unterstuetzung in der eingesetzten Version pruefen.
  Fehlt sie, als lesende Erweiterung im Projekt umsetzen und mit Mocks testen. Ist das nicht tragfaehig,
  bleibt F-AUF-05 der dokumentierte Zustand, und das Thema wird als offener Punkt gefuehrt.

### Abstimmung mit externer Haushaltssoftware

- **F-ABS-01** Jede gebuchte Buchung hat das Kennzeichen "abgestimmt" (ja/nein, Standard nein) mit Zeitpunkt und
  Herkunft der letzten Aenderung (UI, MCP, Import). Vorgemerkte Buchungen lassen sich nicht abstimmen.
- **F-ABS-02** Setzen und Zuruecksetzen einzeln (Zeile, Detailbereich) und als Batch-Aktion auf die Auswahl.
- **F-ABS-03** Pro Konto und Gruppe: abgestimmter Saldo (Summe aller abgestimmten Buchungen plus Anfangssaldo),
  Anzahl und Summe der nicht abgestimmten Buchungen, Differenz zwischen Banksaldo und abgestimmtem Saldo.
  Anzeige im Kopf der Buchungsliste.
- **F-ABS-04** Filter "nicht abgestimmt / abgestimmt / alle" in Buchungsliste und Suche (F-SUC-07).
- **F-ABS-05** Optional pro Buchung eine externe Referenz (Freitext, z. B. die Transaktions-ID in Actual
  Budget), setzbar ueber UI und MCP, durchsuchbar.
- **F-ABS-06** Das Kennzeichen bleibt bei jedem Sync und Re-Import erhalten. Es nimmt nicht am Fingerprint teil.

### Auswahl und Batch

- **F-SEL-01** Checkbox je Buchung und "Alle" (bezogen auf die aktuelle Liste bzw. die Suchtreffer). Die Auswahlleiste zeigt "Auswahl (n)" und die Summe der Betraege, mit Schliessen-Symbol.
- **F-SEL-02** Die Auswahlsumme bleibt dauerhaft sichtbar, auf Mobile als fixierte Leiste unten.
- **F-SEL-03** Batch-Aktionen: Notiz setzen oder anhaengen, als geprueft/ungeprueft markieren, als abgestimmt/nicht abgestimmt markieren, Auswahl exportieren (CSV, JSON, Markdown), Auswahl als Markdown in die Zwischenablage kopieren. Neue Buchungen sind ungeprueft und nicht abgestimmt.

### Detailbereich

- **F-DET-01** Einzelbuchung: Gegenpartei, IBAN, Betrag gross, Verwendungszweck (aufklappbar, vollstaendig), Buchungstag, Wertstellung, Buchungsart, Quelle (FinTS/CSV/JSON, Import-Lauf), Notiz bearbeitbar, Pruef-Flag, Abstimmungskennzeichen mit Zeitpunkt und Herkunft, externe Referenz.
- **F-DET-02** Mehrfachauswahl: "Auswahl (n)", Summe gross, Aktionen aus F-SEL-03.

### Suche

- **F-SUC-01** Ein Suchfeld durchsucht Gegenpartei, Verwendungszweck, Notiz, IBAN und Betrag. Mehrere Begriffe werden UND-verknuepft, Gross-/Kleinschreibung egal.
- **F-SUC-02** Betragssuche: Eingaben wie `48,83`, `48.83`, `-48,83` treffen den Betrag (mit Vorzeichen, falls angegeben, sonst den Absolutwert).
- **F-SUC-03** Bereich: aktuelles Konto, aktuelle Gruppe oder alle Konten.
- **F-SUC-04** Zeitraumfilter (Kalender-Symbol): 30 Tage, 90 Tage, 365 Tage (Standard), laufendes Jahr, Vorjahr, frei von-bis, alle.
- **F-SUC-05** Sortierung: Datum ab/auf, Betrag ab/auf, Gegenpartei A-Z/Z-A.
- **F-SUC-06** Umsetzung ueber portables SQL (SQLAlchemy, LIKE auf einer normalisierten Suchspalte plus Indizes). Keine datenbankspezifischen Volltext-Erweiterungen.
- **F-SUC-07** Zusatzfilter: abgestimmt (ja/nein/alle), geprueft (ja/nein/alle), mit Notiz.

### Import (CSV, JSON)

- **F-IMP-01** Assistent: Datei waehlen -> Formaterkennung (CSV oder JSON) -> Mapping -> Vorschau -> Import.
- **F-IMP-02** CSV-Erkennung mit manueller Korrektur: Kodierung (UTF-8, UTF-8 mit BOM, Windows-1252), Trennzeichen (`;` `,` Tab), Dezimalformat (deutsch `1.234,56` / englisch `1,234.56`), Datumsformat (`TT.MM.JJJJ`, `JJJJ-MM-TT`, `MM/TT/JJJJ` u. a.), Kopfzeile, Vorspann-Zeilen ueberspringen.
- **F-IMP-03** Mapping Quellfeld -> Buchungsfeld, auch Betrag aus getrennten Soll/Haben-Spalten und Vorzeichenumkehr (Kreditkarte). Bei CSV sind Quellfelder die Spalten, bei fremdem JSON die Schluessel der Objekte in einer Liste (verschachtelte Schluessel per Punkt-Pfad, z. B. `amount.value`).
- **F-IMP-04** Eigenes JSON-Format (F-EXP-04) wird ohne Mapping erkannt und direkt importiert.
- **F-IMP-05** Mapping-Vorlagen speichern und pro Konto vorschlagen. Mitgelieferte Vorlagen fuer N26, Easybank, PayPal und den eigenen CSV-Export, sobald Beispieldateien vorliegen.
- **F-IMP-06** Die Vorschau zeigt pro Zeile: neu / bereits vorhanden / Verdachtsdublette (Abgleich nach F-DUP). Der Import ist ein Import-Lauf mit Protokoll.
- **F-IMP-07** Enthaelt die Datei Notiz, geprueft, abgestimmt oder externe Referenz (eigenes CSV- oder JSON-Format), kann
  der Nutzer in der Vorschau waehlen, ob diese Werte fuer bereits vorhandene Buchungen uebernommen werden
  (Wiederherstellung). Standard: nur fuer neue Buchungen uebernehmen.
- **F-IMP-08** PayPal-Besonderheit: Nur abgeschlossene Zahlungen uebernehmen, Hilfszeilen (Waehrungsumrechnung, Einbehalte, Bankeinzug zur Deckung) per Vorlage filtern.

### Export (CSV, JSON, Markdown)

- **F-EXP-01** Export von Konto, Gruppe, Suchtreffern oder Auswahl, jeweils als CSV, JSON oder Markdown.
- **F-EXP-02** Zeitraum und Filter der aktuellen Ansicht gelten fuer den Export. Dateiname mit Konto bzw. Gruppe und
  Zeitraum, z. B. `ing-giro_2026-01-01_2026-11-25.csv`.
- **F-EXP-03** CSV-Optionen: Trennzeichen, Dezimalformat, Datumsformat, Kodierung, Spaltenauswahl und -reihenfolge. Zusatzspalten: Notiz, geprueft, abgestimmt, externe Referenz. Standardprofil angelehnt an den Finanzblick-Export (Spalten nach Beispieldatei des Owners). Profile speicherbar.
- **F-EXP-04** JSON-Format, dokumentiert in `docs/export-format.md` und als JSON-Schema mitgeliefert:
  - Kopf: `format: "finanzuebersicht"`, `version` (ganzzahlig, beginnend mit 1), Erstellungszeitpunkt, Filter und Zeitraum.
  - `accounts`: Kontostammdaten der enthaltenen Konten (ohne Zugangsdaten).
  - `transactions`: alle Buchungsfelder inkl. Status, Notiz, geprueft, abgestimmt mit Zeitpunkt und Herkunft,
    externe Referenz. Betraege als Dezimal-String mit Punkt (`"-48.83"`) plus Waehrung, Datum ISO `JJJJ-MM-TT`, UTF-8.
  - Verlustfrei: Export und Re-Import ergeben denselben Datenstand.
- **F-EXP-05** Markdown fuer die textuelle Weiterverarbeitung, vor allem fuer markierte Buchungen:
  - Kopf mit Konto bzw. Gruppe, Zeitraum, Anzahl und Summe.
  - Tabelle mit Datum, Gegenpartei, Verwendungszweck, Betrag, Notiz; Gruppenansicht zusaetzlich mit Kontospalte.
  - Summenzeile am Ende. Zahlen- und Datumsformat deutsch.
  - Sonderzeichen werden maskiert (`|`, Zeilenumbrueche im Verwendungszweck), damit die Tabelle gueltig bleibt.
  - Ausgabe als `.md`-Datei oder ueber "In Zwischenablage kopieren".
- **F-EXP-06** Roundtrip: Ein CSV-Export (mit passender Vorlage) und ein JSON-Export lassen sich wieder importieren
  und ergeben 0 neue Buchungen. Markdown ist reiner Export.
- **F-EXP-07** Versionen des JSON-Formats: Der Import liest jede fruehere Version (Konvertierung schrittweise auf die
  aktuelle Version). Eine neuere Version als die der App wird mit klarer Meldung abgelehnt. Jede Formatversion bleibt
  als JSON-Schema unter `docs/schemas/` erhalten und hat eine Test-Datei, die bei jedem Release importiert wird.
  Formataenderungen erhoehen `version` und werden in `docs/export-format.md` beschrieben.

### MCP-Server (Zugriff fuer KI-Clients)

- **F-MCP-01** Integrierter MCP-Server im selben Prozess wie die App, Transport Streamable HTTP unter `/mcp`.
  Kein zweiter Dienst, kein zweiter Container.
- **F-MCP-02** Lese-Tools: Konten und Gruppen mit Salden; Buchungen nach Konto, Gruppe, Zeitraum, Suchbegriff,
  Betrag, Abstimmungs- und Pruefstatus (mit Seitenweise-Abruf); Einzelbuchung; Termin- und Dauerauftraege;
  Sync-Status und Protokoll; Abstimmungsuebersicht nach F-ABS-03.
- **F-MCP-03** Berichte als Tools: Ein- und Ausgaben je Monat und Konto/Gruppe, Summen je Gegenpartei im
  Zeitraum, Saldoverlauf (Tagesend-Salden), Liste nicht abgestimmter Buchungen mit Summe. Ohne Kategorien.
- **F-MCP-04** Schreib-Tools (nur mit Token-Berechtigung "lesen und schreiben"): abgestimmt setzen/zuruecksetzen
  (einzeln und Batch), externe Referenz setzen, Notiz setzen/anhaengen, geprueft setzen, Sync eines Kontos oder
  aller Konten anstossen.
- **F-MCP-05** Sync ueber MCP: Das Tool startet den Sync-Job und liefert dessen Status. Ist eine TAN noetig,
  meldet das Tool "TAN erforderlich, bitte in der App bestaetigen". Eine TAN wird nie ueber MCP angenommen.
  App-Freigabe (Push-TAN) laeuft normal weiter.
- **F-MCP-06** Nicht ueber MCP verfuegbar: Loeschen von Buchungen, Konten oder Gruppen, Aendern von Betrag,
  Datum oder Text einer Buchung, Zusammenfuehren von Dubletten, Bankzugaenge, PINs, Einstellungen, Tokens,
  Datei-Import, Restore, Anlegen und Aendern von Backup-Jobs (enthalten Zugangsdaten).
- **F-MCP-07** Werkzeugbeschreibungen auf Deutsch und eindeutig (Parameter, Einheiten: Betraege in Euro mit
  Vorzeichen, Datum ISO `JJJJ-MM-TT`). Antworten strukturiert (JSON), Listen begrenzt mit Hinweis auf weitere Seiten.
- **F-MCP-08** Token-Verwaltung in der UI (Einstellungen): anlegen mit Name und Berechtigung, einmalige Anzeige,
  widerrufen, letzter Zugriff sichtbar. Der MCP-Server laesst sich komplett abschalten.
- **F-MCP-09** Jede schreibende MCP-Aktion wird protokolliert (Zeitpunkt, Token-Name, Tool, betroffene Buchungen).
  Das Protokoll ist in der UI einsehbar.
- **F-MCP-10** Backup ueber MCP: Lese-Tool fuer Backup-Jobs und deren letzte Laeufe (ohne Zugangsdaten); Schreib-Tool
  "Backup-Job jetzt ausfuehren" fuer einen bestehenden Job. Anlegen und Aendern von Jobs nur in der UI (F-MCP-06).
- **F-MCP-11** Begrenzungen gegen Ueberlastung (Werte konfigurierbar, Standard in Klammern):
  - Seitengroesse bei Listen (Standard 50, hoechstens 200), Antwortgroesse hoechstens 1 MB.
  - Berichte: Zeitraum hoechstens 24 Monate pro Aufruf. Saldoverlauf mit automatischer Verdichtung: taeglich bis
    92 Tage, woechentlich bis 2 Jahre, darueber monatlich.
  - Pro Token hoechstens 60 Aufrufe pro Minute und 2 gleichzeitige Aufrufe; darueber Antwort 429 mit `Retry-After`.
  - Laufzeit pro Tool-Aufruf hoechstens 10 s, danach Abbruch mit verstaendlicher Meldung und Hinweis auf kleineren Zeitraum.
  - Sync-Anstoss ueber MCP hoechstens einmal pro Bankzugang alle 10 Minuten (schont den Bankzugang).
  - Ueberschreitungen werden als verstaendliche Tool-Fehlermeldung geliefert, nicht als leeres Ergebnis.

### Backup und Restore

- **F-BAK-01** Backup-Format: ZIP mit
  - `manifest.json`: App-Version, Datenbank-Schemaversion (Alembic-Revision), Version des Backup-Formats,
    Zeitpunkt, Datenbanktyp, Anzahl je Tabelle, SHA-256-Pruefsummen der Inhalte;
  - logischer Abzug aller Tabellen als JSON Lines (datenbankunabhaengig, erlaubt auch den Umzug von SQLite auf
    PostgreSQL/MySQL);
  - bei SQLite zusaetzlich eine konsistente Kopie der Datenbankdatei (SQLite-Backup-Funktion im laufenden Betrieb,
    fuer die Notfall-Wiederherstellung von Hand).
  Der Fernet-Schluessel (N-SEC-01) ist nie im Backup enthalten.
- **F-BAK-02** Verschluesselung: Backups lassen sich mit einem Backup-Passwort verschluesseln (abgeleiteter Schluessel,
  authentifizierte Verschluesselung). Fuer Backup-Jobs auf Netzwerkziele ist sie standardmaessig aktiv. Das Passwort
  eines Jobs wird wie eine PIN verschluesselt gespeichert.
- **F-BAK-03** Manuelles Backup in der UI (Einstellungen -> Backup): "Backup jetzt erstellen" mit Download, wahlweise
  verschluesselt.
- **F-BAK-04** Restore in der UI:
  1. Backup-Datei hochladen (bzw. aus einem Job-Ziel waehlen), ggf. Passwort eingeben.
  2. Pruefung: Manifest, Pruefsummen, Formatversion. Neuere Version als die App: Abbruch mit Meldung.
  3. Zusammenfassung anzeigen (Zeitpunkt, Konten, Buchungen) und Bestaetigung durch Eingabe von "WIEDERHERSTELLEN".
  4. Automatisches Sicherheits-Backup des aktuellen Stands in das lokale Backup-Verzeichnis.
  5. Wartungsmodus: laufende Syncs und Jobs werden beendet, Schreibzugriffe (UI, API, MCP) erhalten 503 mit Hinweis.
  6. Wiederherstellen aus dem logischen Abzug, anschliessend Migration auf das aktuelle Schema.
  7. Alle Sitzungen werden abgemeldet, MCP-Tokens bleiben gueltig, wenn sie im Backup enthalten sind.
  Schlaegt ein Schritt fehl, bleibt der vorherige Stand unveraendert bzw. wird aus dem Sicherheits-Backup
  wiederhergestellt. Passt der Fernet-Schluessel nicht zum Backup, werden gespeicherte PINs und Job-Passwoerter als
  ungueltig markiert und muessen neu eingegeben werden; alle anderen Daten werden wiederhergestellt.
- **F-BAK-05** Backup-Jobs (Einstellungen -> Backup -> Jobs): Name, Ziel, Zeitplan, Aufbewahrung, Verschluesselung, aktiv ja/nein.
  - Ziele: lokales Verzeichnis im Daten-Volume oder SMB-Freigabe (Server, Freigabe, Pfad, Benutzer, Passwort,
    optional Domaene). Nur SMB 2/3, SMB 1 wird abgelehnt. Zugriff ueber eine Python-Bibliothek, ohne Einbinden ins
    Dateisystem (funktioniert im Container ohne Root-Rechte).
  - Zeitplan: taeglich zu einer Uhrzeit, optional nur an bestimmten Wochentagen. Zeitzone nach N-OPS-06.
  - Aufbewahrung: die letzten n Backups am Ziel (Standard 14), aeltere loescht der Job selbst, nur eigene Dateien
    (erkennbar am Namensmuster `finanzuebersicht_<JJJJ-MM-TT_HHMMSS>.zip`).
  - "Verbindung testen" schreibt und loescht eine Testdatei am Ziel und meldet das Ergebnis im Klartext.
  - "Jetzt ausfuehren" startet einen Lauf ausserhalb des Zeitplans.
- **F-BAK-06** Ausfuehrung: Der Zeitplaner laeuft im App-Prozess. War die App zum geplanten Zeitpunkt nicht aktiv, wird
  der verpasste Lauf beim naechsten Start nachgeholt (einmal, nicht pro verpasstem Tag). Ein Job laeuft nie doppelt.
  Jeder Lauf wird protokolliert (Zeitpunkt, Dauer, Groesse, Ziel, Status, Fehler im Klartext, ohne Passwoerter).
  Fehlschlaege erscheinen auf der Statusseite und in der Kopfzeile (F-UI-08).
- **F-BAK-07** Vor jeder Datenbankmigration beim App-Start wird automatisch ein Backup in das lokale
  Backup-Verzeichnis geschrieben (N-DB-03).

### Gleichzeitige Nutzung (mehrere Geraete, MCP)

Ein Nutzer kann gleichzeitig in mehreren Browsern und auf mehreren Geraeten angemeldet sein, waehrend zusaetzlich
ein MCP-Client schreibt. Keine Aenderung darf dabei unbemerkt verloren gehen.

- **F-CON-01** Kennzeichen werden gesetzt, nicht umgeschaltet: Die API kennt "abgestimmt = ja/nein" und
  "geprueft = ja/nein" statt "umschalten". Zwei gleichzeitige gleiche Aenderungen fuehren so zum selben Ergebnis.
  Notiz "anhaengen" ist eine atomare Operation auf dem Server.
- **F-CON-02** Optimistisches Sperren fuer alle anderen Aenderungen (Notiz ersetzen, externe Referenz, Konto, Gruppe,
  Bankzugang, Vorlage, Fehler-Mapping, Backup-Job): Jeder Datensatz hat eine Versionsnummer. Die Aenderung sendet
  die bekannte Version mit; passt sie nicht mehr, antwortet der Server mit 409 und dem aktuellen Stand. Die UI zeigt
  "Wurde zwischenzeitlich auf einem anderen Geraet geaendert" mit beiden Staenden und laesst den Nutzer waehlen.
  MCP-Schreib-Tools liefern im Konfliktfall eine Fehlermeldung mit dem aktuellen Stand.
- **F-CON-03** Batch-Aktionen liefern ein Ergebnis je Buchung (geaendert, unveraendert, Konflikt). Konflikte brechen
  den Rest des Batches nicht ab.
- **F-CON-04** Aenderungsbenachrichtigung: Der Server meldet Aenderungen (Buchungen, Kennzeichen, Konten, Sync-Status,
  TAN-Anforderung) per Server-Sent Events an alle Sitzungen. Die UI laedt betroffene Daten nach. Ohne Verbindung
  laedt die UI beim Zurueckkehren in den Tab bzw. die App neu.
- **F-CON-05** Ein Sync aendert nie Felder des Nutzers (Notiz, geprueft, abgestimmt, externe Referenz). Konflikte
  zwischen Sync und Bearbeitung entstehen dadurch nicht.
- **F-CON-06** Aktive Sitzungen sind in den Einstellungen sichtbar (Geraet/Browser, letzter Zugriff) und einzeln abmeldbar.

### Oberflaeche

- **F-UI-01** Layout nach Mockup (`docs/mockup/`, siehe Abschnitt 6).
- **F-UI-02** Dark- und Light-Mode, Standard folgt dem System, manuell umschaltbar, Wahl wird gespeichert.
- **F-UI-03** Responsive: Ab 1280 px drei Spalten. Zwischen 768 und 1279 px zwei Spalten, Details als Seitenleiste. Unter 768 px eine Spalte mit Navigation Kontenliste -> Buchungen -> Details (Details siehe F-MOB).
- **F-UI-04** Sprache Deutsch, Waehrungs- und Datumsformat deutsch.
- **F-UI-05** Leerzustand beim ersten Start (keine Konten): Statt der leeren Kontenliste erscheint eine Startseite mit
  drei grossen Aktionen: "Bankkonto per FinTS verbinden" (F-KTO-05a), "Offline-Konto anlegen und Datei importieren"
  (F-KTO-05b), "Backup wiederherstellen" (F-BAK-04). Im Entwicklungsmodus zusaetzlich "Demo-Daten laden".
- **F-UI-06** Weitere Leerzustaende mit Erklaerung und naechster sinnvoller Aktion: Konto ohne Buchungen ("Jetzt
  synchronisieren" bzw. "Datei importieren"), Suche ohne Treffer ("Zeitraum erweitern"), Gruppe ohne Konten, keine
  Verdachtsdubletten, keine Auftraege.
- **F-UI-07** Aenderungen aus anderen Sitzungen und von MCP erscheinen ohne manuelles Neuladen (F-CON-04).
- **F-UI-08** Statusseite (Einstellungen -> Status): letzter Sync je Bankzugang, letzter Lauf je Backup-Job, offene
  Fehler, aktive Sitzungen und MCP-Tokens mit letztem Zugriff. Fehlgeschlagene Backups und Syncs zeigen zusaetzlich
  einen Hinweis in der Kopfzeile, bis sie bestaetigt sind.

### Design

Modern, ruhig und sehr gut lesbar. Abgerundete Formen statt harter Ecken. Ein einheitliches Design-System statt
Einzelloesungen pro Bildschirm.

- **F-DES-01** Schrift: Inter (Google Font, variable Schrift) fuer die gesamte Oberflaeche. Betraege, Salden, Daten und
  IBANs mit gleich breiten Ziffern (`font-variant-numeric: tabular-nums`), damit Betraege buendig untereinander stehen.
  Die Schriftdateien werden mit der App ausgeliefert (WOFF2, Zeichensaetze Latin und Latin-Extended), nie von
  Google-Servern geladen (N-SEC-05, Datenschutz). Ersatzschrift bis zum Laden: System-Schrift, ohne unsichtbaren Text.
- **F-DES-02** Schriftgroessen als feste Stufen: Fliesstext 15 px (Desktop) bzw. 16 px (Touch), Zusatztext 13 px,
  Kontoname und Gegenpartei 15 px halbfett, Saldo im Kopf 24-28 px halbfett, Gesamtsaldo 28-32 px. Zeilenhoehe 1,4 bis 1,5.
  Hoechstens drei Schriftstaerken (normal, halbfett, fett).
- **F-DES-03** Abgerundete Formen als feste Stufen: Karten und Panels 16 px, Schaltflaechen und Eingabefelder 12 px,
  Bottom-Sheets oben 24 px, Badges, Chips und Filter als Pille (vollstaendig rund), Bank-Icons und Avatare rund.
  Keine Elemente mit eckigen Kanten ausser Tabellen-Trennlinien.
- **F-DES-04** Flaechen und Tiefe: Karten heben sich durch Flaechenfarbe und weiche, flache Schatten ab (Light) bzw.
  durch hellere Flaechenstufen statt Schatten (Dark). Grosszuegige Innenabstaende auf einem 4-px-Raster
  (Karten mindestens 16 px innen, 12 px Abstand zwischen Karten).
- **F-DES-05** Farben als Design-Tokens fuer Light und Dark (Hintergrund, Flaeche 1-3, Text primaer/sekundaer, Linie,
  Akzent, Erfolg, Fehler, Warnung). Akzent Orange wie im Mockup. Positive und negative Betraege in gruen bzw. rot,
  zusaetzlich immer mit Vorzeichen, damit die Bedeutung nicht nur an der Farbe haengt.
- **F-DES-06** Kontraste nach WCAG 2.2 Stufe AA in beiden Modi: Text mindestens 4,5:1, grosse Schrift und
  Bedienelemente mindestens 3:1. Sichtbarer Fokusrahmen fuer Tastaturbedienung.
- **F-DES-07** Ein einheitlicher Symbolsatz (Linien-Symbole, gleiche Strichstaerke), keine gemischten Symbolquellen.
- **F-DES-08** Bewegung: kurze, weiche Uebergaenge (150-250 ms) fuer Aufklappen, Seitenwechsel und Bottom-Sheets.
  Bei der Systemeinstellung "Bewegung reduzieren" entfallen Animationen. Beim Laden Platzhalter in Form der spaeteren
  Inhalte (Skeleton) statt Drehsymbol.
- **F-DES-09** Alle Werte aus F-DES-01 bis F-DES-08 sind zentrale Design-Tokens (gemeinsam mit den Touch-Groessen aus
  F-MOB). Komponenten verwenden nur Tokens, keine fest eingetragenen Farben, Radien oder Abstaende. Dokumentation in
  `docs/design.md`.
- **F-DES-10** Im Entwicklungsmodus zeigt eine Seite `/design` alle Komponenten in Light und Dark (Referenz fuer
  Umsetzung und Pruefung).
- **F-DES-11** Pruefbarkeit: Automatische Tests in beiden Modi pruefen Kontraste (axe-core, keine Verstoesse der Stufe
  AA), die geladene Schrift (Inter, aus der eigenen Auslieferung), die Radien von Karten und Schaltflaechen sowie,
  dass keine Anfrage an fremde Server geht. Bildschirmfotos der Seite `/design` in beiden Modi liegen als Evidenz bei.

### Mobile und Touch-Bedienung

Auf Touch-Geraeten hat die Bedienung mit dem Finger Vorrang vor der kompakten Desktop-Darstellung. Die Mobilansicht
ist ein eigenes Bedienkonzept, keine verkleinerte Desktop-Seite.

- **F-MOB-01** Touch-Modus: Massgeblich ist das Eingabegeraet (CSS `pointer: coarse`), nicht nur die Bildschirmbreite.
  Ein Tablet mit zwei Spalten bekommt ebenfalls Touch-Groessen.
- **F-MOB-02** Trefferflaechen im Touch-Modus mindestens 48 x 48 px fuer jedes bedienbare Element (Checkbox, Symbol,
  Menue, Umschalter, Link), mindestens 8 px Abstand zwischen benachbarten Zielen. Ist das sichtbare Element kleiner
  (z. B. Checkbox-Kaestchen), wird die Trefferflaeche unsichtbar erweitert.
- **F-MOB-03** Zeilen im Touch-Modus: Buchungszeile mindestens 64 px hoch, Kontokarte mindestens 72 px. Gegenpartei
  und Betrag bleiben vollstaendig lesbar; gekuerzt wird nur der Verwendungszweck. Betraege werden nie abgeschnitten.
- **F-MOB-04** Schrift (ergaenzt F-DES-02): Fliesstext mindestens 15 px, Eingabefelder mindestens 16 px (verhindert das automatische
  Zoomen auf iOS). Kein Zoom-Verbot im Viewport.
- **F-MOB-05** Navigation als Stapel mit Slide-Uebergang: Kontenliste -> Buchungen -> Details. Zurueck ueber die
  Zurueck-Schaltflaeche oben links, die Wisch-Geste vom linken Rand und die Zurueck-Taste des Browsers bzw. Geraets
  (jeder Schritt ist ein eigener Verlaufseintrag).
- **F-MOB-06** Menues, Filter und Aktionen als Bottom-Sheet (von unten einfahrendes Panel mit grossen Eintraegen,
  per Wischen nach unten schliessbar) statt kleiner Dropdowns oder Popover. Gilt fuer das Kontomenue "...", den
  Zeitraumfilter, Sortierung, Export und die Batch-Aktionen.
- **F-MOB-07** Auswahlmodus: Langes Druecken auf eine Buchung startet den Auswahlmodus, danach waehlt ein Tippen
  Buchungen an oder ab. Im Auswahlmodus zeigt eine fixierte Leiste unten Anzahl, Summe und eine Schaltflaeche
  "Aktionen" (Bottom-Sheet). Verlassen ueber "x" oder Zurueck.
- **F-MOB-08** Wisch-Aktionen auf einer Buchungszeile: nach links wischen zeigt "Abgestimmt" und "Notiz", nach
  rechts wischen schaltet "geprueft" um. Jede Wisch-Aktion ist auch ohne Geste erreichbar (Detailansicht, Aktionen).
- **F-MOB-09** Keine Funktion nur per Mouseover. Elemente, die auf dem Desktop erst beim Ueberfahren erscheinen
  (z. B. "..." an der Kontokarte), sind im Touch-Modus immer sichtbar.
- **F-MOB-10** Ansicht aktualisieren durch Herunterziehen der Liste. Das laedt nur die Daten der App neu; der
  Bank-Sync bleibt eine eigene Schaltflaeche, damit keine TAN-Abfrage versehentlich ausgeloest wird.
- **F-MOB-11** Passende Bildschirmtastatur: Betraege `inputmode="decimal"`, TAN `inputmode="numeric"` mit
  `autocomplete="one-time-code"`, Datum ueber die native Datumsauswahl, Suche mit Such-Taste.
- **F-MOB-12** Fixierte Leisten (Auswahlsumme, Aktionen) beruecksichtigen die sicheren Bereiche des Geraets
  (Notch, Home-Indikator) und verdecken keine Listeneintraege (ausreichender Abstand am Listenende).
- **F-MOB-13** Detailansicht und Dialoge (TAN, Import, Export) im Touch-Modus bildschirmfuellend, mit fester
  Aktionsleiste unten und Schaltflaechen in voller Breite.
- **F-MOB-14** Hoch- und Querformat werden unterstuetzt. Ein Web-App-Manifest erlaubt "Zum Startbildschirm
  hinzufuegen" (eigenes Symbol, ohne Browserleiste). Kein Offline-Betrieb.
- **F-MOB-15** Pruefbarkeit: Ein automatischer Test im Mobil-Viewport mit Touch (390 x 844) misst alle sichtbaren,
  bedienbaren Elemente und schlaegt fehl, wenn eines F-MOB-02 unterschreitet. Ein zweiter prueft Navigation und
  Zurueck (F-MOB-05), Auswahlmodus (F-MOB-07) und Bottom-Sheets (F-MOB-06) per Touch-Gesten.

## 5. Nicht-funktionale Anforderungen

- **N-SEC-01** PINs nur Fernet-verschluesselt in der DB. Der Schluessel liegt in einer Datei (`*.key`), deren Pfad per Umgebungsvariable kommt; nie in DB, Repo oder Image.
- **N-SEC-02** Keine PIN, TAN, Schluessel, MCP-Tokens, Backup- oder SMB-Passwoerter oder Session-Daten in Logs, Fehlermeldungen oder Sync-Protokollen. Ein Log-Filter erzwingt das, ein Test prueft es.
- **N-SEC-03** Pausierte FinTS-Dialogdaten nur im Arbeitsspeicher, nie auf Platte, nie im Browser.
- **N-SEC-04** Anmeldung fuer einen Nutzer (Passwort-Hash Argon2, Session-Cookie HttpOnly/Secure/SameSite). Mehrere Sitzungen gleichzeitig (F-CON-06). Nach 5 Fehlversuchen wachsende Wartezeit vor dem naechsten Versuch. Im Entwicklungsmodus abschaltbar, im Container standardmaessig aktiv.
- **N-SEC-05** Ausgehende Verbindungen nur zu konfigurierten FinTS-Endpunkten, zu konfigurierten Backup-Zielen (SMB) und, nur auf ausdrueckliche Aktion, zur Website einer Bank fuer den Icon-Abruf (F-KTO-08). Eingehend nur Web-UI, API und `/mcp`.
- **N-SEC-06** MCP-Zugriff nur mit Bearer-Token. Tokens nur als Hash in der DB, zufaellig erzeugt (mind. 256 Bit),
  widerrufbar. Ohne gueltiges Token antwortet `/mcp` mit 401. Fuer Zugriff von extern ist HTTPS ueber einen
  Reverse Proxy Voraussetzung (README).
- **N-DB-01** SQLAlchemy 2.x ORM, keine rohen SQL-Strings. Standard SQLite, per `DATABASE_URL` auf PostgreSQL oder MySQL umstellbar. Migrationen mit Alembic.
- **N-DB-02** Betraege als Ganzzahl in Cent plus Waehrung.
- **N-DB-03** Schema-Versionierung: Migrationen nur vorwaerts (kein Downgrade). Jede Migration wird getestet von einer
  leeren Datenbank und von einer Datenbank mit Daten im Stand des vorherigen Releases. Vor einer Migration beim Start
  entsteht automatisch ein Backup (F-BAK-07). Backups aelterer Schemaversionen lassen sich wiederherstellen und werden
  danach migriert.
- **N-DB-04** SQLite im WAL-Modus mit Wartezeit bei Sperren, damit Lesezugriffe waehrend Sync und Backup nicht blockieren.
  Schreibtransaktionen bleiben kurz (je Konto bzw. je Batch-Aktion).
- **N-PERF-01** Suche mit Standardzeitraum unter 500 ms bei 100.000 Buchungen (SQLite, Entwicklungsrechner), per Test mit erzeugten Daten belegt.
- **N-OPS-01** Lokale Entwicklung aus VS Code: Backend und Frontend per Launch-Konfiguration auf localhost, Demo-Daten per Befehl.
- **N-OPS-02** Docker-Image (multi-stage, non-root), ein Volume `/data` mit Datenbank und lokalem Backup-Verzeichnis `/data/backups`, die Schluesseldatei getrennt davon (N-OPS-07); Icons liegen in der Datenbank. Healthcheck, docker-compose fuer den Betrieb auf einer VM, Veroeffentlichung auf Docker Hub.
- **N-OPS-03** README mit Setup, Konfiguration, Backup-Konzept (Schluesseldatei getrennt sichern!), Restore, Reverse Proxy mit HTTPS und Bank-Hinweisen (ING 90 Tage, CSV-Weg fuer N26/Easybank/PayPal, FinTS-Produktregistrierung).
- **N-OPS-04** Gesundheitspruefung: `/api/health` (Prozess laeuft) und `/api/health/ready` (Datenbank erreichbar, Schema
  aktuell, Zeitplaner aktiv). Der Docker-Healthcheck nutzt `ready`.
- **N-OPS-05** Logs strukturiert (JSON) auf stdout, Log-Level per Umgebungsvariable, Inhalte nach N-SEC-02 gefiltert.
- **N-OPS-06** Zeitzone fuer Zeitplaene und Anzeigen per Umgebungsvariable `TZ`, Standard `Europe/Berlin`.
- **N-OPS-07** Persistente Daten und Host-Anbindung: Alles, was die App dauerhaft schreibt, liegt unter `DATA_DIR`
  (Standard `/data`), sonst nirgends im Container. Das Container-Dateisystem darf beim Neustart oder Image-Update
  verloren gehen, ohne dass Daten verloren gehen. Die Compose-Datei bindet `/data` als Ordner der VM ein (Bind-Mount,
  z. B. `./data`), damit der Ordner direkt sichtbar und sicherbar ist; ein benanntes Docker-Volume ist als
  auskommentierte Alternative beigelegt. Die Schluesseldatei liegt nicht in `/data`, sondern unter einem eigenen,
  schreibgeschuetzt eingebundenen Pfad (`FINUE_KEY_FILE`, Standard `/keys/fernet.key`) oder als Docker-Secret. Sie wird
  beim ersten Start angelegt, wenn der Pfad beschreibbar ist, sonst bricht der Start mit klarer Meldung ab.
- **N-OPS-08** Rechte: Der Container laeuft mit fester Benutzer-ID (Standard 10001, per `PUID`/`PGID` anpassbar). Beim
  Start prueft die App, ob `/data` beschreibbar ist, und meldet andernfalls im Klartext, welchen `chown`-Befehl der
  Owner auf der VM ausfuehren muss. Die README beschreibt das.
- **N-OPS-09** SQLite nur auf dem lokalen Dateisystem der VM, nicht auf NFS oder SMB (Sperren und WAL-Modus sind dort
  unzuverlaessig). Die README warnt davor; Backups duerfen auf Netzfreigaben (F-BAK-05). Die Datenbankdatei wird nicht
  per Dateikopie im laufenden Betrieb gesichert (WAL), sondern ueber die Backup-Funktion.
- **N-OPS-10** Umzug vom Entwicklungsrechner auf die VM (und von SQLite auf PostgreSQL) ueber Backup und Restore
  (F-BAK-04), nicht ueber Kopieren der Datenbankdatei. Die Schluesseldatei wird getrennt uebertragen; fehlt sie, gilt
  F-BAK-04 (PINs neu eingeben). Die README enthaelt die Schritte als Anleitung.

## 6. Layout (Mockup)

Dateien: `docs/mockup/01-kontenliste-buchungen.png`, `02-einzelbuchung-details.png`, `03-mehrfachauswahl-summe.png`.
Die Mockups zeigen Finanzblick in der Desktop-Ansicht. Uebernommen werden Aufbau, Anordnung, Dichte und Farbgebung (dunkles Theme, orange Akzentfarbe fuer Auswahl und aktiven Tab). Die Formensprache (Schrift, Radien, Abstaende, Tiefe) folgt dem Abschnitt "Design" und ist moderner und runder als im Mockup. Weggelassen werden alle Nicht-Ziele. Die Dichte gilt nur fuer Maus-Bedienung; im Touch-Modus gilt Abschnitt "Mobile und Touch-Bedienung".

| Bereich | Uebernehmen | Weglassen bzw. ersetzen |
|---|---|---|
| Kontenliste (links) | Titel "Konten" mit Gesamtsaldo und Info-Symbol, Sync-Symbol, Menue; Kontokarten; Gruppen mit gestapelten Icons, Kontenzahl, Auf-/Zuklappen, Badge | - |
| Buchungen (Mitte) | Kopf mit Icon, Name, Saldo; Tab "Buchungen"; Suchfeld mit Kalender- und Menue-Symbol; aufklappbare Zeile "Termin- und Dauerauftraege" mit Summe und erwartetem Saldo (F-AUF); Vorgemerkte Buchungen; Tagesgruppen; Auswahlleiste "Alle / Auswahl (n) / Summe / x" | Tabs "Ueberweisung" und "Auftraege" entfallen. Die Kategorie-Spalte ("Unterhaltung & Spiele", "Urlaub") wird zur Statusmarke (Vorgemerkt, Quelle) |
| Details (rechts) | Einzelbuchung wie Mockup 2; Mehrfachauswahl mit Summe wie Mockup 3 | Optionen "Kategorie", "Steuer-Kategorie", "Schlagwort", "Umbuchen" werden zu "Notiz setzen", "Als geprueft markieren", "Als abgestimmt markieren", "Exportieren", "Als Markdown kopieren" |

## 7. Offene Eingaben des Owners

| Eingabe | Benoetigt ab |
|---|---|
| FinTS-Produktregistrierung (Produkt-ID, kostenlos bei der Deutschen Kreditwirtschaft) | Live-Test FinTS |
| Anonymisierte CSV-Beispiele: N26, Easybank, PayPal, Finanzblick-Export (Ablage: `data/samples/`, nicht im Repo) | Import-Vorlagen, CSV-Export-Standardprofil |
| Bank-Icons als Dateien (PNG/WebP, quadratisch) bzw. Website je Bank fuer den Web-Abruf | Feinschliff Kontenverwaltung |
| SMB-Ziel fuer Backups (Server, Freigabe, Benutzer); wird erst zur Laufzeit in der UI eingetragen, nicht im Repo | Live-Test Backup-Job |
| Docker-Hub-Repository und `docker login` | Veroeffentlichung |
