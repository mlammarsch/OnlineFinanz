"""Threadsicherer In-Prozess-Ereignisbus (F-CON-04, D12).

Der Bus meldet Aenderungen an alle angemeldeten Empfaenger im selben Prozess.
Ein Ereignis besteht aus einem Typ und IDs (zum Beispiel ``transaction=42``);
es traegt bewusst **keine** Geheimnisse, Texte oder ganzen Datensaetze
(N-SEC-02). Fach-Tasks veroeffentlichen Ereignisse, die SSE-Route
``/api/events`` (T-101) liefert sie aus.

Der Server laeuft mit genau einem Worker-Prozess (D3). Ein In-Prozess-Bus
genuegt daher; es gibt keinen Verteiler ueber Prozess- oder Geraetegrenzen.

Der Bus ist threadsicher: ``publish`` ruft Empfaenger ausserhalb des Locks auf,
ein fehlerhafter Empfaenger stoppt die uebrigen nicht, und paralleles
An-/Abmelden waehrend einer Zustellung ist zulaessig. Fuer die SSE-Route gibt
es zusaetzlich Warteschlangen-Abonnements mit ``get`` und ``close``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "DEFAULT_SSE_HISTORY_SIZE",
    "DEFAULT_SSE_QUEUE_SIZE",
    "Event",
    "EventBus",
    "EventSubscription",
    "EventValue",
    "RESYNC_EVENT_TYPE",
    "SSE_HEARTBEAT_SECONDS",
    "SSE_POLL_SECONDS",
    "SseBroadcaster",
    "SseConnection",
    "SseFrame",
    "encode_sse",
    "keepalive_frame",
    "new_event",
    "stream_sse",
]

_LOGGER = logging.getLogger(__name__)

#: Erlaubte ID-Werte im Ereignis. IDs sind Zahlen oder kurze Kennungen, nie
#: Geheimnisse (N-SEC-02).
EventValue = int | str

#: Standardgroesse einer SSE-Warteschlange (aeltere Ereignisse fallen heraus).
DEFAULT_QUEUE_SIZE: Final = 1000

#: Standard-Abstand des Heartbeat-Kommentars in der SSE-Route (F-CON-04, T-101).
SSE_HEARTBEAT_SECONDS: Final = 15.0

#: Hoechster Warteabstand je Schleifendurchlauf. Kurz genug, damit ein
#: Disconnect ohne Listener-Leck zeitnah aufgeraeumt wird (T-101, D3).
SSE_POLL_SECONDS: Final = 1.0

#: Standardgroesse der Warteschlange je verbundenem SSE-Client (T-101).
DEFAULT_SSE_QUEUE_SIZE: Final = 256

#: Standardgroesse des Ringpuffers fuer die Last-Event-ID-Nachlieferung (T-101).
DEFAULT_SSE_HISTORY_SIZE: Final = 256

#: Ereignistyp, mit dem die UI nach einem nicht nachlieferbaren Stand neu laedt.
RESYNC_EVENT_TYPE: Final = "resync"


def _utc_now() -> datetime:
    """Aktuelle Zeit in UTC als bewusstes ``datetime``."""

    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Event:
    """Ein Ereignis: Typ, IDs und Zeitpunkt (F-CON-04, N-SEC-02).

    ``ids`` ist eine sortierte Tupel-Liste ``(name, wert)``, damit das Ereignis
    unveraenderlich und hashbar bleibt. Es gibt kein Freitextfeld: Nutzlast,
    Notizen und Geheimnisse gehoeren nicht in ein Ereignis.
    """

    type: str
    ids: tuple[tuple[str, EventValue], ...] = ()
    occurred_at: datetime = field(default_factory=_utc_now)

    def get(self, key: str) -> EventValue | None:
        """Liefert den ID-Wert zu ``key`` oder ``None``."""

        for name, value in self.ids:
            if name == key:
                return value
        return None

    def as_dict(self) -> dict[str, object]:
        """JSON-taugliche Darstellung ohne Geheimnisse (N-SEC-02)."""

        return {
            "type": self.type,
            "ids": dict(self.ids),
            "occurred_at": self.occurred_at.isoformat(),
        }


def new_event(event_type: str, /, **ids: EventValue) -> Event:
    """Baut ein :class:`Event` mit sortierten IDs und aktuellem UTC-Zeitpunkt.

    Args:
        event_type: Dotted-Name, zum Beispiel ``"transaction.updated"``.
        **ids: ID-Schluessel und -Werte, zum Beispiel ``transaction=42``.

    Returns:
        Ein unveraenderliches Ereignis mit sortierten IDs.
    """

    return Event(type=event_type, ids=tuple(sorted(ids.items())))


EventHandler = Callable[[Event], None]


@dataclass(slots=True)
class EventSubscription:
    """Warteschlangen-Abonnement fuer die SSE-Route (T-101).

    Aeltere Ereignisse fallen heraus, wenn die Warteschlange voll ist; die
    neuesten Ereignisse bleiben erhalten. ``close`` meldet das Abonnement vom
    Bus ab; danach liefert ``get`` nur noch bereits gepufferte Ereignisse.
    """

    maxsize: int = DEFAULT_QUEUE_SIZE
    _queue: queue.Queue[Event] = field(init=False, repr=False)
    _unsubscribe: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.maxsize < 1:
            raise ValueError("maxsize muss mindestens 1 sein.")
        self._queue = queue.Queue(maxsize=self.maxsize)

    @property
    def closed(self) -> bool:
        return self._closed

    def get(self, timeout: float | None = None) -> Event:
        """Liefert das naechste Ereignis; wirft ``queue.Empty`` bei Timeout."""

        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        """Meldet das Abonnement ab (idempotent)."""

        if self._closed:
            return
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()

    def _set_unsubscribe(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe

    def _deliver(self, event: Event) -> None:
        # Bei voller Warteschlange faellt das aelteste Ereignis heraus; der
        # Zuhoerer holt sich fehlende Daten ueber die API ohnehin neu.
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                pass


class EventBus:
    """Threadsichere Verwaltung von Empfaengern und Ereignissen."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._lock = threading.Lock()
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Registriert ``handler`` und liefert die Abmeldefunktion."""

        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            self.unsubscribe(handler)

        return unsubscribe

    def unsubscribe(self, handler: EventHandler) -> None:
        """Entfernt ``handler``; unbekannte Handler sind erlaubt (idempotent)."""

        with self._lock:
            try:
                self._handlers.remove(handler)
            except ValueError:
                return

    @property
    def subscriber_count(self) -> int:
        """Anzahl der aktuell registrierten Empfaenger."""

        with self._lock:
            return len(self._handlers)

    def emit(self, event_type: str, /, **ids: EventValue) -> Event:
        """Baut mit der Bus-Uhr ein Ereignis und veroeffentlicht es."""

        event = Event(type=event_type, ids=tuple(sorted(ids.items())), occurred_at=self._clock())
        self.publish(event)
        return event

    def publish(self, event: Event) -> None:
        """Zustellung an alle Empfaenger; Fehler einzelner Empfaenger isoliert.

        Die Empfaengerliste wird unter dem Lock kopiert und ausserhalb des
        Locks aufgerufen. Damit darf ein Empfaenger den Bus erneut betreten,
        ohne zu verklemmen; ein werfender Empfaenger wird protokolliert und
        stoppt die uebrigen nicht (F-CON-04).
        """

        with self._lock:
            handlers = tuple(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                _LOGGER.exception("Ereignis-Empfaenger ist fehlgeschlagen.")

    def subscribe_queue(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> EventSubscription:
        """Registriert eine Warteschlange fuer die SSE-Auslieferung (T-101)."""

        subscription = EventSubscription(maxsize=maxsize)
        subscription._set_unsubscribe(self.subscribe(subscription._deliver))
        return subscription


# ------------------------------------------------------------ Server-Sent Events


def encode_sse(event_id: int, event_type: str, data: dict[str, object]) -> str:
    """Formatiert einen Server-Sent-Event-Rahmen (F-CON-04, T-101).

    Der Rahmen traegt die fortlaufende ``id``, den Ereignistyp im Feld
    ``event`` und die JSON-Nutzlast. Die Nutzlast besteht nur aus Typ und IDs
    (:meth:`Event.as_dict`); Geheimnisse gelangen nicht hinein (N-SEC-02).
    """

    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n"


def keepalive_frame() -> str:
    """Heartbeat-Rahmen als SSE-Kommentar (F-CON-04, T-101)."""

    return ": keepalive\n\n"


@dataclass(frozen=True, slots=True)
class SseFrame:
    """Ein auszuliefernder SSE-Rahmen: fortlaufende ID, Typ und Nutzlast."""

    event_id: int
    event_type: str
    data: dict[str, object]

    def encode(self) -> str:
        """Serialisiert den Rahmen im SSE-Format."""

        return encode_sse(self.event_id, self.event_type, self.data)


def _resync_frame(event_id: int) -> SseFrame:
    """Baut den ``resync``-Rahmen (UI laedt neu, T-101)."""

    return SseFrame(event_id, RESYNC_EVENT_TYPE, {"type": RESYNC_EVENT_TYPE})


class SseConnection:
    """Begrenzte Warteschlange genau einer offenen SSE-Verbindung (T-101).

    Die Warteschlange waechst nie ueber ``maxsize`` hinaus. Bei Ueberlauf (ein
    langsamer Client) wird die Marke gesetzt und der Puffer verworfen; die
    Auslieferung quittiert das mit ``resync`` statt Speicherwachstum. Alle
    Zugriffe sind gegen gleichzeitige Zusteller aus mehreren Threads geschuetzt.
    """

    __slots__ = ("_queue", "_lock", "_overflow", "_closed")

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize muss mindestens 1 sein.")
        self._queue: queue.Queue[SseFrame] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._overflow = False
        self._closed = False

    @property
    def closed(self) -> bool:
        """True, sobald die Verbindung abgemeldet wurde."""

        return self._closed

    def offer(self, frame: SseFrame) -> None:
        """Nimmt einen Rahmen auf; bei voller Warteschlange folgt Ueberlauf."""

        with self._lock:
            if self._closed:
                return
            if self._queue.full():
                self._overflow = True
                self._drain_locked()
                return
            self._queue.put_nowait(frame)

    def take_nowait(self) -> SseFrame | None:
        """Liefert den naechsten Rahmen ohne Warten (oder ``None``)."""

        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def take(self, timeout: float) -> SseFrame | None:
        """Liefert den naechsten Rahmen oder ``None`` nach Ablauf von ``timeout``."""

        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def consume_overflow(self) -> bool:
        """Meldet und loescht die Ueberlaufmarke (Ueberlauf -> ``resync``)."""

        with self._lock:
            overflow = self._overflow
            self._overflow = False
            if overflow:
                self._drain_locked()
            return overflow

    def close(self) -> None:
        """Meldet die Verbindung ab und verwirft gepufferte Rahmen (idempotent)."""

        with self._lock:
            self._closed = True
            self._drain_locked()

    def _drain_locked(self) -> None:
        # Nur unter ``self._lock`` aufrufen. ``queue.Queue`` ist selbst
        # threadsicher; der Lock serialisiert Ueberlauf und Leerlauf.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


class SseBroadcaster:
    """Verteilt Bus-Ereignisse mit fortlaufender ID an SSE-Verbindungen (T-101).

    Ein einzelner Bus-Empfaenger vergibt je Ereignis eine fortlaufende ID, merkt
    die juengsten Ereignisse fuer die ``Last-Event-ID``-Nachlieferung und stellt
    sie in die begrenzte Warteschlange jeder offenen Verbindung. Ist der Stand
    eines Clients nicht mehr nachlieferbar (zu alte ID oder Neustart), liefert
    :meth:`open` ein ``resync``; ein langsamer Client erhaelt bei Ueberlauf
    ebenfalls ein ``resync``. Der Verteiler liegt pro App im Service-Container
    (D12) und arbeitet mit dem einen Worker-Prozess (D3).
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        heartbeat: float = SSE_HEARTBEAT_SECONDS,
        queue_size: int = DEFAULT_SSE_QUEUE_SIZE,
        history_size: int = DEFAULT_SSE_HISTORY_SIZE,
    ) -> None:
        if heartbeat <= 0:
            raise ValueError("heartbeat muss positiv sein.")
        if queue_size < 1:
            raise ValueError("queue_size muss mindestens 1 sein.")
        if history_size < 1:
            raise ValueError("history_size muss mindestens 1 sein.")
        self._bus = bus
        self._heartbeat = heartbeat
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._clients: set[SseConnection] = set()
        self._history: deque[tuple[int, Event]] = deque(maxlen=history_size)
        self._last_id = 0
        self._closed = False
        self._unsubscribe = bus.subscribe(self._on_event)

    @property
    def heartbeat(self) -> float:
        """Abstand des Heartbeat-Kommentars in Sekunden."""

        return self._heartbeat

    @property
    def client_count(self) -> int:
        """Anzahl der aktuell verbundenen SSE-Clients."""

        with self._lock:
            return len(self._clients)

    @property
    def last_event_id(self) -> int:
        """Hoechste bereits vergebene Ereignis-ID (0, solange kein Ereignis)."""

        with self._lock:
            return self._last_id

    def close(self) -> None:
        """Meldet alle Verbindungen und den Bus-Empfaenger ab (idempotent)."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = tuple(self._clients)
            self._clients.clear()
        for connection in clients:
            connection.close()
        self._unsubscribe()

    def open(self, last_event_id: int | None = None) -> SseConnection:
        """Oeffnet eine Verbindung und plant Nachlieferung oder ``resync``.

        Args:
            last_event_id: Der vom Client gemeldete ``Last-Event-ID``-Stand.
                ``None`` oder ``< 1`` bedeutet einen frischen Aufbau.

        Returns:
            Die neue, noch leere Verbindung; Nachlieferung und ein etwaiges
            ``resync`` stehen bereits in ihrer Warteschlange.
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("Der Ereignis-Strom ist geschlossen.")
            connection = SseConnection(self._queue_size)
            replay, resync = self._plan_replay_locked(last_event_id)
            resync_id = self._last_id
            # Nachlieferung und ein etwaiges ``resync`` werden unter genau
            # demselben Verteiler-Lock eingereiht wie spaetere Ereignisse
            # (:meth:`_on_event`); erst danach wird die Verbindung in
            # ``_clients`` sichtbar. So bleiben die IDs je Client monoton,
            # auch wenn parallel ein Ereignis zugestellt wird (F-CON-04,
            # T-101). ``offer`` blockiert nicht, die Lock-Reihenfolge bleibt
            # Broadcaster -> Connection.
            for event_id, event in replay:
                connection.offer(SseFrame(event_id, event.type, event.as_dict()))
            if resync:
                connection.offer(_resync_frame(resync_id))
            self._clients.add(connection)
        return connection

    def discard(self, connection: SseConnection) -> None:
        """Meldet eine Verbindung ab (Disconnect, idempotent)."""

        with self._lock:
            self._clients.discard(connection)
        connection.close()

    def resync_frame(self) -> SseFrame:
        """Baut ein ``resync``-Frame mit dem aktuellen Stand (Ueberlauf)."""

        with self._lock:
            return _resync_frame(self._last_id)

    def _plan_replay_locked(self, last_event_id: int | None) -> tuple[tuple[tuple[int, Event], ...], bool]:
        # ``resync`` genau dann, wenn der Stand nicht mehr nachlieferbar ist:
        # kein Stand gemeldet, Server kennt den Stand nicht mehr (ID vor dem
        # Ringpuffer oder hoeher als der Server) oder der Puffer ist leer.
        if last_event_id is None or last_event_id < 1:
            return (), False
        if last_event_id == self._last_id:
            return (), False
        if last_event_id > self._last_id:
            return (), True
        if not self._history:
            return (), True
        oldest = self._history[0][0]
        if last_event_id < oldest - 1:
            return (), True
        replay = tuple((event_id, event) for event_id, event in self._history if event_id > last_event_id)
        return replay, False

    def _on_event(self, event: Event) -> None:
        # ID-Vergabe und Zustellung liegen unter demselben Lock wie die
        # Nachlieferung in :meth:`open`. Damit kann kein Ereignis an einer
        # Verbindung vor ihren Nachlieferungsrahmen vorbeiziehen; die IDs je
        # Client bleiben monoton (F-CON-04, T-101). ``offer`` blockiert nicht,
        # die Lock-Reihenfolge bleibt Broadcaster -> Connection.
        with self._lock:
            if self._closed:
                return
            self._last_id += 1
            event_id = self._last_id
            self._history.append((event_id, event))
            frame = SseFrame(event_id, event.type, event.as_dict())
            for connection in self._clients:
                connection.offer(frame)


async def stream_sse(
    broadcaster: SseBroadcaster,
    connection: SseConnection,
    *,
    disconnected: Callable[[], Awaitable[bool]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[str]:
    """Erzeugt die SSE-Rahmen einer Verbindung bis zum Disconnect (T-101).

    Der Abfrageabstand bleibt kurz (``min(heartbeat, SSE_POLL_SECONDS)``),
    damit ein Disconnect ohne Listener-Leck zeitnah aufgeraeumt wird; der
    Heartbeat-Kommentar erscheint erst nach ``broadcaster.heartbeat`` Sekunden
    ohne Ausgabe. Der Generator raeumt die Verbindung in jedem Fall ab.

    Args:
        broadcaster: Der Verteiler, der Verbindung und Zeitverhalten traegt.
        connection: Die offene Verbindung dieses Clients.
        disconnected: Optionale Pruefung (``request.is_disconnected``); meldet
            sie ``True``, endet der Strom.
        monotonic: Injizierbare Monotonuhr fuer die Heartbeat-Steuerung.
        sleep: Injizierbarer Wartemechanismus (``asyncio.sleep``).

    Yields:
        SSE-Rahmen als Text.
    """

    heartbeat = broadcaster.heartbeat
    poll = min(heartbeat, SSE_POLL_SECONDS)
    last_output = monotonic()
    try:
        yield ": verbunden\n\n"
        while True:
            if disconnected is not None and await disconnected():
                break
            if connection.consume_overflow():
                yield broadcaster.resync_frame().encode()
                last_output = monotonic()
                continue
            frame = connection.take_nowait()
            if frame is not None:
                yield frame.encode()
                last_output = monotonic()
                continue
            await sleep(poll)
            if monotonic() - last_output >= heartbeat:
                yield keepalive_frame()
                last_output = monotonic()
    finally:
        broadcaster.discard(connection)
