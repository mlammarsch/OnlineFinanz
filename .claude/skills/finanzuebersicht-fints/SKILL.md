---
name: finanzuebersicht-fints
description: Domaenenwissen fuer Finanzuebersicht - FinTS-Sync mit python-fints (PIN/TAN, Dialog pausieren und fortsetzen, camt/MT940, Vormerkungen, Dauerauftraege), Bankbesonderheiten und Fehlercodes (ING, Consorsbank, DKB, Sparkasse, N26, Easybank, PayPal), Import/Export CSV/JSON/Markdown mit Mapping und Vorlagen, Dublettenerkennung (Fingerprint, Multimengen-Abgleich). Aktivieren bei FinTS, TAN, Sync, Bankfehler, CSV-Import, JSON-Export, Markdown-Export, Dubletten.
---
# finanzuebersicht-fints

Lies je Aufgabe nur die passende Referenz. Verbindliche Anforderungen stehen in `SPEC.md`.

| Aufgabe | Referenz |
|---|---|
| Sync, PIN/TAN, Dialog pausieren/fortsetzen, Saldo/Umsaetze/Vormerkungen, Auftragsabruf, Produkt-ID, Mocks | `references/fints.md` |
| Bankspezifisches Verhalten, Endpunkte, Fehlercode -> Klartext -> Aktion, Fehler-Mapping-Daten | `references/banken.md` |
| CSV/JSON-Import, Export-Profile, Kodierung, Formate, Mapping, Vorlagen, PayPal | `references/csv.md` |
| Fingerprint, Normalisierung, Multimengen-Abgleich, Verdachtsdubletten, Vormerkungen | `references/dubletten.md` |

Regeln, die immer gelten:
- Nur lesende FinTS-Vorgaenge. Ueberweisung, Lastschrift und andere schreibende Geschaeftsvorfaelle werden nie aufgerufen.
- Keine echten Bankzugriffe durch Agents. Tests nutzen Mocks der python-fints-Klassen (D8).
- Keine PIN, TAN, Schluessel oder Rohdaten von Zugangsdaten in Logs, Fehlern, Protokollen oder Fixtures (N-SEC-02).
- Angaben ohne Quelle sind als "Owner-Vorgabe, unbelegt" gekennzeichnet. Nicht als Tatsache weitergeben, vor dem Einsatz belegen.
