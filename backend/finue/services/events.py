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

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

__all__ = [
    "Event",
    "EventBus",
    "EventSubscription",
    "EventValue",
    "new_event",
]

_LOGGER = logging.getLogger(__name__)

#: Erlaubte ID-Werte im Ereignis. IDs sind Zahlen oder kurze Kennungen, nie
#: Geheimnisse (N-SEC-02).
EventValue = int | str

#: Standardgroesse einer SSE-Warteschlange (aeltere Ereignisse fallen heraus).
DEFAULT_QUEUE_SIZE: Final = 1000


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
