# FinTS mit python-fints

Quelle der API-Angaben: Context7 `/websites/python-fints_readthedocs_io_en` (python-fints.readthedocs.io, abgerufen
2026-11-25). Version in M3 pinnen und die Signaturen gegen die gepinnte Version nochmals pruefen.

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
8. TAN-Bedarf schon bei der Dialog-Initialisierung (`init_tan_response`): gleicher Weg. Name und Verhalten in M3 gegen die
   gepinnte Version belegen (in dieser Recherche nicht belegt: **unbelegt**).

## Auftragsabruf (F-AUF-07)

python-fints dokumentiert keinen fertigen Abruf fuer Dauerauftraege (HKCDB) und Terminauftraege (HKCSB) (Owner-Vorgabe,
unbelegt). Erster Task in M3: in der gepinnten Version nach Unterstuetzung suchen. Fehlt sie: lesende Erweiterung im
Projekt mit Mocks, sonst F-AUF-05 (Hinweis "Keine Auftragsdaten von der Bank verfuegbar"). Ergebnis in `decisions.md`.
Schreibende Funktionen wie `sepa_transfer` werden nie aufgerufen (die Doku nutzt sie nur als Beispiel fuer die TAN-Mechanik).

## Mock-Dialogskripte (D8)

Happy-Path, TAN, App-Freigabe (mehrfach `NeedTANResponse`, dann Erfolg), ING-90-Tage, Consorsbank 9800/9010, falsche PIN,
Saldo ohne Umsaetze, Auftragsbestand vorhanden/leer/nicht unterstuetzt. Mock-Ebene: die python-fints-Klassen, nie das Netz.
Fixtures enthalten ausschliesslich erfundene Daten (Test-IBANs, erfundene Namen).
