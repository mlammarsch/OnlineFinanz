"""Concurrency-Tests der REST- und Ereignis-Pfade (T-104, F-CON-03/04).

Referenzierte Anforderungen:

* **F-CON-03** Batch-Isolation: Ein Konflikt eines Eintrags bricht die uebrigen
  Eintraege nicht ab; jeder Eintrag traegt sein eigenes Ergebnis.
* **F-CON-04** Aenderungsbenachrichtigung erreicht **alle** verbundenen
  Sitzungen (zwei SSE-Clients erhalten dieselben Ereignisse), auch wenn mehrere
  Schreiber gleichzeitig aendern.

Alle Konten und Buchungen sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

import pytest

from finue.services import transaction_edit as te
from support import (
    add_account,
    add_transaction,
    drain,
    read_transaction,
    run_parallel,
    session_factory,
)

pytestmark = pytest.mark.concurrency


def test_batch_mit_einem_konflikt_f_con_03(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-03: Ein Konflikt-Eintrag bricht den Rest nicht ab; die uebrigen aendern sich.

    Drei Buchungen: zwei mit korrekter Version, eine mit veralteter. Der Batch
    liefert fuer die eine ``konflikt`` mit aktuellem Stand und fuer die beiden
    anderen ``geaendert``; nur die beiden anderen sind in der Datenbank gesetzt.
    """

    account_id = add_account(client)
    ids = [add_transaction(client, account_id) for _ in range(3)]
    payload = {
        "ids": ids,
        "action": "checked",
        "value": True,
        "versions": {ids[0]: 1, ids[1]: 99, ids[2]: 1},
    }

    response = client.post("/api/transactions/batch", json=payload)

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    outcomes = {item["id"]: item["outcome"] for item in results}
    assert outcomes[ids[0]] == "geaendert"
    assert outcomes[ids[1]] == "konflikt"
    assert outcomes[ids[2]] == "geaendert"

    conflict = next(item for item in results if item["id"] == ids[1])
    assert conflict["current"]["id"] == ids[1]
    assert conflict["current"]["checked"] is False

    assert read_transaction(client, ids[0]).checked is True
    assert read_transaction(client, ids[2]).checked is True
    assert read_transaction(client, ids[1]).checked is False


def test_zweiter_sse_client_erhaelt_ereignisse_f_con_04(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-04: Zwei SSE-Clients erhalten dieselben Ereignisse paralleler Schreiber.

    Acht Threads aendern je eine Buchung gleichzeitig. Beide verbundenen Clients
    muessen dieselben Ereignisse mit denselben, streng steigenden IDs sehen.
    """

    account_id = add_account(client)
    transaction_ids = [add_transaction(client, account_id) for _ in range(8)]
    services = client.app.state.services
    bus = services.event_bus
    broadcaster = services.sse_broadcaster

    first = broadcaster.open()
    second = broadcaster.open()

    def worker(index: int) -> te.TransactionFlagResult:
        with session_factory(client)() as session:
            return te.set_flag(
                session,
                transaction_ids[index],
                flag="checked",
                value=True,
                origin="ui",
                events=bus,
            )

    _results, errors = run_parallel(len(transaction_ids), worker)
    assert errors == [], errors

    first_frames = drain(first)
    second_frames = drain(second)

    expected_ids = list(range(1, len(transaction_ids) + 1))
    assert [frame.event_id for frame in first_frames] == expected_ids
    assert [frame.event_id for frame in second_frames] == expected_ids
    assert {frame.event_type for frame in first_frames} == {"transaction.changed"}
    assert [frame.data for frame in first_frames] == [frame.data for frame in second_frames]

    broadcaster.discard(first)
    broadcaster.discard(second)
    assert broadcaster.client_count == 0
