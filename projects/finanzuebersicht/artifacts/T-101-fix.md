# T-101 Fix: Monotone Event-IDs je Client (open/emit-Race)

Branch `m4/develop`. Risiko medium. T-101 bleibt offen bis zur unabhaengigen
Wiederholungspruefung. Kein Push, keine echten Zugangsdaten, keine externe
Schreibaktion.

## Befund (Verifier)

In `backend/finue/services/events.py` war die Reihenfolge je Client nicht
garantiert:

- `SseBroadcaster.open()` legte die neue `SseConnection` **innerhalb** des
  Broadcaster-Locks in `_clients` ab (damit sofort sichtbar), reihte die
  Ringpuffer-Nachlieferung bzw. einen `resync`-Rahmen aber **ausserhalb** des
  Locks ein.
- `_on_event()` vergab die ID zwar unter dem Lock, stellte den Rahmen an die
  Clients aber **ausserhalb** des Locks zu.

Folgte ein `emit` zwischen Lock-Freigabe und Nachlieferung, erhielt der neue
Client die hoehere ID vor den niedrigeren Nachlieferungs-IDs. Die IDs waren
damit nicht mehr monoton (die UI haette auf einem `resync`-Pfad die falsche
Reihenfolge gesehen). F-CON-04 verlangt einen verlaesslichen Strom.

## Fix

`open()` und `_on_event()` serialisieren ID-Vergabe, Nachlieferung und
Zustellung unter **demselben** Broadcaster-Lock:

- `open()` plant Nachlieferung/`resync` und reiht sie unter dem Lock ein; erst
  **danach** wird die Verbindung in `_clients` sichtbar.
- `_on_event()` vergibt die ID und stellt den Rahmen an alle Clients unter dem
  Lock zu.

`SseConnection.offer` blockiert nicht (`put_nowait`, bei Ueberlauf
`drain` statt Warten), daher bleibt die Zustellung unter dem Lock nicht
blockierend. Die Lock-Reihenfolge bleibt **Broadcaster -> Connection**; kein
Pfad haelt einen Connection-Lock und wartet auf den Broadcaster-Lock, also kein
Deadlock. Keine Sync-Runner- oder Payload-Erweiterung.

## Tests (`tests/unit/test_sse.py`, +2, jetzt 16)

- `test_parallele_emits_liefern_monotone_ids_je_client` (F-CON-04):
  4 Threads mit je 200 `emit`-Aufrufen (Barrier-Start, GIL-Umschaltintervall
  auf 1 us gesenkt); zwei verbundene Clients muessen lueckenlos und streng
  steigend die IDs 1..800 sehen.
- `test_open_race_haelt_ids_je_client_monoton` (F-CON-04, Reconnect):
  Die erste Zustellung (Nachlieferungsrahmen) wird angehalten, waehrend ein
  zweiter Thread ein Ereignis veroeffentlicht; nach dem Freigeben muss die
  Verbindung `[2, 3, 4]` sehen, nicht `[4, 2, 3]`. Beide Threads werden mit
  `join(timeout=...)` abgeschlossen (kein Deadlock); am Ende
  `broadcaster.close()` mit `bus.subscriber_count == 0` (kein Listener-Leck).

## Evidenz

- `uv run pytest tests/unit/test_sse.py -q` -> **16 passed**.
- `uv run pytest tests/unit/test_events.py tests/unit/test_api_contract.py -q`
  -> 22 passed/1 failed; der Fehlschlag ist **fremd** (siehe unten).
- `uv run pytest tests/unit -q` -> **1 failed, 1598 passed, 1 skipped**; der
  alleinige Fehlschlag ist `test_api_contract.py::test_alle_api_routen_sind_registriert`
  durch die fuenf **verlorenen T-091-Vertragseintraege** (`/api/banks/lookup`,
  `/api/bank-accesses` ...). Dokumentiert im T-096-Handoff-Eintrag; nicht durch
  T-101 verursacht.
- `uv run ruff check backend tests scripts` -> All checks passed.
- `uv run ruff format --check backend tests scripts` -> 210 files already formatted.
- `uv run mypy backend` -> Success (101 source files).
- **Mutationstest:** `backend/finue/services/events.py` auf den committeten
  Stand (`git show HEAD:...`) zurueckgesetzt -> beide neuen Tests rot
  (`test_open_race...`: `At index 0 diff: 4 != 2`,
  `test_parallele...`: falsche Reihenfolge). Ohne den Fix sind die Tests
  wirksam; mit dem Fix gruen.

## Offen

- T-101 bleibt **offen** bis zur unabhaengigen Wiederholungspruefung durch
  `verifier`; kein `task-done`.
- Kein Push.
