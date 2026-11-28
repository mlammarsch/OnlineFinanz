"""Gemeinsame Helfer des Concurrency-Blocks (T-104, F-CON-01..05, N-DB-04).

Der Block faehrt die Schreibpfade gegen eine **datei-basierte** SQLite-Datenbank
im WAL-Modus an (``tests/conftest.py`` liefert die App samit temporaerer
``DATABASE_URL``); jede der vielen Sitzungen ist eine eigene
:class:`~sqlalchemy.orm.Session` beziehungsweise ein eigener HTTP-Aufruf. Die
Threads synchronisieren sich ueber Barrieren, nie ueber ``sleep`` (deterministisch,
N-DB-04).

Alle Konten und Buchungen sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from datetime import date
from typing import TypeVar

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.models.account import Account
from finue.models.audit import AuditLog
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.events import SseConnection, SseFrame
from finue.services.search_text import build_search_text

__all__ = [
    "add_account",
    "add_transaction",
    "any_locked_error",
    "audit_count",
    "drain",
    "next_fingerprint",
    "read_transaction",
    "run_parallel",
    "session_factory",
    "transaction_fingerprint",
]

T = TypeVar("T")

_sequence = itertools.count(1)

#: Standardfelder einer Testbuchung (F-SUC-01, F-DUP-01); erfundene Werte (D8).
_TRANSACTION_DEFAULTS: dict[str, object] = {
    "booking_date": date(2026, 1, 10),
    "amount_cents": 10000,
    "currency": "EUR",
    "origin": "csv",
    "status": "gebucht",
    "counterparty": None,
    "purpose": None,
    "note": None,
    "counterparty_iban": None,
    "external_ref": None,
    "checked": False,
    "reconciled": False,
    "merged": False,
    "occurrence_index": 1,
}


def next_fingerprint() -> str:
    """Liefert einen eindeutigen, 64-stelligen Fingerprint (F-DUP-01)."""

    return f"{next(_sequence):064x}"


def session_factory(client: TestClient) -> sessionmaker[Session]:
    """Liefert die Session-Factory der Test-App (N-DB-01)."""

    factory: sessionmaker[Session] = client.app.state.session_factory
    return factory


def add_account(client: TestClient, name: str = "Konto", **overrides: object) -> int:
    """Legt ein Konto an und liefert seine ID (erfundene Testdaten, D8)."""

    fields: dict[str, object] = {"name": name, "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    with session_factory(client)() as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


def add_transaction(client: TestClient, account_id: int, **overrides: object) -> int:
    """Legt eine Testbuchung mit gefuellter Suchspalte an (F-SUC-01)."""

    fields = dict(_TRANSACTION_DEFAULTS)
    fields["account_id"] = account_id
    fields.update(overrides)
    if "fingerprint" not in fields:
        fields["fingerprint"] = next_fingerprint()
    fields["search_text"] = build_search_text(
        counterparty=fields["counterparty"] if isinstance(fields["counterparty"], str) else None,
        purpose=fields["purpose"] if isinstance(fields["purpose"], str) else None,
        note=fields["note"] if isinstance(fields["note"], str) else None,
        counterparty_iban=fields["counterparty_iban"] if isinstance(fields["counterparty_iban"], str) else None,
        external_ref=fields["external_ref"] if isinstance(fields["external_ref"], str) else None,
    )
    with session_factory(client)() as session:
        transaction = Transaction(**fields)
        session.add(transaction)
        session.commit()
        return transaction.id


def transaction_fingerprint(
    account_id: int,
    *,
    booking_date: date,
    amount_cents: int,
    currency: str,
    value_date: date | None = None,
    counterparty: str | None = None,
    counterparty_iban: str | None = None,
    purpose: str | None = None,
) -> str:
    """Berechnet den Fingerprint, den eine Sync-Lieferung erzeugt (F-DUP-01)."""

    return compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
    )


def read_transaction(client: TestClient, transaction_id: int) -> Transaction:
    """Laedt eine Buchung aus der Datenbank (frische Sitzung, keine ORM-Identitaet)."""

    with session_factory(client)() as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        session.refresh(transaction)
        return transaction


def audit_count(client: TestClient, transaction_id: int) -> int:
    """Zaehlt die Audit-Eintraege einer Buchung (F-MCP-09)."""

    with session_factory(client)() as session:
        return len(
            list(
                session.scalars(
                    select(AuditLog).where(AuditLog.entity == "transaction", AuditLog.entity_id == transaction_id)
                )
            )
        )


def run_parallel(
    count: int,
    worker: Callable[[int], T],
    *,
    timeout: float = 60.0,
) -> tuple[list[T | None], list[BaseException]]:
    """Fuehrt ``count`` Threads ueber eine Barriere gleichzeitig aus.

    Jeder Thread ruft ``worker(index)`` genau einmal auf. Die Threads warten an
    einer :class:`threading.Barrier`, bis alle gestartet sind (Barriere statt
    ``sleep``). Rueckgabe ist die Ergebnisliste (Index -> Ergebnis) und die Liste
    der aufgetretenen Fehler. Bleibt ein Thread haengen, schlaegt die Pruefung
    fehl, statt die Sitzung zu blockieren.
    """

    results: list[T | None] = [None] * count
    errors: list[BaseException] = []
    guard = threading.Lock()
    barrier = threading.Barrier(count)

    def wrapped(index: int) -> None:
        try:
            barrier.wait(timeout=timeout)
            value = worker(index)
        except BaseException as exc:  # Fehler der Arbeit werden eingesammelt
            with guard:
                errors.append(exc)
        else:
            with guard:
                results[index] = value

    threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    hanging = [thread for thread in threads if thread.is_alive()]
    assert not hanging, "Thread hat die Zeit ueberschritten (moeglicher Verklemmer)"
    return results, errors


def any_locked_error(errors: list[BaseException]) -> bool:
    """Meldet, ob einer der Fehler ein ``database is locked`` ist (N-DB-04)."""

    return any("database is locked" in str(error).lower() for error in errors)


def drain(connection: SseConnection) -> list[SseFrame]:
    """Holt alle gepufferten SSE-Rahmen einer Verbindung ohne Warten (F-CON-04)."""

    frames: list[SseFrame] = []
    while True:
        frame = connection.take_nowait()
        if frame is None:
            return frames
        frames.append(frame)
