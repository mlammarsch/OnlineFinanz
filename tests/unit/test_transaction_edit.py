"""Tests des Edit-Kerns und Buchungsdetails (T-108).

Referenziert F-ABS-01 (Kennzeichen mit Zeitpunkt und Herkunft, Vormerkungen
nicht abstimmbar), F-ABS-02 (Setzen/Zuruecksetzen einzeln), F-CON-01
(Setz-Semantik, idempotent, kein Umschalten), F-CON-02 (optimistisches Sperren,
409 mit aktuellem Stand), F-CON-04 (Ereignis ``transaction.changed``),
F-MCP-09 (Audit-Eintrag je Aenderung) und F-DET-01 (Einzelbuchung mit Quelle,
Import-Lauf und Kennzeichen-Herkunft).

Alle Konten, Buchungen und Laeufe sind erfundene Testdaten (D8); es gibt keine
echten Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.audit import AuditLog
from finue.models.import_run import ImportRun
from finue.models.transaction import Transaction
from finue.services import transaction_edit as te
from finue.services.events import Event
from finue.services.search_text import build_search_text

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
        "occurrence_index": 1,
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


def _add_import_run(client: TestClient, account_id: int, *, kind: str = "csv", status: str = "erfolgreich") -> int:
    """Legt einen Import-Lauf an und liefert seine ID (erfundene Testdaten, D8)."""

    with _session(client) as session:
        run = ImportRun(account_id=account_id, kind=kind, status=status)
        session.add(run)
        session.commit()
        return run.id


def _audit_rows(client: TestClient, transaction_id: int) -> list[AuditLog]:
    """Liefert die Audit-Eintraege zu einer Buchung (F-MCP-09)."""

    with _session(client) as session:
        return list(
            session.scalars(
                select(AuditLog).where(AuditLog.entity == "transaction", AuditLog.entity_id == transaction_id)
            )
        )


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


# ------------------------------------------------------------- F-ABS-01/02


def test_geprueft_setzen_speichert_wert_zeitpunkt_und_herkunft(client: TestClient) -> None:
    """F-ABS-01/02: "geprueft" setzen speichert Wert, Zeitpunkt und Herkunft ui."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})

    assert response.status_code == 200
    body = response.json()
    assert body["checked"] is True
    assert body["checked_origin"] == "ui"
    assert body["checked_at"] is not None
    assert body["version"] == 2


def test_abstimmen_setzen_und_zuruecksetzen(client: TestClient) -> None:
    """F-ABS-02: "abgestimmt" laesst sich setzen und zuruecksetzen (kein Umschalten)."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    first = client.put(f"/api/transactions/{transaction_id}/reconciled", json={"value": True}).json()
    assert first["reconciled"] is True
    assert first["reconciled_origin"] == "ui"
    assert first["version"] == 2

    second = client.put(f"/api/transactions/{transaction_id}/reconciled", json={"value": False}).json()
    assert second["reconciled"] is False
    assert second["reconciled_origin"] == "ui"
    assert second["version"] == 3


def test_vorgemerkte_buchung_geprueft_erlaubt_abstimmen_422(client: TestClient) -> None:
    """F-ABS-01: Vorgemerkte Buchungen sind pruefbar, aber nicht abstimmbar (422 Klartext)."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, status="vorgemerkt")

    checked = client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})
    assert checked.status_code == 200
    assert checked.json()["checked"] is True

    reconciled = client.put(f"/api/transactions/{transaction_id}/reconciled", json={"value": True})
    assert reconciled.status_code == 422
    body = reconciled.json()
    assert body["code"] == "domain_error"
    assert "vorgemerkt" in body["message"]


# ---------------------------------------------------------------- F-CON-01


def test_setz_semantik_idempotent_ohne_versionssprung(client: TestClient) -> None:
    """F-CON-01: Ein bereits gesetzter Wert aendert nichts (kein Versionssprung, kein Audit)."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    first = client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True}).json()
    second = client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True}).json()

    assert second["checked"] is True
    assert second["version"] == first["version"] == 2
    assert second["checked_at"] == first["checked_at"]
    # Genau ein Audit-Eintrag fuer die eine tatsaechliche Aenderung (F-MCP-09).
    assert len(_audit_rows(client, transaction_id)) == 1


# ---------------------------------------------------------------- F-CON-02


def test_versionskonflikt_ist_409_mit_aktuellem_stand(client: TestClient) -> None:
    """F-CON-02: Eine veraltete Version liefert 409 mit dem aktuellen Stand."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True, "version": 5})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == transaction_id
    assert body["current"]["version"] == 1
    assert body["current"]["checked"] is False


def test_service_versionskonflikt(client: TestClient) -> None:
    """F-CON-02: Der Kern meldet einen veralteten Stand als ``TransactionVersionConflict``."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    with _session(client) as session:
        with pytest.raises(te.TransactionVersionConflict):
            te.set_flag(
                session,
                transaction_id,
                flag="checked",
                value=True,
                origin="ui",
                expected_version=99,
            )


def test_unbekannte_buchung_ist_404(client: TestClient) -> None:
    """F-CON-02: Eine unbekannte Buchung liefert 404 (Detail und Kennzeichen)."""

    assert client.get("/api/transactions/9999").status_code == 404
    assert client.put("/api/transactions/9999/checked", json={"value": True}).status_code == 404


@pytest.mark.parametrize("payload", [{"value": "ja"}, {"value": True, "version": 0}])
def test_ungueltiger_body_ist_422(client: TestClient, payload: dict[str, object]) -> None:
    """F-CON-02: Ein ungueltiger Wert oder eine Version < 1 liefert 422."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)
    response = client.put(f"/api/transactions/{transaction_id}/checked", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------- F-CON-04


def test_ereignis_transaction_changed(client: TestClient, captured_events: list[Event]) -> None:
    """F-CON-04: Eine Aenderung veroeffentlicht ``transaction.changed`` mit IDs."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})

    events = [event for event in captured_events if event.type == "transaction.changed"]
    assert len(events) == 1
    assert events[0].get("transaction") == transaction_id
    assert events[0].get("account") == account_id


def test_ereignis_nur_bei_aenderung(client: TestClient, captured_events: list[Event]) -> None:
    """F-CON-01/04: Ein idempotenter Aufruf veroeffentlicht kein Ereignis."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})
    client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})

    assert len([event for event in captured_events if event.type == "transaction.changed"]) == 1


# ---------------------------------------------------------------- F-MCP-09


def test_audit_eintrag_je_aenderung(client: TestClient) -> None:
    """F-MCP-09/F-ABS-01: Je Aenderung entsteht ein Audit-Eintrag mit Herkunft und Vorher/Nachher."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})

    rows = _audit_rows(client, transaction_id)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.source == "ui"
    assert entry.action == "updated"
    assert entry.account_id == account_id
    assert entry.changes == {"checked": {"from": False, "to": True}}


# ---------------------------------------------------------------- Herkunft/Origin


def test_herkunft_mcp_und_abgelehnte_herkunft(client: TestClient) -> None:
    """F-ABS-01/D10: Der Kern speichert die uebergebene Herkunft und lehnt unbekannte ab."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    with _session(client) as session:
        result = te.set_flag(session, transaction_id, flag="checked", value=True, origin="mcp")
        assert result.changed is True
    detail = client.get(f"/api/transactions/{transaction_id}").json()
    assert detail["checked_origin"] == "mcp"

    with _session(client) as session:
        with pytest.raises(te.TransactionEditValidationError):
            te.set_flag(session, transaction_id, flag="checked", value=False, origin="extern")


# ---------------------------------------------------------------- F-DET-01


def test_detail_liefert_alle_felder(client: TestClient) -> None:
    """F-DET-01: Das Detail liefert Quelle, Betrag, Zweck, Kennzeichen-Herkunft und Version."""

    account_id = _add_account(client, "Girokonto")
    transaction_id = _add_transaction(
        client,
        account_id,
        counterparty="REWE Markt",
        counterparty_iban="DE02120300000000202051",
        purpose="Wocheneinkauf",
        amount_cents=-4883,
        origin="fints",
        value_date=date(2026, 1, 9),
        external_ref="REF-7",
    )
    client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})

    body = client.get(f"/api/transactions/{transaction_id}").json()

    assert body["account_name"] == "Girokonto"
    assert body["counterparty"] == "REWE Markt"
    assert body["counterparty_iban"] == "DE02120300000000202051"
    assert body["purpose"] == "Wocheneinkauf"
    assert body["amount_cents"] == -4883
    assert body["currency"] == "EUR"
    assert body["booking_date"] == "2026-01-10"
    assert body["value_date"] == "2026-01-09"
    assert body["origin"] == "fints"
    assert body["external_ref"] == "REF-7"
    assert body["checked"] is True
    assert body["checked_origin"] == "ui"
    assert body["checked_at"] is not None
    assert body["reconciled"] is False
    assert body["version"] == 2


def test_detail_zeigt_import_lauf(client: TestClient) -> None:
    """F-DET-01: Das Detail verweist auf den passenden Import-Lauf des Kontos."""

    account_id = _add_account(client)
    run_id = _add_import_run(client, account_id, kind="csv", status="erfolgreich")
    transaction_id = _add_transaction(client, account_id, origin="csv")

    body = client.get(f"/api/transactions/{transaction_id}").json()

    assert body["import_run"]["id"] == run_id
    assert body["import_run"]["kind"] == "csv"
    assert body["import_run"]["status"] == "erfolgreich"
    assert body["import_run"]["started_at"] is not None


def test_detail_ohne_passenden_lauf(client: TestClient) -> None:
    """F-DET-01: Ohne passenden Lauf bleibt der Import-Lauf-Verweis ``None``."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, origin="fints")

    body = client.get(f"/api/transactions/{transaction_id}").json()

    assert body["import_run"] is None


def test_service_detail_setzt_zeitstempel_typ(client: TestClient) -> None:
    """F-DET-01/N-DB-02: Der gemeldete Zeitpunkt ist ein aware UTC-Datetime-Wert."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)
    client.put(f"/api/transactions/{transaction_id}/checked", json={"value": True})

    with _session(client) as session:
        detail = te.transaction_detail(session, transaction_id)

    assert isinstance(detail.checked_at, datetime)
    assert detail.checked_at.tzinfo is not None
