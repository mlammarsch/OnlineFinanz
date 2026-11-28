# T-101 Verifier-Abnahme (F-CON-04)

Pruefung auf sauberem Detached-Worktree bei 38c85ce (inkl. 342994a, 15c5097), Fix 73db614 (cherry-pick 37ab186 auf m4/develop).

PASS nach Korrektur. Befunde und Beleg:

- next_frame-Atomaritaet (Queue-Overflow/resync): Eigener Stresstest (4 Dauer-Emitter, 6 Clients mit zufaelligem open/Last-Event-ID/reconnect, Queue 2/8/256, je 6 s): 0 Verstoesse gegen streng steigende IDs, kein Deadlock (alle Threads joinen), client_count 0, bus.subscriber_count 0.
- DEFEKT 1: stream_sse endete nach SseBroadcaster.close() (Lifespan-Shutdown) nie (nur Heartbeats). Fix: `connection.closed` beendet die Schleife.
- DEFEKT 2: Disconnect vor dem ersten Generatorlauf liess die Verbindung in _clients (finally laeuft bei nie gestartetem Generator nicht). Fix: BackgroundTask(broadcaster.discard) an der StreamingResponse (idempotent).
- Neue Tests: test_stream_endet_nach_schliessen_des_verteilers, test_route_meldet_nie_gestartete_verbindung_ueber_background_ab; ohne Fix beide rot (Mutation), mit Fix 5x gruen.
- Review F-CON-04: Auth ueber require_user, kein get_session; Ringpuffer/Last-Event-ID/resync, begrenzte Queue, Heartbeat getestet; Payload nur Typ+IDs (Sync-Ereignisse ohne Challenge-Text/TAN/PIN); Emitter fuer transaction/account/group/duplicate/import/sync vorhanden; Vertragstest gruen.
- Laeufe (Fix-Stand): test_sse+test_events+test_api_contract = 42 passed; tests/unit = 1576 passed/1 skipped; integration = 17 passed; ruff check = All checks passed; ruff format --check = 209 files formatted; mypy backend = Success (101).
