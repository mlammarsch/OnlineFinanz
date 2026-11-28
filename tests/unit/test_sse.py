"""Tests der Server-Sent-Events-Route ``GET /api/events`` (T-101).

Referenziert F-CON-04 (Aenderungsbenachrichtigung per SSE an alle Sitzungen),
F-SYN-04 (TAN-Anforderung erreicht alle Sitzungen), N-SEC-02/03 (keine
Geheimnisse im Strom), D3 (ein Worker-Prozess) und D12 (In-Prozess-Bus). Geprueft
werden der Verteiler und die Rahmen-Erzeugung (zwei Clients, Nachlieferung,
Ueberlauf -> resync, Disconnect ohne Leck, TAN-Anforderung, Last-Event-ID),
die Route (Kopfzeilen, Auth-Naht, Nachlieferung) und die Sicherheit der Nutzlast.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finue.config import Settings
from finue.main import create_app
from finue.services.events import (
    RESYNC_EVENT_TYPE,
    EventBus,
    SseBroadcaster,
    SseConnection,
    SseFrame,
    encode_sse,
    keepalive_frame,
    stream_sse,
)

# ------------------------------------------------------------------ Helfer


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Wartet begrenzt, bis ``predicate`` erfuellt ist (fuer Hintergrundabbau)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _drain(connection: SseConnection) -> list[SseFrame]:
    """Holt alle gepufferten Rahmen einer Verbindung ohne Warten."""

    frames: list[SseFrame] = []
    while True:
        frame = connection.take_nowait()
        if frame is None:
            return frames
        frames.append(frame)


def _pull(async_gen: AsyncIterator[str], count: int) -> list[str]:
    """Holt ``count`` Rahmen aus einem asynchronen Generator und schliesst ihn."""

    async def runner() -> list[str]:
        out: list[str] = []
        try:
            async for chunk in async_gen:
                out.append(chunk)
                if len(out) >= count:
                    break
        finally:
            await async_gen.aclose()
        return out

    return asyncio.run(runner())


def _read_route(client: TestClient, *, headers: dict[str, str] | None = None, events: int = 1) -> list[str]:
    """Liest aus der Route bis ``events`` Ereignisse vollstaendig sind."""

    collected: list[str] = []
    seen = 0
    with client.stream("GET", "/api/events", headers=headers or {}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line:
                collected.append(line)
            if line.startswith("event:"):
                seen += 1
            if seen >= events and line == "":
                break
    return collected


# ------------------------------------------------- Verteiler und Rahmen (F-CON-04)


def test_encode_sse_traegt_id_typ_und_json() -> None:
    """Der Rahmen traegt fortlaufende ID, Typ und JSON-Nutzlast (F-CON-04)."""

    rahmen = encode_sse(7, "transaction.changed", {"type": "transaction.changed", "ids": {"transaction": 42}})

    assert rahmen.startswith("id: 7\n")
    assert "event: transaction.changed\n" in rahmen
    assert 'data: {"type":"transaction.changed","ids":{"transaction":42}}\n\n' in rahmen
    assert keepalive_frame() == ": keepalive\n\n"


def test_zwei_clients_erhalten_dasselbe_ereignis() -> None:
    """Zwei verbundene Clients erhalten dasselbe Ereignis mit derselben ID (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, queue_size=8, history_size=8)
    first = broadcaster.open()
    second = broadcaster.open()

    event = bus.emit("transaction.changed", account=1, transaction=42)

    frame_first = first.take(timeout=1)
    frame_second = second.take(timeout=1)
    assert frame_first is not None and frame_second is not None
    assert frame_first.event_id == frame_second.event_id == 1
    assert frame_first.event_type == frame_second.event_type == "transaction.changed"
    assert frame_first.data == frame_second.data == event.as_dict()
    broadcaster.close()


def test_last_event_id_liefert_nach() -> None:
    """Innerhalb des Puffers liefert ``Last-Event-ID`` die fehlenden Ereignisse (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, history_size=8)
    bus.emit("account.changed", account=1)
    bus.emit("transaction.changed", account=1, transaction=2)
    bus.emit("import.finished", account=1, run=3)

    connection = broadcaster.open(last_event_id=1)

    frames = _drain(connection)
    assert [(frame.event_id, frame.event_type) for frame in frames] == [
        (2, "transaction.changed"),
        (3, "import.finished"),
    ]
    broadcaster.close()


def test_last_event_id_nicht_nachlieferbar_folgt_resync() -> None:
    """Eine zu alte, zu neue oder unbekannte ID fuehrt zum ``resync`` (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, history_size=1)
    bus.emit("a", n=1)
    bus.emit("b", n=2)
    bus.emit("c", n=3)

    # Zu alt: der Puffer haelt nur noch ID 3.
    alt = broadcaster.open(last_event_id=1)
    assert alt.take(timeout=1).event_type == RESYNC_EVENT_TYPE  # type: ignore[union-attr]

    # Zu neu/Server-Neustart: der Server kennt die ID nicht.
    neu = broadcaster.open(last_event_id=99)
    assert neu.take(timeout=1).event_type == RESYNC_EVENT_TYPE  # type: ignore[union-attr]
    broadcaster.close()


def test_leerer_puffer_und_unbekannte_id_ergeben_resync() -> None:
    """Ohne eigenen Stand bzw. ohne Puffer folgt ``resync`` (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus)
    connection = broadcaster.open(last_event_id=5)

    assert connection.take(timeout=1).event_type == RESYNC_EVENT_TYPE  # type: ignore[union-attr]
    broadcaster.close()


def test_ueberlauf_liefert_resync_statt_speicherwachstum() -> None:
    """Bei voller Warteschlange bleibt der Puffer leer und es folgt ``resync`` (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, queue_size=2, history_size=8)
    connection = broadcaster.open()
    for index in range(5):
        bus.emit("ping", n=index)

    assert connection.consume_overflow() is True
    assert connection.take_nowait() is None
    assert broadcaster.resync_frame().event_type == RESYNC_EVENT_TYPE
    broadcaster.close()


def test_stream_liefert_resync_nach_ueberlauf() -> None:
    """``stream_sse`` quittiert den Ueberlauf mit einem ``resync``-Rahmen (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, queue_size=1, heartbeat=60.0)
    connection = broadcaster.open()
    bus.emit("a", n=1)
    bus.emit("b", n=2)  # Ueberlauf

    chunks = _pull(stream_sse(broadcaster, connection), 2)

    assert chunks[0] == ": verbunden\n\n"
    assert "event: resync" in chunks[1]
    assert broadcaster.client_count == 0  # der Generator hat abgeraeumt


def test_stream_heartbeat_kommentar() -> None:
    """Ohne Ereignisse liefert ``stream_sse`` nach dem Heartbeat einen Kommentar (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, heartbeat=0.02)
    connection = broadcaster.open()

    chunks = _pull(stream_sse(broadcaster, connection), 2)

    assert chunks[0] == ": verbunden\n\n"
    assert chunks[1] == keepalive_frame()


def test_disconnect_raeumt_auf_ohne_leck() -> None:
    """Ein Disconnect meldet die Verbindung ab; kein Ereignis erreicht sie danach (F-CON-04)."""

    bus = EventBus()
    broadcaster = SseBroadcaster(bus)
    connection = broadcaster.open()
    assert broadcaster.client_count == 1

    broadcaster.discard(connection)

    assert broadcaster.client_count == 0
    assert connection.closed is True
    bus.emit("transaction.changed", account=1)
    assert connection.take_nowait() is None
    broadcaster.close()
    assert bus.subscriber_count == 0


def test_parallele_emits_liefern_monotone_ids_je_client() -> None:
    """Parallele ``emit``-Aufrufe liefern je Client streng steigende, lueckenlose IDs (F-CON-04).

    Der Test verkuerzt das GIL-Umschaltintervall, damit ein Thread genau im
    Race-Fenster zwischen ID-Vergabe und Zustellung verdraengt werden kann. Wird
    Zustellung nicht unter demselben Lock wie die ID-Vergabe serialisiert, sieht
    ein Client eine hoehere ID vor einer niedrigeren und der Vergleich schlaegt
    fehl.
    """

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, queue_size=5000, history_size=5000)
    first = broadcaster.open()
    second = broadcaster.open()

    workers_count = 4
    per_worker = 200
    total = workers_count * per_worker
    start = threading.Barrier(workers_count)

    def emitter(worker: int) -> None:
        start.wait(timeout=5.0)
        for index in range(per_worker):
            bus.emit("transaction.changed", account=worker, transaction=index)

    workers = [threading.Thread(target=emitter, args=(worker,)) for worker in range(workers_count)]
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10.0)
    finally:
        sys.setswitchinterval(previous_interval)

    assert all(not worker.is_alive() for worker in workers)

    expected = list(range(1, total + 1))
    for connection in (first, second):
        ids = [frame.event_id for frame in _drain(connection)]
        assert ids == expected
        assert all(left < right for left, right in zip(ids, ids[1:]))

    broadcaster.close()
    assert bus.subscriber_count == 0


def test_open_race_haelt_ids_je_client_monoton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parallel zu ``open`` zugestellte Ereignisse stehen hinter der Nachlieferung (F-CON-04).

    Die erste Zustellung (der Nachlieferungsrahmen) wird angehalten, waehrend ein
    zweiter Thread ein Ereignis veroeffentlicht. Erst nach dem Freigeben laeuft
    ``open`` weiter. Serialisiert ``open`` Nachlieferung und Sichtbarkeit der
    Verbindung nicht unter demselben Lock wie ``_on_event``, reiht der Emitter
    seine hoehere ID vor den Nachlieferungsrahmen ein; die IDs waeren dann nicht
    mehr monoton.
    """

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, queue_size=16, history_size=16)
    bus.emit("a", n=1)
    bus.emit("b", n=2)
    bus.emit("c", n=3)

    original_offer = SseConnection.offer
    first_offer_started = threading.Event()
    gate = threading.Event()
    calls = {"count": 0}

    def gated_offer(self: SseConnection, frame: SseFrame) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            first_offer_started.set()
            assert gate.wait(timeout=5.0)
        original_offer(self, frame)

    monkeypatch.setattr(SseConnection, "offer", gated_offer)

    opened: dict[str, SseConnection] = {}

    def opener() -> None:
        opened["connection"] = broadcaster.open(last_event_id=1)

    opener_thread = threading.Thread(target=opener)
    opener_thread.start()
    assert first_offer_started.wait(timeout=5.0)

    emitter_thread = threading.Thread(target=lambda: bus.emit("d", n=4))
    emitter_thread.start()
    # Ohne Serialisierung reiht der Emitter vor dem Freigeben ein; die Pause
    # gibt ihm dazu Gelegenheit, ohne den Test bei korrektem Code zu verzoegern.
    time.sleep(0.2)
    gate.set()
    opener_thread.join(timeout=5.0)
    emitter_thread.join(timeout=5.0)

    assert not opener_thread.is_alive()
    assert not emitter_thread.is_alive()

    connection = opened["connection"]
    ids = [frame.event_id for frame in _drain(connection)]
    assert ids == [2, 3, 4], ids

    broadcaster.discard(connection)
    broadcaster.close()
    assert bus.subscriber_count == 0


def test_next_frame_queue2_daueremitter_streng_monoton_ohne_doppel_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``next_frame`` quittiert den Ueberlauf atomar; ein Dauer-Emitter erzeugt keine Doppel-ID (F-CON-04).

    Queue-Groesse 2 erzwingt haeufigen Ueberlauf. Ein parallel laufender
    Dauer-Emitter stellt waehrend des Abholens Ereignisse zu; der Konsument holt
    die Rahmen ueber ``SseBroadcaster.next_frame``. Die beobachteten IDs muessen
    streng steigen und sich nie wiederholen. Die erste Ueberlauf-Quittung wird
    genau im kritischen Fenster angehalten, damit das Parallel-Ereignis
    eintrifft: Nur wenn ``next_frame`` Ueberlaufpruefung und ``resync``-ID unter
    demselben Lock liest, kann das Ereignis nicht vor den ``resync``-Rahmen
    rutschen (sonst folgen eine fallende und eine doppelte ID).
    """

    bus = EventBus()
    broadcaster = SseBroadcaster(bus, queue_size=2, history_size=8)
    connection = broadcaster.open()

    # Queue-Groesse 2: das dritte Ereignis verdraengt den Puffer (Ueberlauf).
    bus.emit("a", n=1)
    bus.emit("b", n=2)
    bus.emit("c", n=3)  # Ueberlauf -> resync faellig; der Stand ist 3

    original_consume = SseConnection.consume_overflow
    gated = threading.Event()
    release = threading.Event()

    def gated_consume(self: SseConnection) -> bool:
        result = original_consume(self)
        if result:
            gated.set()
            assert release.wait(timeout=5.0)
        return result

    monkeypatch.setattr(SseConnection, "consume_overflow", gated_consume)

    observed: list[int] = []
    stop = threading.Event()

    def consumer() -> None:
        while not stop.is_set():
            frame = broadcaster.next_frame(connection)
            if frame is not None:
                observed.append(frame.event_id)

    def emitter() -> None:
        index = 4
        while not stop.is_set():
            bus.emit("d", n=index)
            index += 1

    consumer_thread = threading.Thread(target=consumer)
    consumer_thread.start()
    assert gated.wait(timeout=5.0)  # Konsument steht im kritischen Fenster

    emitter_thread = threading.Thread(target=emitter)
    emitter_thread.start()
    time.sleep(0.1)  # dem Dauer-Emitter Gelegenheit geben, im Fenster zuzustellen
    release.set()

    time.sleep(0.1)  # beide Seiten weiterlaufen lassen
    stop.set()
    consumer_thread.join(timeout=5.0)
    emitter_thread.join(timeout=5.0)

    assert not consumer_thread.is_alive()
    assert not emitter_thread.is_alive()

    assert observed, "es wurden keine Rahmen beobachtet"
    assert observed[0] == 3, observed  # resync auf dem Stand vor dem Parallel-Ereignis
    assert all(left < right for left, right in zip(observed, observed[1:])), observed
    assert len(set(observed)) == len(observed), observed  # keine Doppel-ID

    broadcaster.discard(connection)
    broadcaster.close()
    assert bus.subscriber_count == 0


def test_tan_anforderung_erreicht_alle_clients(client: TestClient) -> None:
    """Der Registry-Statuswechsel des Sync-Runners erreicht alle Clients (F-SYN-04, F-CON-04)."""

    services = client.app.state.services
    registry = services.sync_registry
    broadcaster = services.sse_broadcaster

    job, _created = registry.try_start(7)
    first = broadcaster.open()
    second = broadcaster.open()

    registry.mark_awaiting_tan(job.id, object(), challenge="Bitte TAN eingeben: 123456", tan_kind="text")

    first_types = [frame.event_type for frame in _drain(first)]
    second_types = [frame.event_type for frame in _drain(second)]
    assert "sync.tan_required" in first_types
    assert first_types == second_types
    assert "sync.status" in first_types


def test_payload_traegt_keine_geheimnisse(client: TestClient) -> None:
    """Die Nutzlast traegt nur Typ und IDs, nie PIN, TAN oder Dialogdaten (N-SEC-02/03)."""

    services = client.app.state.services
    registry = services.sync_registry
    broadcaster = services.sse_broadcaster

    connection = broadcaster.open()
    job, _created = registry.try_start(3)
    registry.mark_awaiting_tan(job.id, {"tan": "123456"}, challenge="Bitte TAN eingeben: 123456")

    text = "".join(frame.encode() for frame in _drain(connection))

    assert "123456" not in text
    assert "pin" not in text.lower()
    assert "event: sync.tan_required" in text
    for line in text.splitlines():
        if line.startswith("data:"):
            assert '"challenge"' not in line
            assert '"dialog"' not in line


# --------------------------------------------------------------- Route (F-CON-04)


def test_route_liefert_event_stream_und_kommentar(client: TestClient) -> None:
    """``GET /api/events`` antwortet als ``text/event-stream`` mit Startkommentar (F-CON-04)."""

    with client.stream("GET", "/api/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines: Iterator[str] = response.iter_lines()
        assert next(lines) == ": verbunden"

    broadcaster = client.app.state.services.sse_broadcaster
    assert _wait_until(lambda: broadcaster.client_count == 0)


def test_route_liefert_last_event_id_nach(client: TestClient) -> None:
    """Die Route liefert fehlende Ereignisse ueber ``Last-Event-ID`` nach (F-CON-04)."""

    services = client.app.state.services
    services.event_bus.emit("account.changed", account=1)
    services.event_bus.emit("transaction.changed", account=1, transaction=2)
    letzte_id = services.sse_broadcaster.last_event_id

    lines = _read_route(client, headers={"Last-Event-ID": str(letzte_id - 1)}, events=1)

    assert ": verbunden" in lines
    assert any(line == "event: transaction.changed" for line in lines)
    assert any(f"id: {letzte_id}" == line for line in lines)


def test_route_braucht_anmeldung_wenn_aktiv(tmp_path: Path) -> None:
    """Mit aktivem ``auth_required`` liefert die Route 401 (N-SEC-04)."""

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'sse.db').as_posix()}",
        data_dir=tmp_path,
        auth_required=True,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/events")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_stream_endet_nach_schliessen_des_verteilers() -> None:
    """Beim Herunterfahren (Lifespan) endet jeder offene Strom von selbst (F-CON-04, D3)."""

    async def scenario() -> None:
        bus = EventBus()
        broadcaster = SseBroadcaster(bus, heartbeat=0.05)
        connection = broadcaster.open()

        async def consume() -> None:
            async for _ in stream_sse(broadcaster, connection):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.2)
        broadcaster.close()
        await asyncio.wait_for(task, timeout=3.0)
        assert bus.subscriber_count == 0

    asyncio.run(scenario())


def test_route_meldet_nie_gestartete_verbindung_ueber_background_ab() -> None:
    """Trennt der Client vor dem ersten Rahmen, gibt die Route die Verbindung frei (F-CON-04)."""

    from types import SimpleNamespace
    from typing import cast

    from starlette.requests import Request

    from finue.api.container import AppServices
    from finue.api.events import stream_events

    broadcaster = SseBroadcaster(EventBus())
    services = cast(AppServices, SimpleNamespace(sse_broadcaster=broadcaster))
    request = Request({"type": "http", "headers": []})

    response = stream_events(request, services)
    assert broadcaster.client_count == 1
    assert response.background is not None
    asyncio.run(response.background())

    assert broadcaster.client_count == 0
    broadcaster.close()
