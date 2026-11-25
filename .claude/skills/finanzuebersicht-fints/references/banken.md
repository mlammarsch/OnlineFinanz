# Banken und Fehler-Mapping

Kennzeichnung: **belegt (Quelle)** oder **Owner-Vorgabe, unbelegt**. Alles "unbelegt" vor produktiver Nutzung pruefen.
Endpunkte nur mit Quelle vorbelegen (F-KTO-10): Stand M0 hat nur der Owner Angaben geliefert, daher ist **kein Endpunkt
vorbelegbar**, bis eine Quelle (z. B. die FinTS-Institutsliste der Deutschen Kreditwirtschaft) eingetragen ist.

## Banken (SPEC Abschnitt 3)

| Bank | Quelle | Angaben | Status |
|---|---|---|---|
| ING | FinTS | Endpunkt `https://fints.ing.de/fints/`. Zugriff setzt Online-Banking-Login innerhalb 90 Tage voraus. Klartext: "Bitte einmal im ING Online-Banking einloggen." | Owner-Vorgabe, unbelegt |
| Consorsbank | FinTS | Instabil. 9800 und 9010 regelmaessig, wiederholte TAN-Anforderungen | Owner-Vorgabe, unbelegt |
| DKB | FinTS | Stabil, Push-TAN (App-Freigabe, `decoupled`) | Owner-Vorgabe, unbelegt |
| Sparkasse (Darlehen) | FinTS | Endpunkt traegt der Nutzer ein. Umsaetze ggf. eingeschraenkt, Konto nur mit Saldo zulaessig | Owner-Vorgabe, unbelegt |
| N26 | CSV | Kein FinTS | Owner-Vorgabe, unbelegt |
| Easybank (AT, Kreditkarte) | CSV | Kein FinTS | Owner-Vorgabe, unbelegt |
| PayPal | CSV | Kein FinTS, Privat-APIs abgekuendigt | Owner-Vorgabe, unbelegt |

Beispieldateien fuer die CSV-Banken liefert der Owner (SPEC Abschnitt 7, `data/samples/`, nicht im Repo).

## Fehlercode-Mapping (Standarddaten, F-SYN-05/09)

Struktur eines Eintrags: Code, optional BLZ, Klartext, Handlungsempfehlung, wiederholbar (ja/nein), Standard-Kennzeichen.
Eigene Eintraege haben Vorrang. Unbekannte Codes zeigen den Originaltext der Bank.

| Code | Klartext | Aktion | wiederholbar | Status |
|---|---|---|---|---|
| 9800 | Dialog abgebrochen | Automatisch erneut versuchen (hoechstens 2 Versuche, neuer Dialog, Wartezeit) | ja | Owner-Vorgabe, unbelegt |
| 9010 | Verarbeitung nicht moeglich | Wie 9800, danach spaeter erneut | ja | Owner-Vorgabe, unbelegt |
| ING, Login zu alt (Code unbekannt) | FinTS-Zugriff nicht erlaubt | "Bitte einmal im ING Online-Banking einloggen." | nein | Owner-Vorgabe, Code unbelegt: bei erstem Live-Test ermitteln |
| falsche PIN | PIN nicht akzeptiert | PIN pruefen, bei Wiederholung droht Sperre | nein | Code unbelegt |
| gesperrter Zugang | Zugang gesperrt | Bank kontaktieren bzw. im Online-Banking entsperren | nein | Code unbelegt |

Codes fuer falsche PIN und Sperre werden nicht aus dem Gedaechtnis eingetragen. Sie werden in M3 aus einer Quelle
(FinTS-Spezifikation, python-fints-Quelltext/Doku) belegt oder aus Live-Tests des Owners ergaenzt
(`docs/live-test-fints.md`). Bis dahin greift der Fallback auf den Originaltext der Bank.
