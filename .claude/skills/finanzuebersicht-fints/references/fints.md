# FinTS mit python-fints

Quelle der API-Angaben: Context7 `/websites/python-fints_readthedocs_io_en` (python-fints.readthedocs.io, abgerufen
2026-11-25) und - fuer die mit **(5.0.1)** markierten Punkte - der Quelltext der gepinnten, installierten Version
python-fints 5.0.1 (`pyproject.toml`, `uv.lock`, offline geprueft in `tests/unit/test_fints_capabilities.py`, T-060).
Bei einem Versionssprung die Capability-Tests erneut laufen lassen.

## Client und Dialog

- `FinTS3PinTanClient(bank_identifier, user_id, pin, server, customer_id=None, tan_medium=None, *args, **kwargs)`.
  `product_id=...` als Schluesselwort (Quickstart). Die Produkt-ID kommt aus `FINTS_PRODUCT_ID` (Registrierung bei der
  Deutschen Kreditwirtschaft; "kostenlos" ist Owner-Vorgabe, unbelegt; Owner-Eingabe laut SPEC Abschnitt 7). `from_data=` stellt einen Client wieder her.
- Dialog: `with client:` oeffnet einen Dialog und beendet ihn beim Verlassen, ausser er wurde mit `pause_dialog()` eingefroren.
- TAN-Verfahren: `client.get_tan_mechanisms()` liefert `{security_function: TwoStepParameters}`.
  `client.set_tan_mechanism(security_function)` setzt es. Nicht moeglich bei stehendem Dialog (wirft Exception).
- Konten: `client.get_sepa_accounts()` (SEPAAccount-Liste, im Quickstart der Doku; Signatur in M3 gegen die gepinnte Version pruefen).

## Abruf (nur lesend)

| Aufruf | Ergebnis | Quelle |
|---|---|---|
| `get_balance(account)` | `mt940.models.Balance` | reading.html |
| `get_transactions_xml(account, start_date, end_date, supported_camt_messages)` | Tupel `(booked_streams, pending_streams)`, je Liste von XML-Bytestrings, gebuchte und vorgemerkte getrennt | reading.html |
| `get_transactions(account, start_date, end_date, include_pending=False)` | Liste `mt940.models.Transaction` bzw. `fints.models.Transaction`. Bevorzugt MT940, Fallback camt (`HKCAZ1`) | reading.html, client.py |

Entscheidung D2: camt bevorzugen (liefert Vormerkungen getrennt). Fallback MT940 mit `include_pending=True`
(Vormerkungen koennen Felder wie den Buchungstag nicht haben). Jeder Abruf kann statt des Ergebnisses eine
`NeedTANResponse` liefern: immer mit `isinstance` pruefen (client.py zeigt das fuer die interne XML-Variante).

## TAN-Ablauf in der Web-App (D3, Pause/Resume)

Quelle: tans.html, client.py `pause_dialog`/`resume_dialog`.

1. Abruf liefert `NeedTANResponse` (`response.challenge` = Text, `response.decoupled` = App-Freigabe).
2. Im selben `with client:`-Block: `dialog_data = client.pause_dialog()`. Danach keine Kommandos mehr im Dialog.
   Beim Verlassen des Blocks bleibt der Dialog offen.
3. `client_data = client.deconstruct()` und `tan_data = response.get_data()` sichern.
4. Fortsetzen: `client = FinTS3PinTanClient(..., from_data=client_data)`, `tan_request = NeedRetryResponse.from_data(tan_data)`,
   `with client.resume_dialog(dialog_data): response = client.send_tan(tan_request, tan)`.
   `resume_dialog` wirft, wenn schon ein Dialog steht.
5. `send_tan(challenge, tan)`: Bei `challenge.decoupled == True` wird `tan` ignoriert (leer lassen). Ist die Freigabe in der
   App noch nicht erfolgt, liefert der Aufruf erneut eine `NeedTANResponse`. Daraus folgt das Abfragen im Intervall bis zur
   Freigabe oder zum Timeout (5 Minuten).
6. Sicherheit: `dialog_data`, `client_data`, `tan_data` duerfen nie aus nicht vertrauenswuerdiger Quelle stammen
   (`from_data` ist nicht fuer fremde Daten gedacht). Nur im Arbeitsspeicher halten, nie auf Platte oder in den Browser (N-SEC-03).
   Ein Worker-Prozess, weil die Job-Registry im Speicher liegt.
7. Timeout, Abbruch: Dialog verwerfen, Job auf `zeitueberschreitung` bzw. `abgebrochen`, je Konto in eigener Transaktion speichern (F-SYN-10).
8. TAN-Bedarf schon bei der Dialog-Initialisierung: **belegt (5.0.1)**. `FinTS3Client.init_tan_response` ist ein
   Attribut (kein Methodenname), initial `None` (`client.py:196`). Verlangt schon der Dialogaufbau eine TAN, setzt
   `fints/dialog.py:90` `client.init_tan_response = NeedTANResponse(None, <HITAN-Segment>, '_continue_dialog_initialization', ...)`;
   bei Code `3955` wird zusaetzlich `decoupled = True` gesetzt. Fortsetzen wie oben ueber `pause_dialog`/`deconstruct`
   bzw. `resume_dialog`/`send_tan`; `send_tan` ignoriert `tan` bei `decoupled == True` (`client.py:1534`).

## Auftragsabruf (F-AUF-07)

**Ergebnis T-060: tragfaehig, aber nicht eingebaut.** Geprueft gegen python-fints 5.0.1:
- Kein fertiger HKCDB- (Dauerauftraege) oder HKCSB- (Terminauftraege) Abruf; `HKCDB`/`HKCSB` kommen in der
  installierten Distribution nicht vor (siehe `tests/unit/test_fints_capabilities.py`).
- Lesende Erweiterung ist technisch tragfaehig: eigene Segmente als Unterklasse von `fints.segments.base.FinTS3Segment`
  (Typ/Version aus dem Klassennamen, automatische Registrierung, `segments/base.py:26,50`), Versionswahl ueber
  `FinTS3Client._find_highest_supported_command` und die BPD-Parametersegmente `HICDBS`/`HICSBS` (`client.py:504`),
  Abruf und Antwortauswertung ueber `_fetch_with_touchdowns` (`client.py:483`) mit `FinTSMessage.response_segments`,
  Muster `get_transactions_xml` (`client.py:620`).
- Umsetzung (T-075): Segmentklassen `HKCDB4`/`HICDB4`, `HKCSB4`/`HICSB4` mit Feldern aus der offiziellen
  FinTS-Spezifikation (nicht raten), rein lesende Fetch-Funktionen, Tests mit Fake-Client (D8). Aufwand ca. 1-2 Tage.
- Grenze: der generische Kommando-Weg nutzt geschuetzte (`_`)Helfer; durch den exakten Pin abgesichert. Ohne
  FinTS-Feldvorlage bleibt F-AUF-05 ("Keine Auftragsdaten von der Bank verfuegbar") der Laufzeitzustand.
Schreibende Funktionen wie `sepa_transfer` werden nie aufgerufen (die Doku nutzt sie nur als Beispiel fuer die TAN-Mechanik).

## Mock-Dialogskripte (D8)

Happy-Path, TAN, App-Freigabe (mehrfach `NeedTANResponse`, dann Erfolg), ING-90-Tage, Consorsbank 9800/9010, falsche PIN,
Saldo ohne Umsaetze, Auftragsbestand vorhanden/leer/nicht unterstuetzt. Mock-Ebene: die python-fints-Klassen, nie das Netz.
Fixtures enthalten ausschliesslich erfundene Daten (Test-IBANs, erfundene Namen).
