# T-101 Overflow-Fix: Atomare Ueberlauf-/Entnahme-Sequenz je Client

Branch `m4/develop`. Risiko medium. T-101 bleibt offen bis zur unabhaengigen
Wiederholungspruefung. Kein Push, keine echten Zugangsdaten, keine externe
Schreibaktion. Keine T-091-/T-096-Aenderungen.

## Befund (Verifier)

Nach dem Fix in `15c5097` (ID-Vergabe und Zustellung unter demselben
Broadcaster-Lock) war die **Konsumentenseite** in `stream_sse` noch nicht atomar:

- Die Ueberlauf-Quittung (`connection.consume_overflow()`), die Entnahme
  (`connection.take_nowait()`) und der ``resync``-Aufbau
  (`broadcaster.resync_frame()`, das den Stand `_last_id` erst in einem
  **eigenen** Lock-Abschnitt liest) waren drei getrennte Schritte.
- Zwischen der Ueberlauf-Quittung und dem Lesen von `_last_id` konnte ein
  paralleles `emit` eine neue ID vergeben. Der Client sah dann einen
  ``resync`` mit der **neuen** ID und anschliessend den Rahmen mit **derselben**
  ID -- d. h. eine fallende und eine doppelte ID. F-CON-04 verlangt einen
  verlaesslichen, streng aufsteigenden Strom.

## Fix

Neu: `SseBroadcaster.next_frame(connection)` (`backend/finue/services/events.py`).
Unter **genau einem** Broadcaster-Lock:

- liefert `next_frame` bei `connection.consume_overflow() == True` genau ein
  `_resync_frame(self._last_id)`, sonst
- `connection.take_nowait()` (der naechste gepufferte Rahmen oder `None`).

Weil `_on_event` die ID unter demselben Lock vergibt, kann kein Ereignis
zwischen Ueberlauf-Quittung und Entnahme die Reihenfolge verdrehen. `stream_sse`
ersetzt die drei getrennten Schritte durch **einen** `next_frame`-Aufruf.

`consume_overflow` und `take_nowait` blockieren nie (`put_nowait`/`get_nowait`),
daher bleibt die Lock-Reihenfolge Broadcaster -> Connection erhalten (kein
Deadlock). `resync_frame()` (oeffentlicher Aufbau des aktuellen Standes) bleibt
unveraendert. Keine Sync-Runner-, Payload- oder API-Aenderung.

## Tests (`tests/unit/test_sse.py`, +1, jetzt 17)

`test_next_frame_queue2_daueremitter_streng_monoton_ohne_doppel_id` (F-CON-04):
Queue-Groesse 2 (haeufiger Ueberlauf), ein parallel laufender **Dauer-Emitter**
stellt waehrend des Abholens zu; der Konsument holt ueber `next_frame`. Die
erste Ueberlauf-Quittung wird genau im kritischen Fenster angehalten, damit ein
Parallel-Ereignis eintrifft. Geprueft wird: erste beobachtete ID ist der
``resync`` auf dem Stand `3` (vor dem parallelen Ereignis), die beobachteten IDs
steigen **streng** und enthalten **keine Doppel-ID**.

## Evidenz

- `uv run pytest tests/unit/test_sse.py -q` -> **17 passed** (der neue Test
  isoliert 8x wiederholt, jedes Mal gruen -- nicht flakig).
- `uv run pytest tests/unit/test_sse.py tests/unit/test_events.py tests/unit/test_api_contract.py -q`
  -> **40 passed**.
- `uv run pytest tests/unit -q` -> **1600 passed, 1 skipped**.
- `uv run ruff check backend tests scripts` -> All checks passed.
- `uv run ruff format --check backend tests scripts` -> 210 files already
  formatted.
- `uv run mypy backend` -> Success (101 source files).
- **Mutationstest:** `next_frame` auf die nicht-atomare alte Sequenz
  zurueckgesetzt (`if connection.consume_overflow(): return _resync_frame(self._last_id)` ohne
  Lock) -> der neue Test ist rot:
  `AssertionError: [25829, 25829, 28722, 28722, ...]` (doppelte ID). Mit dem Fix
  gruen; der Test ist wirksam.

## Offen

- T-101 bleibt **offen** bis zur unabhaengigen Wiederholungspruefung durch
  `verifier`; kein `task-done`.
- Kein Push.
