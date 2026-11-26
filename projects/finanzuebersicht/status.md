# Status finanzuebersicht

_generated 2026-11-26T13:46:43Z_

Tasks: pending=0, running=0, done=57, failed=0, blocked=0, cancelled=2

Verified complete: 57/59; retry rate 0.05; learnings 45; events 242

## now

- (empty)

## next

- (empty)

## blocked

- (empty)

## improve

- [ ] hx.py: --verify mit erwartetem Exit-Code (z. B. expect-fail) unterstuetzen
- [ ] Log-Filter: passphrase, otp, jwt, csrf, Plurale und mehrzeilige Container ergaenzen
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
- [ ] scripts/validate.py: Regel ergaenzen, dass jeder '# type: ignore[...]' eine nachgestellte Begruendung tragen muss, sonst Stufe lint rot (verhindert undokumentierte/pauschale ignores, siehe T-041).
- [ ] scripts/validate.py: IMPLEMENTED['m1'] auf STAGE_M1 kuerzen (run_stage haengt die m0-Kette bereits selbst an), verhindert doppelte Check-Ausgabe bei jeder kuenftigen mN-Stufe
- [ ] Vor ImportRun-Tasks Statuswerte aus SPEC und Modell als expliziten Vorab-Check aufnehmen; keine Implementierung bei Vokabular-Konflikt.
- [ ] scripts/validate.py: check_roundtrip_import bildet die Fixture-Konten (1/2) beim Import bewusst auf ein Zielkonto ab; fuer M4 den Mehrkonto-Roundtrip (Kontozuordnung, Gruppen) als eigenen Check ergaenzen. Ausserdem deckt der Check nur JSON v1 ab; CSV-Roundtrip mit Vorlage folgt mit dem CSV-Export (M4).
- [ ] scripts/validate.py: den eingebetteten ROUNDTRIP_SCRIPT-String bei wachsendem Umfang in ein eigenes, mit uv ausgefuehrtes Pruefskript unter scripts/ auslagern (bleibt testbar, vermeidet grosse String-Literale in validate.py).
- [ ] scripts/validate.py: check_lint um 'uv run ruff format --check backend tests scripts' ergaenzen, damit Formatdrift nicht erst spaeter auffaellt.

