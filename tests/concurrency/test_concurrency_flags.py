"""Concurrency-Tests der Kennzeichen und Notizen (T-104, F-CON-01/02, N-DB-04).

Der Block faehrt die schreibenden Service-Funktionen gegen eine **datei-basierte**
SQLite-Datenbank im WAL-Modus mit vielen eigenen Sitzungen gleichzeitig an. Die
Threads synchronisieren sich ueber Barrieren, nie ueber ``sleep``.

Referenzierte Anforderungen:

* **F-CON-01** Kennzeichen und Notizanhang werden gesetzt, nicht umgeschaltet:
  paralleles Setzen desselben Werts fuehrt zum selben Ergebnis und hebt die
  Version hoechstens einmal; 20 parallele Anhaenge verlieren keinen Text.
* **F-CON-02** Optimistisches Sperren: zwei Notiz-Ersetzungen mit derselben
  Version ergeben genau einen Erfolg und einen Konflikt mit aktuellem Stand.
* **N-DB-04** Kein ``database is locked`` bei gleichzeitigen Schreibern (WAL +
  ``busy_timeout``).

Alle Konten und Buchungen sind erfundene Testdaten (D8).
"""

from __future__ import annotations

import pytest

from finue.services import accounts as accounts_service
from finue.services import transaction_edit as te
from support import (
    add_account,
    add_transaction,
    any_locked_error,
    audit_count,
    read_transaction,
    run_parallel,
    session_factory,
)

pytestmark = pytest.mark.concurrency


def test_paralleles_setzen_desselben_kennzeichens_f_con_01(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: Gleichzeitiges Setzen desselben Kennzeichens ergibt ein Ergebnis.

    12 Threads setzen ``checked`` auf wahr. Das Ergebnis ist ueberall derselbe
    Wert, die Version steigt hoechstens einmal (genau einmal), und nur die eine
    wirksame Aenderung erzeugt einen Audit-Eintrag.
    """

    account_id = add_account(client)
    transaction_id = add_transaction(client, account_id)

    def worker(_index: int) -> te.TransactionFlagResult:
        with session_factory(client)() as session:
            return te.set_flag(session, transaction_id, flag="checked", value=True, origin="ui")

    results, errors = run_parallel(12, worker)

    assert errors == [], errors
    assert all(result is not None for result in results)
    assert all(result.value is True and result.flag == "checked" for result in results if result is not None)

    stored = read_transaction(client, transaction_id)
    assert stored.checked is True
    assert stored.version == 2, "genau ein Versionssprung (F-CON-01)"
    assert audit_count(client, transaction_id) == 1, "nur die wirksame Aenderung protokolliert"


def test_20_parallele_notizanhaenge_f_con_01(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-01: 20 parallele Anhaenge verlieren keinen Text; jeder Text genau einmal.

    Der Anhang ist ein einzelner UPDATE-Ausdruck; die SQLite-Schreibsperre
    serialisiert die Anhaenge, sodass jeder Text genau einmal enthalten ist und
    die Version um genau die Anzahl erfolgreicher Anhaenge steigt.
    """

    account_id = add_account(client)
    transaction_id = add_transaction(client, account_id, note="Start")
    count = 20

    def worker(index: int) -> te.TransactionFieldResult:
        with session_factory(client)() as session:
            return te.append_note(session, transaction_id, text=f"Zeile-{index:02d}", origin="ui")

    results, errors = run_parallel(count, worker)

    assert errors == [], errors
    assert all(result is not None for result in results)

    stored = read_transaction(client, transaction_id)
    assert stored.note is not None
    assert stored.note.startswith("Start")
    for index in range(count):
        assert stored.note.count(f"Zeile-{index:02d}") == 1, stored.note
    assert stored.version == 1 + count


def test_zwei_notiz_ersetzungen_gleicher_version_f_con_02(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-02: Zwei Ersetzungen mit derselben Version -> ein Erfolg, ein Konflikt.

    Der Konflikt traegt den aktuellen Stand (Version 2). Der Datenbankstand
    entspricht dem erfolgreichen Aufruf.
    """

    account_id = add_account(client)
    transaction_id = add_transaction(client, account_id, note="alt")
    texts = ("Von-A", "Von-B")

    def worker(index: int) -> te.TransactionFieldResult:
        session = session_factory(client)()
        try:
            return te.set_note(session, transaction_id, text=texts[index], origin="ui", expected_version=1)
        finally:
            session.close()

    results, errors = run_parallel(2, worker)

    successes = [result for result in results if result is not None]
    conflicts = [error for error in errors if isinstance(error, te.TransactionVersionConflict)]
    assert len(successes) == 1, results
    assert len(conflicts) == 1, errors
    assert conflicts[0].current_version == 2
    assert not any_locked_error(errors)

    stored = read_transaction(client, transaction_id)
    assert stored.note == successes[0].value
    assert stored.note in texts
    assert stored.version == 2


def test_notiz_ersetzen_veraltete_version_http_409_f_con_02(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-02: Eine veraltete Version liefert 409 mit dem aktuellen Stand."""

    account_id = add_account(client)
    transaction_id = add_transaction(client, account_id, note="alt")

    first = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "neu", "version": 1})
    assert first.status_code == 200

    stale = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "noch neuer", "version": 1})
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == transaction_id
    assert body["current"]["version"] == 2
    assert body["current"]["note"] == "neu"


def test_konto_patch_konflikt_parallel(client) -> None:  # type: ignore[no-untyped-def]
    """F-CON-02: Zwei gleichzeitige Konto-Aenderungen ergeben einen Versionskonflikt."""

    account_id = add_account(client, "Konto")

    def worker(index: int) -> object:
        session = session_factory(client)()
        try:
            return accounts_service.update_account(
                session,
                account_id,
                expected_version=1,
                changes={"name": f"Name-{index}"},
            )
        finally:
            session.close()

    results, errors = run_parallel(2, worker)

    successes = [result for result in results if result is not None]
    conflicts = [error for error in errors if isinstance(error, accounts_service.AccountVersionConflict)]
    assert len(successes) == 1, results
    assert len(conflicts) == 1, errors

    # HTTP: veraltete Version -> 409 mit aktuellem Stand.
    response = client.patch(f"/api/accounts/{account_id}", json={"name": "X", "version": 1})
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["current"]["version"] == 2
