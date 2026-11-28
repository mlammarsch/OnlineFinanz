# T-096 Bau-Evidenz: Aufloesung von Verdachtsdubletten (F-DUP-03/04/05)

- Task: T-096, Ziel G-005, Milestone M4, Risiko medium
- Branch: m4/develop
- Datum: 2026-11-28
- Anforderungen: F-DUP-03, F-DUP-04, F-DUP-05, F-CON-01, F-CON-02, F-CON-04, F-MCP-09, N-SEC-02
- Keine echten Bankzugriffe, keine Secrets, nur erfundene Testdaten (D8).

## Umfang

- Neu `backend/finue/services/duplicate_resolution.py`:
  - `list_suspected_duplicates(session, account_ids)` - offene Verdachtspaare
    (selbes Konto, gleicher Betrag, Buchungstage +/- 2 Tage, abweichender
    normalisierter Text; gebucht, nicht zusammengefuehrt; Paare mit Entscheidung
    fehlen), Vorschlag FinTS-vor-Datei, sonst die aeltere.
  - `merge_duplicates(...)` - Bankfelder des Bleibers unveraendert, Notizen mit
    Herkunftsvermerk zusammengefuehrt, Kennzeichen gesetzt wenn bei einer gesetzt
    (F-CON-01), externe Referenz uebernommen bzw. Wahl Pflicht (422), andere
    Buchung nur `merged=true`; `versions` Pflicht (F-CON-02) -> 409 mit Stand;
    `DuplicateDecision` + Audit mit Vorher-Stand; Ereignis `duplicate.changed`.
  - `undo_merge(session, decision_id)` - stellt beide Buchungen exakt her,
    409 wenn der Bleiber seither geaendert wurde.
  - `ignore_duplicates(...)` - speichert `verworfen`, aendert den Bestand nicht.
- Neu `backend/finue/api/duplicates.py`: `GET /api/duplicates?account_id|group_id`,
  `POST /api/duplicates/merge`, `POST /api/duplicates/{decision_id}/undo`,
  `POST /api/duplicates/ignore`.
- `backend/finue/api/routes.py`: Router eingehaengt (nur T-096-Anteil).
- `tests/unit/test_api_contract.py`: vier Vertragseintraege (lesen/version/aktion/aktion).
- `tests/unit/test_duplicate_resolution.py`: 32 Tests zu allen Spiegelstrichen der DoD.
- Doku: `docs/api.md` (Abschnitt Dublettenaufloesung), `decisions.md` (T-096), `improve.md`.

## Verifikationsplan (gruen)

| Befehl | Ergebnis |
|---|---|
| `uv run pytest tests/unit/test_duplicate_resolution.py -q` | 32 passed (mit `test_api_contract.py` 45 passed) |
| `uv run ruff check backend tests scripts` | All checks passed |
| `uv run ruff format --check backend tests scripts` | 210 files already formatted |
| `uv run mypy backend` | Success: no issues found in 101 source files |

Zusatz (Regression): `uv run pytest tests/unit -q` = 1597 passed, 1 skipped.

## Belegte Spiegelstriche der DoD

- Liste mit Vorschlag FinTS-vor-Datei / aelteste: `test_offene_paare_mit_vorschlag_fints_vor_datei`,
  `test_offene_paare_vorschlag_aeltere_buchung`, `test_gruppenbereich_listet_paare_der_mitglieder`.
- F-DUP-03-Kriterien (Betrag/Tag/Text/Konto) und Vormerkungen ausgenommen:
  `test_gleicher_text_ist_kein_verdacht`, `test_betrag_oder_tag_ausserhalb_ist_kein_verdacht`,
  `test_verschiedene_konten_sind_kein_verdacht`, `test_vorgemerkte_buchung_nimmt_nicht_teil`.
- Merge: `test_merge_haelt_bankfelder_und_fuehrt_notizen_zusammen`,
  `test_merge_uebernimmt_kennzeichen_und_referenz`, `test_merge_externe_referenz_verschieden_ohne_wahl_ist_422`,
  `test_merge_externe_referenz_verschieden_mit_wahl`, `test_merge_protokoll_und_audit_mit_vorher_stand`.
- Andere Buchung unsichtbar (Liste/Suche/Summen/Salden/Abstimmung/Export), Fingerprint/Index bleiben:
  `test_merge_macht_andere_buchung_unsichtbar`.
- Versionspruefung: `test_merge_versionskonflikt_ist_409_mit_stand`, `test_merge_ohne_version_ist_422`.
- Undo exakt + Konflikt: `test_undo_stellt_beide_buchungen_wieder_her`,
  `test_undo_gibt_409_wenn_bleiber_geaendert`, `test_undo_rundreise_ohne_datenverlust`,
  `test_undo_unbekannt_ist_404`.
- Ignorieren: `test_ignore_speichert_keine_dublette_und_versteckt_das_paar`,
  `test_ignore_wirkt_beim_erneuten_import`, `test_ignore_kein_verdachtspaar_ist_422`.
- Nie automatisch / Re-Import nach Merge = 0 neu: `test_liste_aendert_nichts`,
  `test_reimport_nach_merge_ist_null_neu`.
- Ereignis: `test_merge_ereignis_duplicate_changed`.

## Offen

- Medium-Risiko: unabhaengige `verifier`-Pruefung (Review-Checkliste AGENTS.md) folgt;
  `task-done` erst danach. Kein Push.
