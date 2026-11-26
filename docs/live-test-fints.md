# Live-Test FinTS (Owner)

Dieses Dokument beschreibt den Live-Test der lesenden FinTS-Anbindung. Es ist ein
Protokoll fuer den Owner, keine automatisierte Pruefung. Die automatisierten Tests
arbeiten ausschliesslich mit Mocks (D8) und Fake-Fixtures.

## Zweck und Grenzen

- Der Live-Test belegt das Verhalten gegen die echte Bank: Anmeldung, Saldo,
  Umsaetze, Vormerkungen, TAN- und App-Freigabe, Abbruch, Timeout und die
  Fehlertexte des Mappings (F-SYN-01 bis F-SYN-08, F-SYN-10, F-AUF-05).
- **Nur der Owner fuehrt echte Logins durch.** Agents und automatisierte Tests
  rufen keine echte Bank auf und verwenden keine echten Zugangsdaten.
- Echte Kontodaten, PINs, TANs, Schluessel und Dialogdaten gehoeren **nie** in
  Fixtures, Repos, Tickets oder Screenshots. Test-Fixtures bleiben erfunden.
- Protokolliert werden nur Fehlercode und Klartext aus dem Fehler-Mapping, nie
  PIN, TAN oder Rohdaten des Dialogs (N-SEC-02, N-SEC-03).
- Stand: Die Service-Schicht, die Sync-Orchestrierung und die Mocks sind in M3
  vorhanden. Der eigentliche Live-Test wird ausgefuehrt, sobald der Sync ueber die
  Oberflaeche oder eine vom Projekt bereitgestellte Bedienung startbar ist.
  Dieses Dokument ist die vorbereitete Owner-Aufgabe aus der M3-Abnahme.

## Voraussetzungen

Vor dem ersten Lauf pruefen:

1. **FINTS_PRODUCT_ID** ist gesetzt und entspricht der registrierten Produkt-ID
   aus SPEC Abschnitt 7. Ohne Produkt-ID bricht der Adapter vor jedem Bankzugriff
   mit einer klaren Meldung ab; es gibt keinen Standardwert und kein Secret im
   Code. Die konkrete ID wird nicht in dieses Dokument kopiert.
2. **Fernet-Schluesseldatei** liegt ausserhalb von Repo, Datenbank und Backup
   (N-SEC-01). Der Pfad kommt aus einer Umgebungsvariable: aktuell liest
   `Settings.key_file` die Variable `KEY_FILE` (Standard `./key`). SPEC N-OPS-07
   nennt fuer den Container `FINUE_KEY_FILE` mit Standard `/keys/fernet.key`;
   diese Umbenennung erfolgt im Betriebs-Task (M10). Bis dahin gilt `KEY_FILE`.
   Die Datei wird einmal erzeugt, restriktiv berechtigt und getrennt gesichert.
   Fehlt sie bei gespeicherter PIN, schlaegt der Zugriff mit Klartext fehl.
3. **Bank-Endpunkt** steht ausschliesslich in `Bank.fints_url` (N-SEC-05). Fehlt
   der Endpunkt, bricht der Adapter mit `MissingEndpointError` ab; es gibt keinen
   Fallback auf fremde Server. Zusaetzlich werden BLZ und Login-Name des Zugangs
   benoetigt.
4. **PIN-Speicheroption** (`BankAccess.store_pin`, F-SYN-08) ist bewusst gewaehlt:
   - `store_pin = False`: Die PIN wird bei jedem Sync abgefragt und nur im
     Arbeitsspeicher gehalten. `pin_encrypted` bleibt leer. Fuer den ersten
     Live-Test empfohlen.
   - `store_pin = True`: Die PIN wird mit Fernet verschluesselt gespeichert und
     beim Sync entschluesselt. Die PIN ist danach nicht mehr im Klartext sichtbar.
5. Der Server laeuft mit **genau einem Worker-Prozess** (D3). Pausierte
   TAN-Dialoge liegen nur im Arbeitsspeicher dieses Prozesses (N-SEC-03).
6. Fuer den Test gilt eine **eigene, wegwerfbare Datenbank** (z. B. eigener
   `DATA_DIR`/`DATABASE_URL`). Keine Produktions- oder Entwicklungsdatenbank mit
   echten Konten verwenden, die erhalten bleiben soll.

## Vorbereitung

- App starten, Migration auf `head` ausfuehren und die FinTS-Funktion aktivieren.
- Bank und Bankzugang mit Testkennzeichnung anlegen: BLZ, Endpunkt, Login-Name,
  `store_pin` und optionales TAN-Verfahren. Die PIN erst im Testdialog eingeben,
  nicht vorab in eine Datei schreiben.
- Sicherstellen, dass das JSON-Log (stdout, N-OPS-05) sichtbar ist und der
  Secret-Filter aktiv ist.
- Vor dem Test notieren: Datum, App-Version, Bank, BLZ, Anzahl Konten, gewaehlte
  PIN-Option und TAN-Verfahren.
- Fuer Screenshots Kontonummern, Namen und Betraege maskieren oder ganz auf
  Screenshots verzichten.

## Testfaelle

Jeden Fall einzeln ausfuehren und im Ergebnisprotokoll festhalten. Nach einem
Fehlschlag nicht sofort wiederholen, sondern Ursache und Fehlercode notieren.

### Erstsync ohne TAN

1. Batch-Sync fuer den Bankzugang starten (F-SYN-01).
2. PIN eingeben, falls `store_pin = False`.
3. Ablauf ohne TAN abwarten.

Erwartet:

- Der Lauf endet mit Status `erfolgreich`; je Konto entsteht genau eine
  `ImportRun`-Zeile mit `kind='sync'`.
- Saldo und gebuchte Umsaetze stimmen mit dem Online-Banking ueberein; vorgemerkte
  Umsaetze sind als vorgemerkt gekennzeichnet (F-SYN-03).
- `new_count` ist beim ersten Lauf plausibel gross, `failed_count` ist 0.
- Ein unmittelbar folgender zweiter Sync erzeugt keine Dubletten (`new_count = 0`,
  F-DUP-02); bankseitig neue Buchungen koennen den Wert natuerlich erhoehen.
- Liefert die Bank keine Auftragsdaten, bleibt der Sync erfolgreich und es
  erscheint "Keine Auftragsdaten von der Bank verfuegbar" (F-AUF-05).

### TAN-Eingabe

1. Sync starten, der eine TAN ausloest.
2. Challenge-Text in der Oberflaeche pruefen und die TAN eingeben.
3. Sync fortsetzen.

Erwartet:

- Vor der Antwort steht der Lauf auf `wartet_auf_tan`, die Challenge ist sichtbar
  (F-SYN-04).
- Nach korrekter TAN wechselt dieselbe `ImportRun`-Zeile auf `erfolgreich`; es
  entsteht keine zweite Zeile fuer dasselbe Konto.
- Die TAN erscheint weder im Log noch in `ImportRun.message`, `ImportRunError`
  oder einer Fehlermeldung (N-SEC-02).
- Eine bereits beantwortete TAN-Abfrage kann nicht erneut beantwortet werden; die
  erste Antwort gilt. In weiteren Sitzungen schliesst sich der Dialog.

### App-Freigabe

1. Sync starten, der eine App-Freigabe (z. B. photoTAN/App) ausloest.
2. In der Oberflaeche "In der App bestaetigen" waehlen.
3. Freigabe in der Bank-App erteilen und den Fortschritt beobachten.

Erwartet:

- Die Oberflaeche zeigt den Fortschritt der Freigabeabfrage; intern steigt
  `approval_polls`, ohne den Dialog zu verlieren.
- Nach der Freigabe endet der Lauf mit `erfolgreich`.
- Ein Abbruch in der App oder ein Timeout fuehrt zu einem klaren Fehlerstatus,
  nicht zu einem haengenden Lauf.

### Batch-Sync mit mehreren Konten

1. Einen Zugang mit mindestens zwei aktiven FinTS-Konten verwenden.
2. Batch-Sync starten.

Erwartet:

- Es wird genau ein Dialog fuer den Zugang gefuehrt (F-SYN-01); nicht je Konto
  ein eigener Login.
- Je Konto entsteht eine eigene `ImportRun`-Zeile mit eigenem Status und Zaehlern
  (F-SYN-07, F-SYN-10).
- Scheitert ein Konto, bleiben die vollstaendig abgerufenen Konten gespeichert;
  der Gesamtstatus ist `teilweise`, das fehlerhafte Konto `fehler`.
- Ein zweiter Anstoss waehrend des Laufs startet keinen zweiten Job, sondern
  liefert den laufenden Job zurueck (F-SYN-11).

### Abbruch

1. Sync starten und warten, bis die TAN-Abfrage oder App-Freigabe erscheint.
2. Abbrechen.

Erwartet:

- Der Dialog mit der Bank wird beendet; bereits vollstaendig uebernommene Konten
  bleiben gespeichert, das abgebrochene Konto und die restlichen Konten erhalten
  `abgebrochen` (F-SYN-10).
- Vom abgebrochenen Konto wird keine Teilmenge gespeichert.
- Die Sync-Sperre des Zugangs ist danach frei; ein neuer Sync startet mit einem
  neuen Job (F-SYN-11).
- PIN, TAN und Dialogdaten tauchen nicht in Log oder Protokoll auf.

### Zeitueberschreitung (Timeout)

1. Sync starten und die TAN-Abfrage oder App-Freigabe absichtlich **fuenf Minuten**
   unbeantwortet lassen.
2. Danach den Zustand pruefen.

Erwartet:

- Der Lauf endet mit `zeitueberschreitung` (F-SYN-04, F-SYN-07).
- Der Dialog wird verworfen; es werden keine pausierten Dialogdaten auf Platte
  geschrieben (N-SEC-03).
- Ein neuer Sync ist anschliessend moeglich; die Sperre ist frei.

### ING-90-Tage-Regel

1. Mit einem ING-Zugang synchronisieren, dessen FinTS-Freigabe laenger als 90 Tage
   nicht im Online-Banking bestaetigt wurde, oder den Fehler gezielt provozieren.
2. Fehlercode, Klartext und Handlungsempfehlung notieren.

Erwartet:

- Die Oberflaeche zeigt Klartext und Handlungsempfehlung statt eines Rohfehlers
  (F-SYN-05). Der aktuelle Standardeintrag lautet "FinTS-Zugriff nicht erlaubt."
  mit "Bitte einmal im ING Online-Banking einloggen."
- Der Fehler wird **nicht** automatisch wiederholt (nicht wiederholbar).
- Der echte Bankcode ist derzeit **unbelegt**; das Mapping nutzt den internen
  Platzhalter `ING90`. Der Owner notiert den tatsaechlichen Code aus dem Lauf,
  damit der Standardeintrag mit Beleg ersetzt werden kann (F-SYN-09).

### Falsche PIN (nur ein Versuch)

1. Einmal absichtlich eine falsche PIN eingeben.
2. Ergebnis pruefen und den Test an dieser Stelle beenden.

Erwartet:

- Klartext "PIN nicht akzeptiert." mit dem Hinweis, dass nach wiederholten
  Fehlversuchen die Sperre des Zugangs droht.
- Kein automatischer Wiederholungsversuch fuer PIN-Codes (nicht wiederholbar).
- **Nur ein Versuch.** Weitere Fehlversuche koennen den Zugang sperren. Bei einer
  Sperre den Test abbrechen und den Zugang im Online-Banking entsperren.

### PIN-Speicheroption (F-SYN-08)

1. Den Fall mit `store_pin = False` und anschliessend mit `store_pin = True`
   ausfuehren.
2. Jeweils `BankAccess.pin_encrypted` und das Log pruefen.

Erwartet:

- `store_pin = False`: Die PIN wird bei jedem Sync abgefragt; `pin_encrypted`
  bleibt leer; nach einem Neustart ist keine PIN gespeichert.
- `store_pin = True`: `pin_encrypted` enthaelt nur Chiffretext; ein Sync nach
  Neustart funktioniert ohne erneute PIN-Eingabe.
- In keinem Fall steht die PIN im Klartext in Datenbank, Log, Fehlermeldung oder
  Backup.

### Wiederholbare Bankcodes 9800 und 9010 (beobachten)

Diese Codes lassen sich nicht zuverlaessig erzwingen. Falls sie im Lauf
auftreten:

- Der Adapter wiederholt hoechstens zweimal mit neuem Dialog und Wartezeit
  (F-SYN-06).
- Danach erscheint der Klartext des Mappings; der Lauf bleibt nachvollziehbar.
- Den beobachteten Code samt Bank und Datum im Protokoll notieren.

## Erwartete Nachweise

Nach dem Test liegen vor:

- `ImportRun`-Zeilen je Konto mit Zeitpunkt, Status, Zaehlern (neu/unveraendert/
  Verdacht/Fehler) und Klartext (F-SYN-07).
- `ImportRunError`-Zeilen nur mit Fehlercode und Klartext, ohne Geheimnisse.
- JSON-Log auf stdout ohne PIN, TAN, Schluessel, Tokens oder Dialogdaten
  (N-SEC-02, N-OPS-05).
- Datenbankpruefung: `BankAccess.pin_encrypted` ist je nach Option leer oder
  Chiffretext, nie Klartext-PIN.
- Ein ausgefuelltes Ergebnisprotokoll (siehe unten), keine echten Kontodaten im
  Repo.

## Sicherheitsregeln waehrend des Tests

- Nur der Owner fuehrt echte Logins aus. Agents erhalten keine Zugangsdaten.
- PIN und TAN niemals in Chat, Ticket, Log, Screenshot oder Datei schreiben.
- Fehlermeldungen nur mit Code und Klartext dokumentieren, nie mit Rohdaten des
  Dialogs.
- Falsche PIN hoechstens einmal eingeben; bei Sperrgefahr sofort abbrechen.
- Keine echten Kontodaten in `tests/fixtures/`, `data/samples/` oder Artefakte
  uebernehmen.
- Bei einem Geheimnis im Log oder in einer Fehlermeldung: Test stoppen, Lauf
  sichern und den Vorfall melden (N-SEC-02).

## Offene Owner-Eingaben

- **Produkt-ID:** Die registrierte FinTS-Produkt-ID aus SPEC Abschnitt 7 muss
  bestaetigt und als `FINTS_PRODUCT_ID` gesetzt werden.
- **Bank-Endpunkte:** `fints_url` je Bank. Aktuell ist nur ING als Owner-Vorgabe
  genannt, aber unbelegt; weitere Banken und Endpunkte fehlen.
- **Fehlercodes mit Beleg:** Der echte Code der ING-90-Tage-Regel ist unbekannt
  (Platzhalter `ING90`). Ebenso fehlen belegte Quellen fuer 9800/9010 und das je
  Bank gueltige TAN-Verfahren.
- **TAN-Verfahren:** Je Zugang das tatsaechlich angebotene Verfahren notieren.
- **Bedienung:** Der Live-Test setzt eine startbare Sync-Bedienung voraus. Bis
  dahin bleibt dieses Dokument der vorbereitete Testplan.

## Stand F-AUF-07

- Die Machbarkeitspruefung ist **positiv**. python-fints 5.0.1 bietet keinen
  fertigen Abruf fuer Dauerauftraege (HKCDB) und Terminauftraege (HKCSB); die
  lesende Erweiterung im Projekt ist tragfaehig und mit Mocks getestet.
- Umgesetzt sind die Segmente HKCDB1/HICDB1/HICDBS1 und
  HKCSB1/HICSB1/HICSBS1 (Version 1) sowie die Anzeige-Vorbereitung nach
  F-AUF-02/F-AUF-05. Schreibende Auftragssegmente werden nie aufgerufen.
- **Einschraenkung:** Banken, die ausschliesslich HKCDB2/HICDB2 anbieten, fallen
  auf "nicht unterstuetzt" zurueck. Der Sync gilt dann weiterhin als erfolgreich
  und zeigt "Keine Auftragsdaten von der Bank verfuegbar" (F-AUF-05).
- Loest der Auftragsabruf selbst eine TAN aus, liefert die Fortsetzung nur die
  Dauerauftraege; Terminauftraege folgen im naechsten Sync.
- F-AUF-01 bis F-AUF-04 und F-AUF-06 bleiben damit aktiv (bedingt), die Anzeige
  entsteht in der Oberflaeche. Einzelheiten und Quellen: `decisions.md`,
  Eintrag F-AUF-07.

## Ergebnisprotokoll

| Datum | Bank/BLZ | Testfall | Erwartet | Beobachtet | Status | Nachweis |
|---|---|---|---|---|---|---|
| | | Erstsync | erfolgreich, keine Dubletten | | | |
| | | TAN | wartet_auf_tan -> erfolgreich | | | |
| | | App-Freigabe | erfolgreich nach Freigabe | | | |
| | | Batch | ein Dialog, Zeile je Konto | | | |
| | | Abbruch | abgebrochen, Sperre frei | | | |
| | | Timeout | zeitueberschreitung | | | |
| | | ING 90 Tage | Klartext, kein Retry | | | |
| | | Falsche PIN | Klartext, kein Retry | | | |
| | | PIN-Option | Chiffretext bzw. leer | | | |

Das Protokoll wird ohne echte Kontodaten abgelegt; Nachweise nur redigiert.

## Nach dem Test

1. Testergebnis und gefundene Codes in `handoff.md` und, bei Mapping-Aenderungen,
   in `decisions.md` festhalten.
2. Belegte Fehlercodes in das Fehler-Mapping uebernehmen (F-SYN-09) und den
   Platzhalter `ING90` ersetzen.
3. Testdatenbank verwerfen oder anonymisieren; Schluesseldatei getrennt sichern.
4. Abweichungen von diesem Plan als Improve-Kandidat eintragen.
