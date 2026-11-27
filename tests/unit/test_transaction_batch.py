"""Tests der Batch-Aktion auf eine Buchungsauswahl (T-110).

Referenziert F-CON-03 (Batch liefert ein Ergebnis je Buchung; Konflikte brechen
den Rest nicht ab), F-ABS-02 (Setzen/Zuruecksetzen als Batch-Aktion auf die
Auswahl), F-ABS-06 (Kennzeichen, Notiz und externe Referenz bleiben bei Sync und
Re-Import erhalten), F-CON-05 (ein Sync aendert nie Nutzerfelder), F-CON-01
(Setz-Semantik, idempotent), F-CON-02 (optimistisches Sperren, 409 mit Stand je
Eintrag) und F-MCP-09/F-CON-04 (Audit und Ereignis ueber den Kern aus T-108/109).

Der Batch ruft je Eintrag genau den Kern aus T-108/T-109 auf; es gibt keine
zweite Schreibpfad-Logik. Alle Konten, Buchungen und Laeufe sind erfundene
Testdaten (D8); es gibt keine echten Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.audit import AuditLog
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.events import Event
from finue.services.fints_types import FetchedTransaction, SyncAccountData
from finue.services.import_run import execute_import
from finue.services.search_text import build_search_text
from finue.services.sync_persist import persist_account_sync

IBAN = "DE89370400440532013000"
BOOKING_DATE = date(2026, 1, 15)
AMOUNT_CENTS = -4883
COUNTERPARTY = "Max Mustermann"
PURPOSE = "SEPA Ueberweisung"

_sequence = 0


def _next_fingerprint() -> str:
    """Liefert einen eindeutigen, 64-stelligen Fingerprint (F-DUP-01)."""

    global _sequence
    _sequence += 1
    return f"{_sequence:064x}"


def _session(client: TestClient) -> Session:  # type: ignore[no-untyped-def]
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _add_account(client: TestClient, name: str = "Konto", **overrides: object) -> int:
    """Legt ein Konto an und liefert seine ID (erfundene Testdaten, D8)."""

    fields: dict[str, object] = {"name": name, "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    with _session(client) as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


def _add_transaction(client: TestClient, account_id: int, **overrides: object) -> int:
    """Legt eine Testbuchung mit gefuellter Suchspalte an (F-SUC-01)."""

    fields: dict[str, object] = {
        "account_id": account_id,
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
        "fingerprint": _next_fingerprint(),
        "occurrence_index": 0,
    }
    fields.update(overrides)
    fields["search_text"] = build_search_text(
        counterparty=fields["counterparty"] if isinstance(fields["counterparty"], str) else None,
        purpose=fields["purpose"] if isinstance(fields["purpose"], str) else None,
        note=fields["note"] if isinstance(fields["note"], str) else None,
        counterparty_iban=fields["counterparty_iban"] if isinstance(fields["counterparty_iban"], str) else None,
        external_ref=fields["external_ref"] if isinstance(fields["external_ref"], str) else None,
    )
    with _session(client) as session:
        transaction = Transaction(**fields)
        session.add(transaction)
        session.commit()
        return transaction.id


def _matching_fingerprint(account_id: int) -> str:
    """Fingerprint der Testbuchung, die Sync und CSV gemeinsam treffen (F-DUP-01)."""

    return compute_fingerprint(
        account_id=account_id,
        booking_date=BOOKING_DATE,
        value_date=None,
        amount_cents=AMOUNT_CENTS,
        currency="EUR",
        counterparty=COUNTERPARTY,
        counterparty_iban=IBAN,
        purpose=PURPOSE,
    )


def _seed_matching_transaction(client: TestClient) -> tuple[int, int]:
    """Legt Konto und Bestandsbuchung mit gueltigem Fingerprint an (Fresh-DB, D8)."""

    account_id = _add_account(client, "Girokonto")
    transaction_id = _add_transaction(
        client,
        account_id,
        booking_date=BOOKING_DATE,
        amount_cents=AMOUNT_CENTS,
        counterparty=COUNTERPARTY,
        counterparty_iban=IBAN,
        purpose=PURPOSE,
        origin="fints",
        fingerprint=_matching_fingerprint(account_id),
    )
    return account_id, transaction_id


def _fetched() -> FetchedTransaction:
    """FetchedTransaction, die zur Bestandsbuchung passt (F-CON-05, D8)."""

    return FetchedTransaction(
        booking_date=BOOKING_DATE,
        amount_cents=AMOUNT_CENTS,
        currency="EUR",
        status="gebucht",
        value_date=None,
        counterparty=COUNTERPARTY,
        counterparty_iban=IBAN,
        purpose=PURPOSE,
        raw_reference=None,
    )


def _csv_row() -> dict[str, str]:
    """CSV-Zeile, die zur Bestandsbuchung passt (F-IMP-06, D8)."""

    return {
        "Datum": "15.01.2026",
        "Betrag": "-48,83",
        "Gegenpartei": COUNTERPARTY,
        "IBAN": IBAN,
        "Zweck": PURPOSE,
        "Waehrung": "EUR",
    }


def _csv_mapping() -> dict[str, str]:
    """Mapping fuer die CSV-Testzeile."""

    return {
        "Datum": "booking_date",
        "Betrag": "amount_cents",
        "Gegenpartei": "counterparty",
        "IBAN": "counterparty_iban",
        "Zweck": "purpose",
        "Waehrung": "currency",
    }


def _reload(client: TestClient, transaction_id: int) -> Transaction:
    """Laedt eine Buchung direkt aus der Datenbank (Pruefung der Spalten)."""

    with _session(client) as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        return transaction


def _audit_rows(client: TestClient, transaction_id: int) -> list[AuditLog]:
    """Liefert die Audit-Eintraege zu einer Buchung (F-MCP-09)."""

    with _session(client) as session:
        return list(
            session.scalars(
                select(AuditLog).where(AuditLog.entity == "transaction", AuditLog.entity_id == transaction_id)
            )
        )


def _batch(client: TestClient, **payload: object) -> dict[str, object]:
    """Ruft die Batch-Route auf und liefert den JSON-Koerper."""

    response = client.post("/api/transactions/batch", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _results(body: dict[str, object]) -> list[dict[str, object]]:
    """Liefert die Ergebnisliste aus der Batch-Antwort."""

    results = body["results"]
    assert isinstance(results, list)
    return results


@pytest.fixture
def captured_events(client: TestClient) -> Iterator[list[Event]]:
    """Sammelt die auf dem Ereignisbus veroeffentlichten Ereignisse (F-CON-04)."""

    seen: list[Event] = []
    event_bus = client.app.state.services.event_bus
    unsubscribe = event_bus.subscribe(seen.append)
    try:
        yield seen
    finally:
        unsubscribe()


# --------------------------------------------------------------- F-ABS-02/03


def test_batch_setzt_kennzeichen_auf_auswahl(client: TestClient) -> None:
    """F-ABS-02: Eine Batch-Aktion setzt das Kennzeichen auf alle gewaehlten Buchungen."""

    account_id = _add_account(client)
    ids = [_add_transaction(client, account_id) for _ in range(3)]

    body = _batch(client, ids=ids, action="checked", value=True)

    results = _results(body)
    assert [item["outcome"] for item in results] == ["geaendert"] * 3
    for transaction_id in ids:
        assert _reload(client, transaction_id).checked is True
        assert _reload(client, transaction_id).checked_origin == "ui"


def test_batch_erhaelt_die_reihenfolge_der_ids(client: TestClient) -> None:
    """F-CON-03: Die Ergebnisfolge bleibt in der Reihenfolge der Anfrage."""

    account_id = _add_account(client)
    first = _add_transaction(client, account_id)
    second = _add_transaction(client, account_id)
    third = _add_transaction(client, account_id)

    body = _batch(client, ids=[third, first, second], action="checked", value=True)

    assert [item["id"] for item in _results(body)] == [third, first, second]


def test_batch_idempotent_liefert_unveraendert(client: TestClient) -> None:
    """F-CON-01/F-CON-03: Ein bereits gesetzter Wert liefert ``unveraendert`` ohne Versionssprung."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    first = _batch(client, ids=[transaction_id], action="checked", value=True)
    assert _results(first)[0]["outcome"] == "geaendert"
    version_after_first = client.get(f"/api/transactions/{transaction_id}").json()["version"]

    second = _batch(client, ids=[transaction_id], action="checked", value=True)
    assert _results(second)[0]["outcome"] == "unveraendert"
    assert client.get(f"/api/transactions/{transaction_id}").json()["version"] == version_after_first
    assert len(_audit_rows(client, transaction_id)) == 1


# --------------------------------------------------------------- F-CON-03


def test_konflikt_bricht_den_batch_nicht_ab(client: TestClient) -> None:
    """F-CON-03/F-CON-02: Ein Konflikt je Eintrag stoppt die uebrigen Buchungen nicht."""

    account_id = _add_account(client)
    first = _add_transaction(client, account_id)
    second = _add_transaction(client, account_id)
    third = _add_transaction(client, account_id)

    body = _batch(
        client,
        ids=[first, second, third],
        action="checked",
        value=True,
        versions={first: 5},  # veraltet (Bestand hat Version 1)
    )

    results = _results(body)
    assert [item["id"] for item in results] == [first, second, third]
    assert [item["outcome"] for item in results] == ["konflikt", "geaendert", "geaendert"]
    conflict = results[0]
    assert conflict["current"]["id"] == first
    assert conflict["current"]["version"] == 1
    assert conflict["current"]["checked"] is False
    assert conflict["message"]
    # Der Rest des Batches wurde trotzdem veraendert (F-CON-03).
    assert _reload(client, first).checked is False
    assert _reload(client, second).checked is True
    assert _reload(client, third).checked is True


def test_versionsangabe_wird_je_eintrag_geprueft(client: TestClient) -> None:
    """F-CON-02/03: Nur der Eintrag mit veralteter Version ist ein Konflikt."""

    account_id = _add_account(client)
    first = _add_transaction(client, account_id)
    second = _add_transaction(client, account_id)

    body = _batch(
        client,
        ids=[first, second],
        action="checked",
        value=True,
        versions={first: 1, second: 1},
    )

    assert [item["outcome"] for item in _results(body)] == ["geaendert", "geaendert"]


def test_fehler_eines_eintrags_bricht_den_batch_nicht_ab(client: TestClient) -> None:
    """F-CON-03: Eine unbekannte Buchung wird als Fehler gemeldet, der Rest laeuft weiter."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    body = _batch(client, ids=[9999, transaction_id], action="checked", value=True)

    results = _results(body)
    assert results[0]["id"] == 9999
    assert results[0]["outcome"] == "fehler"
    assert "nicht gefunden" in str(results[0]["message"])
    assert results[1]["outcome"] == "geaendert"
    assert _reload(client, transaction_id).checked is True


def test_vorgemerkte_abstimmung_ist_fehler_im_batch(client: TestClient) -> None:
    """F-ABS-01/03: Eine vorgemerkte Buchung wird im Batch als Fehler gemeldet."""

    account_id = _add_account(client)
    pending = _add_transaction(client, account_id, status="vorgemerkt")
    booked = _add_transaction(client, account_id)

    body = _batch(client, ids=[pending, booked], action="reconciled", value=True)

    results = _results(body)
    assert results[0]["outcome"] == "fehler"
    assert "vorgemerkt" in str(results[0]["message"])
    assert results[1]["outcome"] == "geaendert"
    assert _reload(client, booked).reconciled is True


# --------------------------------------------------------------- Notiz


def test_batch_notiz_ersetzen(client: TestClient) -> None:
    """F-SEL-03: ``note_set`` ersetzt die Notiz auf allen gewaehlten Buchungen."""

    account_id = _add_account(client)
    ids = [_add_transaction(client, account_id, note="alt") for _ in range(2)]

    body = _batch(client, ids=ids, action="note_set", value="neu")

    assert [item["outcome"] for item in _results(body)] == ["geaendert", "geaendert"]
    for transaction_id in ids:
        assert _reload(client, transaction_id).note == "neu"


def test_batch_notiz_ersetzen_mit_null_loescht(client: TestClient) -> None:
    """F-CON-02: ``note_set`` mit ``value: null`` loescht die Notiz."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="alt")

    _batch(client, ids=[transaction_id], action="note_set", value=None)

    assert _reload(client, transaction_id).note is None


def test_batch_notiz_anhaengen_atomar_und_suchspalte(client: TestClient) -> None:
    """F-CON-01/F-ABS-05: ``note_append`` haengt an und baut die Suchspalte neu."""

    account_id = _add_account(client)
    ids = [_add_transaction(client, account_id, purpose="Miete", note="Start") for _ in range(2)]

    body = _batch(client, ids=ids, action="note_append", value="Nebenkosten")

    assert [item["outcome"] for item in _results(body)] == ["geaendert", "geaendert"]
    for transaction_id in ids:
        assert _reload(client, transaction_id).note == "Start\nNebenkosten"
        assert "nebenkosten" in _reload(client, transaction_id).search_text


# --------------------------------------------------------------- Grenzen/Validierung


def test_batch_ueber_grenze_ist_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """F-CON-03: Die Batchgrenze ist zentral konfiguriert und wird geprueft."""

    settings = client.app.state.services.settings
    monkeypatch.setattr(settings, "api_max_batch_size", 2)
    account_id = _add_account(client)
    ids = [_add_transaction(client, account_id) for _ in range(3)]

    response = client.post("/api/transactions/batch", json={"ids": ids, "action": "checked", "value": True})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_batch_genau_an_der_grenze_ist_erlaubt(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """F-CON-03: Ein Batch genau an der Grenze ist zulaessig."""

    settings = client.app.state.services.settings
    monkeypatch.setattr(settings, "api_max_batch_size", 2)
    account_id = _add_account(client)
    ids = [_add_transaction(client, account_id) for _ in range(2)]

    body = _batch(client, ids=ids, action="checked", value=True)

    assert [item["outcome"] for item in _results(body)] == ["geaendert", "geaendert"]


def test_batch_leere_liste_ist_422(client: TestClient) -> None:
    """F-CON-03: Eine leere Auswahl wird abgelehnt."""

    response = client.post("/api/transactions/batch", json={"ids": [], "action": "checked", "value": True})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_batch_doppelte_ids_sind_422(client: TestClient) -> None:
    """F-CON-03: Doppelte Buchungs-IDs werden abgelehnt."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.post(
        "/api/transactions/batch",
        json={"ids": [transaction_id, transaction_id], "action": "checked", "value": True},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "checked", "value": "ja"},
        {"action": "reconciled", "value": "ja"},
        {"action": "note_append", "value": ""},
        {"action": "note_append", "value": "   "},
        {"action": "toggle", "value": True},
    ],
)
def test_batch_ungueltiger_body_ist_422(client: TestClient, payload: dict[str, object]) -> None:
    """F-CON-03: Ungueltige Aktion oder Wert werden abgelehnt."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)
    payload = {"ids": [transaction_id], **payload}

    response = client.post("/api/transactions/batch", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_batch_version_null_ist_422(client: TestClient) -> None:
    """F-CON-02: Eine Versionsnummer < 1 wird abgelehnt."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.post(
        "/api/transactions/batch",
        json={"ids": [transaction_id], "action": "checked", "value": True, "versions": {str(transaction_id): 0}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --------------------------------------------------------------- Audit/Ereignis


def test_batch_audit_je_aenderung(client: TestClient) -> None:
    """F-MCP-09: Je tatsaechlicher Aenderung entsteht ein Audit-Eintrag."""

    account_id = _add_account(client)
    first = _add_transaction(client, account_id)
    second = _add_transaction(client, account_id)

    _batch(client, ids=[first, second], action="checked", value=True)
    _batch(client, ids=[first, second], action="checked", value=True)  # idempotent

    assert len(_audit_rows(client, first)) == 1
    assert len(_audit_rows(client, second)) == 1


def test_batch_ereignis_je_aenderung(client: TestClient, captured_events: list[Event]) -> None:
    """F-CON-04: Je tatsaechlicher Aenderung wird ``transaction.changed`` gemeldet."""

    account_id = _add_account(client)
    first = _add_transaction(client, account_id)
    second = _add_transaction(client, account_id)

    _batch(client, ids=[first, second], action="checked", value=True)
    _batch(client, ids=[first, second], action="checked", value=True)  # idempotent, kein Ereignis

    events = [event for event in captured_events if event.type == "transaction.changed"]
    assert len(events) == 2
    assert {event.get("transaction") for event in events} == {first, second}
    assert all(event.get("account") == account_id for event in events)


# ------------------------------------------------ F-ABS-06 Regression Sync/Re-Import


def test_batch_aenderungen_ueberleben_sync_und_reimport(client: TestClient) -> None:
    """F-ABS-06/F-CON-05: Batch-Aenderungen bleiben bei Sync und Re-Import erhalten.

    Nach Batch-Aenderungen an Kennzeichen und Notiz sowie einer externen
    Referenz (Einzelroute) duerfen ein FinTS-Sync mit identischer Zeile und ein
    CSV-Re-Import dieselben Werte und die Version nicht aendern.
    """

    account_id, transaction_id = _seed_matching_transaction(client)

    _batch(client, ids=[transaction_id], action="checked", value=True)
    _batch(client, ids=[transaction_id], action="reconciled", value=True)
    _batch(client, ids=[transaction_id], action="note_set", value="Eigene Notiz")
    ref = client.put(
        f"/api/transactions/{transaction_id}/external-ref",
        json={"value": "USER-REF", "version": _reload(client, transaction_id).version},
    )
    assert ref.status_code == 200
    version_before = _reload(client, transaction_id).version

    # FinTS-Sync mit identischer Buchung (F-CON-05).
    with _session(client) as session:
        sync_run = persist_account_sync(
            session,
            account_id,
            SyncAccountData(account_ref=IBAN, balance=None, transactions=(_fetched(),)),
            "job-1",
        )

    assert sync_run.new_count == 0
    assert sync_run.skipped_count == 1
    after_sync = _reload(client, transaction_id)
    assert after_sync.checked is True
    assert after_sync.reconciled is True
    assert after_sync.note == "Eigene Notiz"
    assert after_sync.external_ref == "USER-REF"
    assert after_sync.version == version_before

    # CSV-Re-Import derselben Buchung (F-ABS-06).
    with _session(client) as session:
        import_run = execute_import(account_id, [_csv_row()], _csv_mapping(), "csv", session)

    assert import_run.new_count == 0
    assert import_run.skipped_count == 1
    after_import = _reload(client, transaction_id)
    assert after_import.checked is True
    assert after_import.reconciled is True
    assert after_import.note == "Eigene Notiz"
    assert after_import.external_ref == "USER-REF"
    assert after_import.version == version_before
