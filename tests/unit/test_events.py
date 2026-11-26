"""Tests des Ereignisbusses (T-086).

Referenziert F-CON-04 (Aenderungsbenachrichtigung), N-SEC-02 (keine Geheimnisse
in Ereignissen) und D12 (In-Prozess-Bus, ein Worker). Geprueft werden
An-/Abmelden, Zustellung, Fehlerisolierung, die Warteschlangen-Abnahme fuer die
SSE-Route sowie die Nebenlaeufigkeit.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone

from finue.services.events import Event, EventBus, new_event


def test_new_event_sortiert_ids_und_traegt_typ() -> None:
    """Ein Ereignis traegt Typ plus IDs; as_dict bleibt ohne Geheimnisfelder (F-CON-04, N-SEC-02)."""
    event = new_event("transaction.updated", transaction=42, account=7)

    assert event.type == "transaction.updated"
    assert dict(event.ids) == {"account": 7, "transaction": 42}
    assert event.get("transaction") == 42
    assert event.get("unbekannt") is None
    body = event.as_dict()
    assert set(body) == {"type", "ids", "occurred_at"}


def test_zeitpunkt_aus_bus_uhr_und_festem_stempel() -> None:
    """emit stempelt mit der injizierbaren Bus-Uhr; Event nimmt einen festen Zeitpunkt (D12)."""
    fixed = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)

    bus = EventBus(clock=lambda: fixed)
    gestempelt = bus.emit("ping", n=1)
    direkt = Event(type="ping", ids=(("n", 1),), occurred_at=fixed)

    assert gestempelt.occurred_at == fixed
    assert gestempelt.ids == (("n", 1),)
    assert direkt.occurred_at == fixed
    # new_event setzt einen bewussten UTC-Zeitpunkt.
    assert new_event("ping").occurred_at.tzinfo is not None


def test_publish_liefert_an_abonnenten() -> None:
    """publish ruft jeden registrierten Empfaenger genau einmal auf (F-CON-04)."""
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)

    event = bus.emit("account.updated", account=3)

    assert received == [event]
    assert bus.subscriber_count == 1


def test_unsubscribe_stoppt_zustellung() -> None:
    """Nach dem Abmelden kommen keine weiteren Ereignisse an (F-CON-04)."""
    bus = EventBus()
    received: list[Event] = []
    unsubscribe = bus.subscribe(received.append)

    bus.emit("a", n=1)
    unsubscribe()
    bus.emit("b", n=2)

    assert [event.type for event in received] == ["a"]
    assert bus.subscriber_count == 0
    # Idempotent: erneutes Abmelden ist erlaubt.
    unsubscribe()
    bus.unsubscribe(received.append)


def test_fehlender_empfaenger_stoppt_andere_nicht() -> None:
    """Ein werfender Empfaenger wird isoliert und protokolliert (F-CON-04)."""
    bus = EventBus()
    received: list[Event] = []

    def kaputt(event: Event) -> None:
        raise RuntimeError("Empfaenger defekt")

    bus.subscribe(kaputt)
    bus.subscribe(received.append)

    bus.emit("ping", n=1)

    assert len(received) == 1


def test_publish_ohne_abonnenten_ist_sicher() -> None:
    """Ohne Empfaenger ist publish folgenlos (F-CON-04)."""
    bus = EventBus()
    event = bus.emit("ping", n=1)
    assert isinstance(event, Event)


def test_warteschlangen_abonnement_liefert_und_schliesst() -> None:
    """subscribe_queue liefert Ereignisse fuer die SSE-Route (F-CON-04, T-101)."""
    bus = EventBus()
    subscription = bus.subscribe_queue(maxsize=4)

    sent = bus.emit("sync.status", job="abc", status=1)
    got = subscription.get(timeout=1)

    assert got == sent
    subscription.close()
    subscription.close()  # idempotent
    assert bus.subscriber_count == 0


def test_warteschlange_verwirft_aeltere_bei_ueberlauf() -> None:
    """Bei voller Warteschlange bleibt das neueste Ereignis erhalten (T-101)."""
    bus = EventBus()
    subscription = bus.subscribe_queue(maxsize=2)

    for index in range(5):
        bus.emit("ping", n=index)

    aelter = subscription.get(timeout=1)
    neuester = subscription.get(timeout=1)
    assert aelter.get("n") == 3
    assert neuester.get("n") == 4


def test_warteschlange_ohne_ereignis_meldet_timeout() -> None:
    """Ohne Ereignis wirft get nach Timeout ``queue.Empty`` (T-101)."""
    bus = EventBus()
    subscription = bus.subscribe_queue()

    try:
        subscription.get(timeout=0.01)
    except queue.Empty:
        pass
    else:  # pragma: no cover - nur bei Fehlverhalten
        raise AssertionError("Erwartet wurde queue.Empty.")


def test_paralleles_anmelden_und_senden_ist_sicher() -> None:
    """Paralleles subscribe/emit/unsubscribe laeuft ohne Fehler und ohne Leck (D3, F-CON-04)."""
    bus = EventBus()
    errors: list[BaseException] = []
    counter_lock = threading.Lock()
    received: list[Event] = []

    def handler(event: Event) -> None:
        with counter_lock:
            received.append(event)

    def worker() -> None:
        try:
            for index in range(100):
                unsubscribe = bus.subscribe(handler)
                bus.emit("ping", n=index)
                unsubscribe()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert bus.subscriber_count == 0
    # Jedes der 4*100 Ereignisse erreicht mindestens einen Empfaenger; durch
    # parallel angemeldete Handler koennen es mehr Zustellungen sein.
    assert len(received) >= 400
