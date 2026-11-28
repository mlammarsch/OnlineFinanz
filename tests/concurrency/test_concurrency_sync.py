"""Concurrency-Tests der Sync-Pfade (T-104, F-CON-05, F-SYN-11, N-DB-04).

Referenzierte Anforderungen:

* **F-CON-05** Ein Sync aendert keine Nutzerfelder bestehender Buchungen, auch
  nicht, wenn gleichzeitig Kennzeichen und Notizen gepflegt werden.
* **F-SYN-11** Je Bankzugang laeuft hoechstens ein Sync; parallele Anstosse
  erhalten denselben Job, unterschiedliche Zugaenge blockieren sich nicht.
* **N-DB-04** Kein ``database is locked`` bei gleichzeitigen Schreibern.

Die Sync-Daten sind erfundene Werte (D8); der Sync wird ohne Bankzugriff direkt
ueber :func:`finue.services.sync_persist.persist_account_sync` gespeist.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from finue.models.transaction import Transaction
from finue.services import sync_persist
from finue.services import transaction_edit as te
from finue.services.fints_types import BalanceResult, FetchedTransaction, SyncAccountData
from support import (
    add_account,
    add_transaction,
    any_locked_error,
    read_transaction,
    run_parallel,
    session_factory,
    transaction_fingerprint,
)

pytestmark = pytest.mark.concurrency

#: Erfundene Buchungswerte (D8).
_BOOKING = date(2026, 11, 20)
_IBAN = "DE02120300000000202051"


def test_sync_sperre_je_bankzugang_parallel_f_syn_11(client) -> None:  # type: ignore[no-untyped-def]
    """F-SYN-11: Parallele Anstosse desselben Zugangs teilen genau einen Job.

    16 Threads stoessen denselben Bankzugang an: genau einer legt den Job an,
    alle uebrigen erhalten denselben. Ein anderer Zugang laeuft ungehindert.
    """

    registry = client.app.state.services.sync_registry

    def worker(_index: int) -> tuple[str, bool]:
        job, created = registry.try_start(7)
        return job.id, created

    results, errors = run_parallel(16, worker)

    assert errors == [], errors
    job_ids = {result[0] for result in results if result is not None}
    created_flags = [result[1] for result in results if result is not None]
    assert len(job_ids) == 1, job_ids
    assert created_flags.count(True) == 1

    other_job, other_created = registry.try_start(99)
    assert other_created is True
    assert other_job.id not in job_ids


def test_sync_persistenz_parallel_zu_nutzeraenderungen_f_con_05(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-05: Sync-Persistenz parallel zu Kennzeichen- und Notizaenderungen.

    Eine bereits gebuchte, von der Bank erneut gelieferte Buchung wird beim Sync
    uebersprungen; waehrenddessen setzt der Nutzer das Kennzeichen und haengt 12
    Notizzeilen an. Nach dem Lauf sind alle Nutzerfelder unveraendert, der Sync
    hat genau die eine neue Buchung angelegt und keine Nutzeraenderung
    ueberschrieben.
    """

    account_id = add_account(client, "Girokonto")
    existing_fields: dict[str, object] = {
        "booking_date": _BOOKING,
        "value_date": _BOOKING,
        "amount_cents": -4200,
        "currency": "EUR",
        "counterparty": "Erfundene Beispiel GmbH",
        "counterparty_iban": _IBAN,
        "purpose": "Erfundene Rechnung 2026-001",
    }
    fingerprint = transaction_fingerprint(account_id, **_business_fields(existing_fields))  # type: ignore[arg-type]
    existing_id = add_transaction(client, account_id, origin="fints", fingerprint=fingerprint, **existing_fields)

    new_fields: dict[str, object] = {
        "booking_date": _BOOKING,
        "value_date": _BOOKING,
        "amount_cents": -9900,
        "currency": "EUR",
        "counterparty": "Erfundene Zweite GmbH",
        "counterparty_iban": _IBAN,
        "purpose": "Erfundene Rechnung 2026-002",
    }
    data = SyncAccountData(
        account_ref=_IBAN,
        balance=BalanceResult(amount_cents=123456, currency="EUR"),
        transactions=(
            FetchedTransaction(**_business_fields(existing_fields)),  # type: ignore[arg-type]
            FetchedTransaction(**_business_fields(new_fields)),  # type: ignore[arg-type]
        ),
    )
    fragments = [f"Notiz-{index:02d}" for index in range(12)]

    def user_worker() -> None:
        with session_factory(client)() as session:
            te.set_flag(session, existing_id, flag="checked", value=True, origin="ui")
            for fragment in fragments:
                te.append_note(session, existing_id, text=fragment, origin="ui")

    def sync_worker() -> None:
        with session_factory(client)() as session:
            sync_persist.persist_account_sync(session, account_id, data, job_id=None)

    _results, errors = run_parallel(2, lambda index: user_worker() if index == 0 else sync_worker())

    assert errors == [], errors
    assert not any_locked_error(errors)

    stored = read_transaction(client, existing_id)
    assert stored.checked is True
    assert stored.note is not None
    assert stored.note.startswith("Notiz-00")
    for fragment in fragments:
        assert stored.note.count(fragment) == 1, stored.note
    assert stored.external_ref is None

    with session_factory(client)() as session:
        rows = list(session.scalars(select(Transaction).where(Transaction.account_id == account_id)))
    assert len(rows) == 2, "der Sync legt genau die eine neue Buchung an, keine Dublette"
    assert existing_id in {row.id for row in rows}


def test_gleichzeitige_schreiber_ohne_locked_fehler_n_db_04(client) -> None:  # type: ignore[no-untyped-def]
    """N-DB-04: Gemischte gleichzeitige Schreiber erzeugen kein ``database is locked``.

    20 Threads setzen Kennzeichen verschiedener Buchungen und haengen Notizen an,
    dazu parallele Leser. Kein Aufruf darf an der SQLite-Sperre scheitern (WAL +
    ``busy_timeout``).
    """

    account_id = add_account(client)
    transaction_ids = [add_transaction(client, account_id) for _ in range(5)]

    def worker(index: int) -> None:
        transaction_id = transaction_ids[index % len(transaction_ids)]
        with session_factory(client)() as session:
            te.set_flag(session, transaction_id, flag="checked", value=True, origin="ui")
            te.append_note(session, transaction_id, text=f"parallel-{index:02d}", origin="ui")
        read_transaction(client, transaction_id)

    _results, errors = run_parallel(20, worker)

    assert not any_locked_error(errors), errors
    assert errors == [], errors


def _business_fields(fields: dict[str, object]) -> dict[str, object]:
    """Schneidet die Felder heraus, die fuer Fingerprint und Sync-Buchung zaehlen."""

    return {
        "booking_date": fields["booking_date"],
        "value_date": fields["value_date"],
        "amount_cents": fields["amount_cents"],
        "currency": fields["currency"],
        "counterparty": fields["counterparty"],
        "counterparty_iban": fields["counterparty_iban"],
        "purpose": fields["purpose"],
    }
