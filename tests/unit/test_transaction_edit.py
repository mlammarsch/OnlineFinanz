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

import threading
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


def _reload(client: TestClient, transaction_id: int) -> Transaction:
    """Laedt eine Buchung direkt aus der Datenbank (Pruefung der Spalten)."""

    with _session(client) as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        return transaction


def _search_text(client: TestClient, transaction_id: int) -> str:
    """Liefert die gespeicherte Suchspalte einer Buchung (F-SUC-01, F-ABS-05)."""

    return _reload(client, transaction_id).search_text


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


# ------------------------------------------------ F-CON-02 Notiz ersetzen (T-109)


def test_notiz_ersetzen_setzt_text_und_version(client: TestClient) -> None:
    """F-CON-02/F-ABS-05: PUT .../note ersetzt die Notiz und hebt die Version."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="alt")

    response = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "neu", "version": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["note"] == "neu"
    assert body["version"] == 2
    assert _reload(client, transaction_id).note == "neu"


def test_notiz_ersetzen_mit_null_loescht(client: TestClient) -> None:
    """F-CON-02: ``text: null`` loescht die Notiz."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="alt")

    response = client.put(f"/api/transactions/{transaction_id}/note", json={"text": None, "version": 1})

    assert response.status_code == 200
    assert response.json()["note"] is None
    assert _reload(client, transaction_id).note is None


def test_notiz_ersetzen_versionskonflikt_ist_409_mit_stand(client: TestClient) -> None:
    """F-CON-02: Eine veraltete Version liefert 409 mit dem aktuellen Stand."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="alt")

    response = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "neu", "version": 7})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == transaction_id
    assert body["current"]["version"] == 1
    assert body["current"]["note"] == "alt"
    assert _reload(client, transaction_id).note == "alt"


def test_notiz_ersetzen_gleicher_wert_ist_idempotent(client: TestClient) -> None:
    """F-CON-01/02: Derselbe Text aendert nichts (kein Versionssprung, kein Audit)."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    first = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "gleich", "version": 1}).json()
    second = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "gleich", "version": 2}).json()

    assert second["note"] == "gleich"
    assert second["version"] == first["version"] == 2
    assert len(_audit_rows(client, transaction_id)) == 1


def test_notiz_ersetzen_ohne_version_ist_422(client: TestClient) -> None:
    """F-CON-02: Bei den ersetzenden Routen ist ``version`` Pflicht."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.put(f"/api/transactions/{transaction_id}/note", json={"text": "ohne version"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# ------------------------------------------------ F-CON-01 Notiz anhaengen


def test_notiz_anhaengen_mit_trennzeichen(client: TestClient) -> None:
    """F-CON-01: Anhaengen setzt das festgelegte Trennzeichen (Zeilenumbruch)."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="Start")

    first = client.post(f"/api/transactions/{transaction_id}/note/append", json={"text": "Zweite"})
    assert first.status_code == 200
    assert first.json()["note"] == "Start\nZweite"
    assert first.json()["version"] == 2

    second = client.post(f"/api/transactions/{transaction_id}/note/append", json={"text": "Dritte"})
    assert second.json()["note"] == "Start\nZweite\nDritte"
    assert _reload(client, transaction_id).note == "Start\nZweite\nDritte"


def test_notiz_anhaengen_ohne_vorhandene_notiz(client: TestClient) -> None:
    """F-CON-01: Ohne Notiz entsteht kein fuehrendes Trennzeichen."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note=None)

    response = client.post(f"/api/transactions/{transaction_id}/note/append", json={"text": "Erste"})

    assert response.status_code == 200
    assert response.json()["note"] == "Erste"
    assert _reload(client, transaction_id).note == "Erste"


def test_paralleles_anhaengen_verliert_keinen_text(client: TestClient) -> None:
    """F-CON-01: Gleichzeitiges Anhaengen verliert keinen Text (einzelner UPDATE)."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="Start")
    fragments = [f"Zeile-{index}" for index in range(6)]
    barrier = threading.Barrier(len(fragments))
    errors: list[Exception] = []

    def worker(fragment: str) -> None:
        try:
            barrier.wait(timeout=15)
            with _session(client) as session:
                te.append_note(session, transaction_id, text=fragment, origin="ui")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(fragment,)) for fragment in fragments]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    final = _reload(client, transaction_id).note
    assert final is not None
    assert final.startswith("Start")
    for fragment in fragments:
        assert fragment in final
    # Jeder erfolgreiche Anhang hebt die Version genau einmal (F-CON-02).
    assert client.get(f"/api/transactions/{transaction_id}").json()["version"] == 1 + len(fragments)


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "   "}])
def test_leere_anhaengung_ist_422(client: TestClient, payload: dict[str, str]) -> None:
    """F-CON-01: Ein leerer oder reiner Leerraum-Anhang wird abgelehnt."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.post(f"/api/transactions/{transaction_id}/note/append", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_anhaengen_ignoriert_veralteten_leseversuch(client: TestClient) -> None:
    """F-CON-01: Der Anhang nutzt den DB-Stand, nicht einen veralteten Leseversuch.

    Zwei Sitzungen: die erste liest die Buchung (veralteter ORM-Schnappschuss),
    die zweite haengt zuerst an. Die erste Sitzung haengt danach an; weil das
    Anhaengen ein einzelner UPDATE-Ausdruck ist, greift es auf den aktuellen
    Datenbankstand zu und verliert den Text der zweiten Sitzung nicht. Eine
    naive Read-Modify-Write-Variante wuerde den zweiten Text ueberschreiben oder
    einen Versionskonflikt ausloesen.
    """

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, note="Start")

    session_a = _session(client)
    session_b = _session(client)
    try:
        # Starke Referenz halten: die ORM-Identitaetskarte haelt Objekte nur
        # schwach, sonst wuerde session_a die Buchung still neu laden.
        stale = session_a.get(Transaction, transaction_id)
        assert stale is not None
        assert stale.note == "Start"
        te.append_note(session_b, transaction_id, text="Zwei", origin="ui")
        te.append_note(session_a, transaction_id, text="Drei", origin="ui")
    finally:
        session_a.close()
        session_b.close()

    assert _reload(client, transaction_id).note == "Start\nZwei\nDrei"


# ------------------------------------------------ F-ABS-05 externe Referenz


def test_externe_referenz_setzen_und_versionspruefung(client: TestClient) -> None:
    """F-ABS-05/F-CON-02: Die externe Referenz laesst sich setzen und schuetzen."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    first = client.put(f"/api/transactions/{transaction_id}/external-ref", json={"value": "ACTUAL-42", "version": 1})
    assert first.status_code == 200
    assert first.json()["external_ref"] == "ACTUAL-42"
    assert first.json()["version"] == 2

    stale = client.put(f"/api/transactions/{transaction_id}/external-ref", json={"value": "X", "version": 1})
    assert stale.status_code == 409
    assert stale.json()["current"]["external_ref"] == "ACTUAL-42"


def test_externe_referenz_zu_lang_ist_422(client: TestClient) -> None:
    """F-ABS-05: Eine zu lange Referenz wird als Fachfehler abgelehnt."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    response = client.put(
        f"/api/transactions/{transaction_id}/external-ref",
        json={"value": "R" * 256, "version": 1},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


# ------------------------------------------------ Suchspalte und Audit


def test_search_text_wird_bei_notiz_und_referenz_aktualisiert(client: TestClient) -> None:
    """F-SUC-01/F-ABS-05: Notiz und externe Referenz landen in der Suchspalte."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(
        client,
        account_id,
        counterparty="REWE Markt",
        purpose="Wocheneinkauf",
    )

    client.put(f"/api/transactions/{transaction_id}/note", json={"text": "Urlaub Kroatien", "version": 1})
    after_note = _search_text(client, transaction_id)
    assert "rewe markt" in after_note
    assert "wocheneinkauf" in after_note
    assert "urlaub kroatien" in after_note

    client.put(f"/api/transactions/{transaction_id}/external-ref", json={"value": "ACTUAL-99", "version": 2})
    after_ref = _search_text(client, transaction_id)
    assert "actual-99" in after_ref
    assert "urlaub kroatien" in after_ref


def test_search_text_nach_anhaengen(client: TestClient) -> None:
    """F-CON-01/F-ABS-05: Auch angehaengter Text erscheint in der Suchspalte."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id, purpose="Miete")

    client.post(f"/api/transactions/{transaction_id}/note/append", json={"text": "Nebenkosten"})

    assert "nebenkosten" in _search_text(client, transaction_id)


def test_audit_und_ereignis_bei_notizaenderung(client: TestClient, captured_events: list[Event]) -> None:
    """F-MCP-09/F-CON-04: Eine Notizaenderung schreibt Audit und meldet das Ereignis."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    client.put(f"/api/transactions/{transaction_id}/note", json={"text": "Alle Jahre wieder", "version": 1})

    rows = _audit_rows(client, transaction_id)
    assert len(rows) == 1
    assert rows[0].source == "ui"
    assert rows[0].changes == {"note": {"from": None, "to": "Alle Jahre wieder"}}
    events = [event for event in captured_events if event.type == "transaction.changed"]
    assert len(events) == 1
    assert events[0].get("transaction") == transaction_id


def test_unbekannte_buchung_notiz_und_referenz_404(client: TestClient) -> None:
    """F-CON-02: Notiz- und Referenzrouten auf eine unbekannte Buchung liefern 404."""

    assert client.put("/api/transactions/9999/note", json={"text": "x", "version": 1}).status_code == 404
    assert client.post("/api/transactions/9999/note/append", json={"text": "x"}).status_code == 404
    assert client.put("/api/transactions/9999/external-ref", json={"value": "x", "version": 1}).status_code == 404


def test_service_notiz_und_referenz_herkunft_mcp(client: TestClient) -> None:
    """F-ABS-05/D10: Der Kern speichert die uebergebene Herkunft der Aenderung."""

    account_id = _add_account(client)
    transaction_id = _add_transaction(client, account_id)

    with _session(client) as session:
        note_result = te.set_note(session, transaction_id, text="aus mcp", origin="mcp")
        assert note_result.changed is True
        ref_result = te.set_external_ref(session, transaction_id, value="REF-1", origin="mcp")
        assert ref_result.value == "REF-1"

    rows = _audit_rows(client, transaction_id)
    assert {row.source for row in rows} == {"mcp"}
