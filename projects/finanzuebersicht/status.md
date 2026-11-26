# Status finanzuebersicht

_generated 2026-11-26T20:30:22Z_

Tasks: pending=22, running=0, done=86, failed=0, blocked=3, cancelled=4

Verified complete: 86/115; retry rate 0.04; learnings 75; events 420

## now

- (empty)

## next

- [ ] T-089 M4: Konto loeschen mit vollstaendiger Bereinigung (F-KTO-07)
- [ ] T-090 M4: Banken-Stammdaten und Icon-Upload (F-KTO-10, F-KTO-11)
- [ ] T-092 M4: Such- und Filterabfrage als gemeinsamer Baustein plus Such-API (F-SUC-01..07, F-ABS-04/05)
- [ ] T-095 M4: Abstimmungsuebersicht je Konto und Gruppe (F-ABS-03)
- [ ] T-100 M4: REST fuer Datei-Import, Importvorlagen, Lauf-Protokoll und lesende Auftraege

## blocked

- [ ] T-094 M4: Kennzeichen, Notiz, externe Referenz und Batch mit Setz-Semantik und Versionspruefung (F-ABS-01/02/05/06, F-CON-01..03): Abschlusstask: Umsetzung in T-108,T-109,T-110. Freigabe (task-set pending) erst wenn alle drei done; dann nur Gesamtverifikation, kein Neubau
- [ ] T-099 M4: Sync-REST mit TAN-Endpunkten und DB-Session-Freigabe waehrend der TAN-Pause: Abschlusstask: Umsetzung in T-111,T-112. Freigabe (task-set pending) erst wenn beide done; dann nur Gesamtverifikation, kein Neubau
- [ ] T-106 M4: API-E2E-Szenario gegen echten Uvicorn-Prozess mit Fake-Bank und SSE: Abschlusstask: Szenarien in T-113,T-115. Freigabe (task-set pending) erst wenn beide done; Rest: validate.py Check api-e2e, 3x gruen
- [ ] T-091 M4: FinTS-Anlage-Assistent im Backend (F-KTO-05a): waiting on T-088,T-099
- [ ] T-093 M4: Buchungsliste mit Vormerkungen, Tagessalden, Gruppenansicht und Keyset-Paginierung (F-BUC-01..06): waiting on T-088,T-092
- [ ] T-096 M4: Aufloesung von Verdachtsdubletten - Zusammenfuehren, Rueckgaengig, Ignorieren (F-DUP-05): waiting on T-094
- [ ] T-097 M4: Export-Service JSON und Markdown mit Umfang, Filter und Dateiname (F-EXP-01, 02, 04, 05): waiting on T-092,T-085
- [ ] T-098 M4: CSV-Export mit Optionen, Exportprofilen und passender Importvorlage (F-EXP-03, F-EXP-06): waiting on T-097
- [ ] T-101 M4: Aenderungsbenachrichtigung per Server-Sent Events /api/events (F-CON-04): waiting on T-086,T-099
- [ ] T-103 M4: Performance-Block mit 100.000 Buchungen (N-PERF-01, F-BUC-06): waiting on T-093
- [ ] T-104 M4: Concurrency-Block (F-CON-01..05, N-DB-04): waiting on T-088,T-094,T-099,T-101
- [ ] T-105 M4: Roundtrip-Block erweitern (Mehrkonto-JSON, CSV mit Vorlage, alle Schema-Versionen) (F-EXP-06/07, F-ABS-06): waiting on T-094,T-096,T-098,T-100
- [ ] T-107 M4: Abnahme und Aufraeumen (validate.py m4 inkl. roundtrip, perf, concurrency, api-e2e): waiting on T-084,T-085,T-086,T-087,T-088,T-089,T-090,T-091,T-092,T-093,T-094,T-095,T-096,T-097,T-098,T-099,T-100,T-101,T-102,T-103,T-104,T-105,T-106
- [ ] T-109 M4 T-094-B: Notiz ersetzen/anhaengen und externe Referenz (F-CON-01/02, F-ABS-05): waiting on T-108
- [ ] T-110 M4 T-094-C: Batch mit Ergebnis je Buchung und Regression Sync/Re-Import (F-CON-03, F-ABS-02/06, F-CON-05): waiting on T-109
- [ ] T-113 M4 T-106-A: API-E2E Rahmen und Szenario Teil 1 (Konten, Import, Liste, Suche, Kennzeichen, Abstimmung, Archiv, Icon): waiting on T-088,T-090,T-093,T-095,T-100,T-101,T-110
- [ ] T-115 M4 T-106-B: API-E2E Szenario Teil 2 (FinTS-Assistent, Sync mit TAN und SSE, Dubletten, Export, Loeschen, Sitzungen, Leckage-Scan): waiting on T-113,T-089,T-091,T-096,T-098,T-102,T-112

## improve

- [ ] hx.py: --verify mit erwartetem Exit-Code (z. B. expect-fail) unterstuetzen
- [ ] Log-Filter: deutsche Plural-/Umlautformen (z. B. "Passwörter") erfasst der ASCII-Wortregex nicht; bei Bedarf Unicode-Wortgrenzen oder explizite Formen ergaenzen.
- [ ] Bankfehler-Fremdtext (F-SYN-05/N-SEC-02): `resolve_error` maskiert nur labelbare Geheimnisse im Originaltext. Unmarkierte Geheimniswerte bleiben unerkannt; falls der Owner F-SYN-05 enger auslegt, fuer unbekannte Codes nur Code plus festen Klartext protokollieren.
- [ ] engine.py: make_url statt str.replace, StaticPool fuer :memory:, Tests fuer FK-Erzwingung und mehrere Connections; Naming-Convention uq/ix auf column_0_N_* pruefen
- [ ] backend/finue/models/__init__.py: Sammelimport aller Modellmodule ergaenzen (duplicate, standing_order, audit, bank, ...), sonst fehlen Tabellen bei Alembic-Autogenerate (Base.metadata unvollstaendig ohne expliziten Import) - vor T-027/T-028 beachten
- [ ] Constraint-Namensduplikation pruefen: ck_<table>_ck_<table>_<name> entsteht bei CheckConstraint mit bereits vollem Namen (betrifft import_run.py und security.py) - Naming-Convention in db/base.py bzw. Aufrufstellen vereinheitlichen vor Alembic-Autogenerate (T-028)
- [ ] T-025: CheckConstraint retention_count >= 1 ergaenzen, verhindert versehentliches Loeschen aller Backups bei Wert 0
- [ ] T-028/env.py: kein Test fuehrt einen echten Alembic-Migrationslauf (upgrade/downgrade) gegen eine Temp-DB aus, nur ScriptDirectory/Config-Ladbarkeit. Bei T-029 (erste Migration) oder T-031 (Migrationstests) nachholen.
- [ ] backend/finue/migrations/env.py: render_item-Hook fuer UtcDateTime ergaenzen (autogen_context.imports.add('from finue.db.mixins import UtcDateTime')), damit Autogenerate den Import automatisch schreibt und Migrationsdateien ohne Handnachbearbeitung laufen.
- [ ] test_alembic_config.py test_initial_migration_is_single_head haertet den Head-Wert hart - vor T-031/T-032 auf generische Pruefung (genau ein Head, down_revision pruefen) umstellen, sonst bricht der Test bei der naechsten Migration
- [ ] test_migration_0001.py um alembic.autogenerate.compare_metadata Vergleich erweitern fuer strengere Typ- und FK-Pruefung, nicht nur Tabellen- und Spaltennamen
- [ ] tests/unit/helpers_migration.py, test_migration_0001.py, test_alembic_config.py bauen Alembic-Config noch ohne configure_logger=False - aktuell folgenlos, da neue Logging-Tests ihren Root-Handler-Zustand selbst sichern, aber bei kuenftigen Logger-abhaengigen Tests potenzielle Testreihenfolge-Kopplung. Bei Gelegenheit vereinheitlichen.
- [ ] test_smoke.py: expliziten Assert ergaenzen dass die Alembic-Logzeile (logger startswith alembic, Running upgrade) im JSON-Eintrag erscheint - staerkerer direkter Beweis fuer den T-033-Logging-Fix statt nur indirekter Ableitung
- [ ] Nach Alembic-Autogenerate die neue Migrationsdatei immer mit 'uv run ruff format' formatieren (Post-Generate-Schritt vor dem Commit), damit generierte Dateien den lint-Block von scripts/validate.py nicht rot machen.
- [ ] Vor ImportRun-Tasks Statuswerte aus SPEC und Modell als expliziten Vorab-Check aufnehmen; keine Implementierung bei Vokabular-Konflikt.
- [ ] scripts/validate.py: check_roundtrip_import bildet die Fixture-Konten (1/2) beim Import bewusst auf ein Zielkonto ab; fuer M4 den Mehrkonto-Roundtrip (Kontozuordnung, Gruppen) als eigenen Check ergaenzen. Ausserdem deckt der Check nur JSON v1 ab; CSV-Roundtrip mit Vorlage folgt mit dem CSV-Export (M4).
- [ ] scripts/validate.py: check_lint/run_external geben bei Fehlschlag nur die letzte Ausgabezeile aus; bei Formatdrift nennt der Check dadurch nicht die betroffenen Dateien. Bei Bedarf ruff-Ausgabe mehrzeilig durchreichen (T-084).
- [ ] Owner-Live-Test FinTS (M3) steht aus: `FINTS_PRODUCT_ID` setzen und das Protokoll in `docs/live-test-fints.md` gegen die echte Bank ausfuehren; dabei ING-90-Tage-Code, 9800/9010 und HKCDB2/HICDB2 belegen. Ohne diesen Lauf bleiben F-SYN-05/06 und F-AUF-04/05/07 nur mit Mocks/Fakes belegt.
- [ ] tests/unit/test_fints_capabilities.py ist an python-fints 5.0.1 gebunden: bei jedem Pin-Sprung laufen lassen und den F-AUF-07-Eintrag in decisions.md samt Quellzeilen aktualisieren. Der Test prueft auch das Ergebnis-Kennzeichen, damit Doku und Code nicht auseinanderlaufen.
- [ ] T-075: python-fints hat keinen oeffentlichen generischen Kommando-Weg; die lesende HKCDB/HKCSB-Erweiterung nutzt geschuetzte Helfer (`_find_highest_supported_command`, `_fetch_with_touchdowns`). Eine duenne, getestete Kapsel bauen und die Capability-Tests als Waechter beim Pin-Upgrade nutzen.
- [ ] Bestandszaehlung fuer F-DUP-02 ist an drei Stellen dupliziert (duplicate._load_existing_state, sync_persist._load_existing_counts, import_preview._load_existing_counts); ein gemeinsamer Helfer mit Statusparameter und ein Paritaetstest (Vorschau-Status == reconcile-Ergebnis fuer dieselbe Lieferung) verhindert kuenftige Drift.
- [ ] sync_service/9800: SPEC F-SYN-06 fuehrt 9800 als wiederholbar, der Adapter meldet es laut T-060 als PinWrongError. Der dokumentierte Shim in `_fetch_with_retry` loest das zur Laufzeit; langfristig Adapter-Einstufung, Fake-Skript `consors_9800` und T-060-Beleglage in `decisions.md` mit SPEC F-SYN-05/06 in Uebereinstimmung bringen.
- [ ] T-075: die HKCDB/HKCSB-Erweiterung unterstuetzt nur Segmentversion 1. Vor dem Owner-Live-Test HKCDB2/HICDB2 (DauerDetails4 mit optionaler Ausfuehrungszeit) aus `hbci-300.xml` ergaenzen, sonst fallen Banken, die nur Version 2 anbieten, auf `unsupported` zurueck. Dabei `_find_highest_supported_command(HKCDB1, HKCDB2)` und einen gemischten Versionstest ergaenzen.
- [ ] T-075: loest HKCDB selbst eine TAN aus, wird HKCSB nach der Freigabe nicht mehr im selben Aufruf gelesen. Ein Resume-Hook oder ein automatischer Folgeabruf sollte beide Auftragsarten in einem Sync liefern; aktuell holt der naechste Sync HKCSB nach.
- [ ] hx.py: Befehl task-deps (depends_on bestehender Tasks aendern) und Bedingung cancelled/superseded als erfuellte Abhaengigkeit ergaenzen
- [ ] backend/finue/api/health.py nutzt fuer `scheduler_required` weiterhin das globale `get_settings()` statt der in `create_app` injizierten Settings; in Tests mit eigenen Settings kann die Readiness dadurch von der App-Konfiguration abweichen. Bei Gelegenheit `app.state.services.settings` verwenden (T-086-Fund).
- [ ] Vertragstest: FastAPI 0.165 bindet Router verschachtelt ein (`_IncludedRouter` mit `original_router`), daher muss `tests/unit/test_api_contract.py` die Routen rekursiv einsammeln. Bei einem FastAPI-Sprung die Hilfsfunktion `_iter_api_routes` pruefen (T-086).
- [ ] helpers_migration.upgrade_preserves_data um eine Child-Table-Pruefung erweitern (Batch-Umbau darf Kindzeilen nicht loeschen), sobald eine zweite Tabelle mit FK-Kindern in einem Batch umgebaut wird.
- [ ] search_text: F-SUC-06 nennt "LIKE auf einer normalisierten Suchspalte plus Indizes". Ein Btree-Index hilft bei `LIKE '%begriff%'` (fuehrendes Wildcard) nicht. Im Perf-Block T-103 (100.000 Buchungen) die Suche messen und erst dann entscheiden, ob eine eigene Index-/Strategiestufe noetig ist; bis dahin keine unnuetze Indexmigration.
- [ ] Migration 0004 enthaelt eine eingefrorene Kopie der Suchtext-Normalisierung; `tests/unit/test_search_text.py::TestMigrationParity` erzwingt die Gleichheit mit `services/search_text.py`. Bei jeder inhaltlichen Aenderung der Laufzeitregel (Felder, Reihenfolge, casefold/Umlaute) bewerten: bestehende Migration unveraendert lassen, ein erneuter Backfill braucht eine neue Migration.
- [ ] T-112 (Sync-REST/Runner): denselben Vertrag einhalten - im Runner-/Event-Zustand ueber eine Pause nur IDs/Werte halten, keine DB-Session/ORM-Objekte; im Runner-Test die Pool-Pruefung (Engine.pool.checkedout()==0) waehrend AWAITING_TAN wiederholen.
- [ ] T-088 Fund (API-Struktur): FastAPI 0.165 setzt bei verschachtelten include_router das Elternpraefix NICHT in APIRoute.path; deshalb bringt jeder Fachrouter prefix="/api" und require_user selbst mit (siehe decisions.md T-088). Neue Fachrouter im selben Muster anlegen, sonst findet der Vertragstest die Pfade nicht.
- [ ] T-088 Fund (Modell-Typisierung): Account.balance_at ist als Mapped[UtcDateTime | None] deklariert (statt Mapped[datetime | None] wie in TimestampMixin); Schreibzugriffe brauchen daher setattr/cast-Umwege (services/accounts.py, api-DTO). Ebenso ImportRun.finished_at. Annotationen an das Mixin-Muster angleichen, dann entfallen die Umwege.

