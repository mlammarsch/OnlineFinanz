# Dublettenerkennung (SPEC F-DUP-01 bis F-DUP-05)

## Normalisierung und Fingerprint (F-DUP-01)

Je Textfeld: Unicode NFC, Grossschreibung, Zeilenumbrueche entfernen (MT940 bricht nach 27/65 Zeichen um; Owner-Vorgabe aus SPEC F-DUP-01, unbelegt),
Mehrfach-Leerzeichen zusammenfassen, trimmen. Fingerprint = SHA-256 ueber die normalisierten Felder in fester
Reihenfolge mit eindeutigem Trenner: Konto, Buchungstag, Wertstellung, Betrag in Cent, Waehrung, Gegenpartei,
Gegenpartei-IBAN, Verwendungszweck. Nicht enthalten: Notiz, geprueft, abgestimmt, externe Referenz, Status.

Hinweis zum Umbruch: Zeilenumbrueche werden **entfernt** (nicht durch Leerzeichen ersetzt), damit ein bei 27 Zeichen
umbrochenes Wort identisch zum unumbrochenen Text wird. Mehrfach-Leerzeichen danach zusammenfassen.

## Multimengen-Abgleich (F-DUP-02)

Je Fingerprint f: `n_neu` = Anzahl in der Lieferung, `n_db` = Anzahl im Konto (inklusive als `zusammengefuehrt`
markierter, ihre Plaetze bleiben belegt). Einfuegen: `max(0, n_neu - n_db)` Buchungen mit Index `n_db .. n_neu-1`.
Eindeutig ueber (Konto, Fingerprint, Index). Nie loeschen, wenn die Bank weniger liefert, nur Warnung im Protokoll.

Beispiele:

| Fall | Lieferung | DB | Ergebnis |
|---|---|---|---|
| Zwei gleiche Buchungen am selben Tag (je 3,50 EUR Baeckerei) | 2x f | leer | 2 neu, Index 0 und 1 |
| Wiederholter Import derselben Datei | 2x f | 2x f | 0 neu |
| Dritte gleiche Buchung kommt spaeter | 3x f | 2x f | 1 neu, Index 2 |
| Bank liefert weniger (Zeitraum kuerzer) | 1x f | 2x f | 0 neu, 0 geloescht, Warnung |
| Zusammengefuehrte Buchung (Index 1 markiert) | 2x f | 2x f (1 markiert) | 0 neu, nichts wird neu angelegt |

## Verdachtsdubletten (F-DUP-03)

Gleiches Konto, gleicher Betrag, Buchungstag +/- 2 Tage, abweichender Text (Fingerprint verschieden). Werden importiert,
markiert und als Paar angeboten. Nie automatisch zusammengefuehrt oder geloescht. Ignorieren speichert "keine Dublette"
und schlaegt das Paar nicht erneut vor.

## Zusammenfuehren (F-DUP-05)

Der Nutzer waehlt die bleibende Buchung (Vorschlag: FinTS vor Datei, sonst die aeltere). Bankfelder der bleibenden bleiben.
Notizen beider mit Herkunftsvermerk, geprueft/abgestimmt gesetzt, wenn bei einer gesetzt. Externe Referenz: eine gesetzt ->
uebernehmen, zwei verschiedene -> Nutzer waehlt. Die andere Buchung wird `zusammengefuehrt` (unsichtbar, nicht in Summen
und Exporten), nicht geloescht. Fingerprint und Index bleiben belegt. Rueckgaengig ueber das Protokoll.

## Vormerkungen (F-DUP-04)

Nehmen nicht am Fingerprint-Abgleich teil. Jeder Sync ersetzt die Vormerkungen des Kontos komplett. Notiz und
Pruef-Flag gehen auf die gebuchte Buchung ueber, wenn genau ein Kandidat passt (Betrag, Gegenpartei, +/- 10 Tage).
Sonst bleiben sie als Hinweis im Protokoll.

## Tests (Pflicht, IDs referenzieren)

Alle Faelle der Tabelle, Wiederholimport, Umbruch-Normalisierung, Verdacht plus Ignorieren plus Rueckgaengig,
Vormerkung mit einem und mit mehreren Kandidaten.
