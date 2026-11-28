"""Tests der Aufloesung von Verdachtsdubletten (T-096).

Referenziert F-DUP-03 (Verdachtspaar: gleiches Konto, gleicher Betrag,
Buchungstag +/- 2 Tage, abweichender Text), F-DUP-04 (Vormerkungen nehmen nicht
teil, Zusammenfuehren nur als Markierung), F-DUP-05 (Zusammenfuehren mit
Notiz-Zusammenfuehrung und Herkunftsvermerk, Uebernahme der Kennzeichen und der
externen Referenz, Rueckgaengig ueber das Protokoll, Ignorieren, nie
automatisch), F-CON-01/02 (Setz-Semantik, Versionspruefung), F-CON-04 (Ereignis
``duplicate.changed``) und F-MCP-09 (Audit-Eintrag mit Vorher-Stand).

Alle Konten, Buchungen und Entscheidungen sind erfundene Testdaten (D8); es gibt
keine echten Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.audit import AuditLog
from finue.models.duplicate import DuplicateDecision
from finue.models.transaction import Transaction
from finue.services.duplicate import (
    TransactionData,
    compute_fingerprint,
    find_suspected_duplicates,
    reconcile_multiset,
)
from finue.services.events import Event
from finue.services.search_text import build_search_text


def _session(client: TestClient) -> Session:
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


def _add_group(client: TestClient, name: str = "Gruppe") -> int:
    """Legt eine Konto-Gruppe an und liefert ihre ID (erfundene Testdaten, D8)."""

    with _session(client) as session:
        group = AccountGroup(name=name)
        session.add(group)
        session.commit()
        return group.id


def _add_transaction(client: TestClient, account_id: int, **overrides: object) -> int:
    """Legt eine Testbuchung mit echtem Fingerprint und gefuellter Suchspalte an."""

    fields: dict[str, object] = {
        "account_id": account_id,
        "booking_date": date(2026, 1, 10),
        "value_date": None,
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
        "occurrence_index": None,
    }
    fields.update(overrides)
    fields["search_text"] = build_search_text(
        counterparty=fields["counterparty"] if isinstance(fields["counterparty"], str) else None,
        purpose=fields["purpose"] if isinstance(fields["purpose"], str) else None,
        note=fields["note"] if isinstance(fields["note"], str) else None,
        counterparty_iban=fields["counterparty_iban"] if isinstance(fields["counterparty_iban"], str) else None,
        external_ref=fields["external_ref"] if isinstance(fields["external_ref"], str) else None,
    )
    # Echter F-DUP-01-Fingerprint, damit Verdachtspaare und Re-Import real arbeiten.
    booking_date = fields["booking_date"]
    value_date = fields["value_date"]
    amount_cents = fields["amount_cents"]
    counterparty = fields["counterparty"]
    counterparty_iban = fields["counterparty_iban"]
    purpose = fields["purpose"]
    assert isinstance(booking_date, date)
    assert value_date is None or isinstance(value_date, date)
    assert isinstance(amount_cents, int)
    fingerprint = compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=value_date,
        amount_cents=amount_cents,
        currency=str(fields["currency"]),
        counterparty=counterparty if isinstance(counterparty, str) else None,
        counterparty_iban=counterparty_iban if isinstance(counterparty_iban, str) else None,
        purpose=purpose if isinstance(purpose, str) else None,
    )
    fields["fingerprint"] = fingerprint
    with _session(client) as session:
        existing = session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.account_id == account_id, Transaction.fingerprint == fingerprint)
        )
        fields["occurrence_index"] = int(existing or 0) + 1
        transaction = Transaction(**fields)
        session.add(transaction)
        session.commit()
        return transaction.id


def _reload(client: TestClient, transaction_id: int) -> Transaction:
    """Laedt eine Buchung direkt aus der Datenbank (Pruefung der Spalten)."""

    with _session(client) as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        return transaction


def _update(client: TestClient, transaction_id: int, **fields: object) -> None:
    """Setzt Felder einer Buchung direkt in der Datenbank (Testaufbau)."""

    with _session(client) as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        for key, value in fields.items():
            setattr(transaction, key, value)
        session.commit()


def _version(client: TestClient, transaction_id: int) -> int:
    """Liefert die aktuelle Version einer Buchung (F-CON-02)."""

    return _reload(client, transaction_id).version


def _decision_rows(client: TestClient) -> list[DuplicateDecision]:
    """Liefert alle Dublettenentscheidungen (F-DUP-05)."""

    with _session(client) as session:
        return list(session.scalars(select(DuplicateDecision)))


def _audit_rows(client: TestClient, decision_id: int) -> list[AuditLog]:
    """Liefert die Audit-Eintraege zu einer Entscheidung (F-MCP-09)."""

    with _session(client) as session:
        return list(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.entity == "duplicate_decision",
                    AuditLog.entity_id == decision_id,
                )
            )
        )


def _pair_data_from(transaction: Transaction) -> TransactionData:
    """Baut eine Importzeile mit denselben Fingerprint-Feldern wie ``transaction``."""

    return TransactionData(
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
        origin=transaction.origin,
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


def _suspected_pair(client: TestClient, *, keep_origin: str = "fints") -> tuple[int, int]:
    """Legt ein Verdachtspaar an: ``keep_id`` (Vorschlag) und ``other_id``.

    Beide Buchungen liegen am selben Tag, haben denselben Betrag und
    abweichenden Text (F-DUP-03).
    """

    account_id = _add_account(client)
    keep_id = _add_transaction(
        client,
        account_id,
        origin=keep_origin,
        counterparty="REWE Markt",
        purpose="Wocheneinkauf",
    )
    other_id = _add_transaction(
        client,
        account_id,
        origin="csv",
        counterparty="Rewe Markt GmbH",
        purpose="Einkauf 10.01",
    )
    return keep_id, other_id


# ---------------------------------------------------------------- F-DUP-03 Liste


def test_offene_paare_mit_vorschlag_fints_vor_datei(client: TestClient) -> None:
    """F-DUP-03/05: Ein Verdachtspaar wird mit FinTS-Vorrang als bleibend vorgeschlagen."""

    keep_id, other_id = _suspected_pair(client, keep_origin="fints")
    account_id = _reload(client, keep_id).account_id

    response = client.get(f"/api/duplicates?account_id={account_id}")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["suggested_keep_id"] == keep_id
    assert item["reason"] == "fints"
    assert item["keep"]["id"] == keep_id
    assert item["other"]["id"] == other_id


def test_offene_paare_vorschlag_aeltere_buchung(client: TestClient) -> None:
    """F-DUP-05: Ohne FinTS-Beteiligung bleibt die aeltere Buchung."""

    account_id = _add_account(client)
    older_id = _add_transaction(
        client,
        account_id,
        origin="csv",
        booking_date=date(2026, 1, 9),
        counterparty="Baeckerei",
        purpose="Brot",
    )
    newer_id = _add_transaction(
        client,
        account_id,
        origin="csv",
        booking_date=date(2026, 1, 11),
        counterparty="Baeckerei Sued",
        purpose="Brot",
    )

    item = client.get(f"/api/duplicates?account_id={account_id}").json()["items"][0]

    assert item["suggested_keep_id"] == older_id
    assert item["reason"] == "alter"
    assert item["other"]["id"] == newer_id


def test_gleicher_text_ist_kein_verdacht(client: TestClient) -> None:
    """F-DUP-03: Ein identischer Text ist keine Verdachtsdublette."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Netto", purpose="Einkauf")
    _add_transaction(client, account_id, counterparty="Netto", purpose="Einkauf")

    items = client.get(f"/api/duplicates?account_id={account_id}").json()["items"]

    assert items == []


@pytest.mark.parametrize(
    "override",
    [
        {"amount_cents": 12345},
        {"booking_date": date(2026, 1, 20)},
    ],
)
def test_betrag_oder_tag_ausserhalb_ist_kein_verdacht(client: TestClient, override: dict[str, object]) -> None:
    """F-DUP-03: Abweichender Betrag oder mehr als zwei Tage Abstand ist kein Verdacht."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="A", purpose="erster")
    _add_transaction(client, account_id, counterparty="B", purpose="zweiter", **override)

    assert client.get(f"/api/duplicates?account_id={account_id}").json()["items"] == []


def test_verschiedene_konten_sind_kein_verdacht(client: TestClient) -> None:
    """F-DUP-03: Ein Verdachtspaar gehoert zu genau einem Konto."""

    first_account = _add_account(client, "Giro")
    second_account = _add_account(client, "Tagesgeld")
    _add_transaction(client, first_account, counterparty="A", purpose="eins")
    _add_transaction(client, second_account, counterparty="B", purpose="zwei")

    assert client.get(f"/api/duplicates?account_id={first_account}").json()["items"] == []


def test_vorgemerkte_buchung_nimmt_nicht_teil(client: TestClient) -> None:
    """F-DUP-04: Vorgemerkte Buchungen nehmen am Verdachtsabgleich nicht teil."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="A", purpose="eins", status="vorgemerkt")
    _add_transaction(client, account_id, counterparty="B", purpose="zwei", status="gebucht")

    assert client.get(f"/api/duplicates?account_id={account_id}").json()["items"] == []


def test_gruppenbereich_listet_paare_der_mitglieder(client: TestClient) -> None:
    """F-DUP-03: Der Gruppenbereich listet Paare ueber die aktiven Mitglieder."""

    group_id = _add_group(client)
    keep_id, other_id = _suspected_pair(client)
    account_id = _reload(client, keep_id).account_id
    # Das Konto des Paares in die Gruppe aufnehmen.
    with _session(client) as session:
        account = session.get(Account, account_id)
        assert account is not None
        account.group_id = group_id
        session.commit()

    items = client.get(f"/api/duplicates?group_id={group_id}").json()["items"]

    assert len(items) == 1
    assert {items[0]["keep"]["id"], items[0]["other"]["id"]} == {keep_id, other_id}


@pytest.mark.parametrize(
    "query",
    ["", "account_id=1&group_id=2"],
)
def test_liste_braucht_genau_einen_bereich(client: TestClient, query: str) -> None:
    """F-DUP-03: Die Liste verlangt genau ein Konto oder eine Gruppe (422)."""

    response = client.get(f"/api/duplicates?{query}")

    assert response.status_code == 422


def test_liste_unbekanntes_konto_ist_404(client: TestClient) -> None:
    """F-DUP-03: Ein unbekanntes Konto liefert 404."""

    assert client.get("/api/duplicates?account_id=9999").status_code == 404


def test_liste_ist_paginiert(client: TestClient) -> None:
    """F-DUP-03/SPEC F-MCP-11: Die Liste ist paginiert (limit, next_cursor)."""

    account_id = _add_account(client)
    for index in range(3):
        _add_transaction(client, account_id, counterparty=f"Glaeubiger {index}", purpose="Miete")
    _add_transaction(client, account_id, counterparty="Vermieter", purpose="Miete")

    first = client.get(f"/api/duplicates?account_id={account_id}&limit=1").json()
    assert len(first["items"]) == 1
    assert first["next_cursor"] is not None

    second = client.get(f"/api/duplicates?account_id={account_id}&limit=1&cursor={first['next_cursor']}").json()
    assert len(second["items"]) == 1
    first_pair = (first["items"][0]["keep"]["id"], first["items"][0]["other"]["id"])
    second_pair = (second["items"][0]["keep"]["id"], second["items"][0]["other"]["id"])
    assert second_pair != first_pair


def test_liste_aendert_nichts(client: TestClient) -> None:
    """F-DUP-05: Die Liste loest nie automatisch etwas aus."""

    keep_id, other_id = _suspected_pair(client)
    account_id = _reload(client, keep_id).account_id

    client.get(f"/api/duplicates?account_id={account_id}")

    assert _decision_rows(client) == []
    assert _reload(client, keep_id).merged is False
    assert _reload(client, other_id).merged is False


# ---------------------------------------------------------------- F-DUP-05 Merge


def test_merge_haelt_bankfelder_und_fuehrt_notizen_zusammen(client: TestClient) -> None:
    """F-DUP-05: Der Bleiber behaelt seine Bankfelder; Notizen werden mit Vermerk vereint."""

    keep_id, other_id = _suspected_pair(client)
    keep_before = _reload(client, keep_id)
    _update(client, keep_id, note="Bank-Notiz")
    _update(client, other_id, note="Datei-Notiz")

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["keep"]["id"] == keep_id
    assert body["other"]["merged"] is True

    keep = _reload(client, keep_id)
    assert keep.counterparty == keep_before.counterparty
    assert keep.purpose == keep_before.purpose
    assert keep.amount_cents == keep_before.amount_cents
    assert keep.origin == keep_before.origin
    assert keep.fingerprint == keep_before.fingerprint
    assert keep.occurrence_index == keep_before.occurrence_index
    assert keep.note == "Bank-Notiz\n[CSV] Datei-Notiz"
    assert _reload(client, other_id).merged is True


def test_merge_uebernimmt_kennzeichen_und_referenz(client: TestClient) -> None:
    """F-DUP-05: Kennzeichen und externe Referenz werden bei Bedarf uebernommen."""

    keep_id, other_id = _suspected_pair(client)
    _update(client, other_id, checked=True, checked_origin="import", external_ref="REF-1")

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    assert response.status_code == 200
    keep = _reload(client, keep_id)
    assert keep.checked is True
    assert keep.checked_origin == "import"
    assert keep.external_ref == "REF-1"
    assert response.json()["external_ref"] == "REF-1"


def test_merge_externe_referenz_verschieden_ohne_wahl_ist_422(client: TestClient) -> None:
    """F-DUP-05: Zwei verschiedene externe Referenzen verlangen eine Wahl (422)."""

    keep_id, other_id = _suspected_pair(client)
    _update(client, keep_id, external_ref="A")
    _update(client, other_id, external_ref="B")

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert _reload(client, other_id).merged is False


def test_merge_externe_referenz_verschieden_mit_wahl(client: TestClient) -> None:
    """F-DUP-05: Die gewaehlte externe Referenz wird uebernommen."""

    keep_id, other_id = _suspected_pair(client)
    _update(client, keep_id, external_ref="A")
    _update(client, other_id, external_ref="B")

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
            "external_ref_choice": "B",
        },
    )

    assert response.status_code == 200
    assert _reload(client, keep_id).external_ref == "B"


def test_merge_versionskonflikt_ist_409_mit_stand(client: TestClient) -> None:
    """F-CON-02: Eine veraltete Version liefert 409 mit dem aktuellen Stand."""

    keep_id, other_id = _suspected_pair(client)

    response = client.post(
        "/api/duplicates/merge",
        json={"keep_id": keep_id, "other_id": other_id, "versions": {str(keep_id): 5, str(other_id): 1}},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == keep_id
    assert body["current"]["version"] == 1
    assert _reload(client, other_id).merged is False


def test_merge_ohne_version_ist_422(client: TestClient) -> None:
    """F-CON-02: Ohne Version fuer beide Buchungen ist die Anfrage ungueltig (422)."""

    keep_id, other_id = _suspected_pair(client)

    response = client.post(
        "/api/duplicates/merge",
        json={"keep_id": keep_id, "other_id": other_id, "versions": {str(keep_id): 1}},
    )

    assert response.status_code == 422
    assert _reload(client, other_id).merged is False


def test_merge_unbekannte_buchung_ist_404(client: TestClient) -> None:
    """F-DUP-05: Eine unbekannte Buchung liefert 404."""

    keep_id, other_id = _suspected_pair(client)

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": 9999,
            "versions": {str(keep_id): _version(client, keep_id), "9999": 1},
        },
    )

    assert response.status_code == 404
    assert _reload(client, other_id).merged is False


def test_merge_protokoll_und_audit_mit_vorher_stand(client: TestClient) -> None:
    """F-DUP-05/F-MCP-09: Entscheidung und Audit mit Vorher-Stand entstehen."""

    keep_id, other_id = _suspected_pair(client)
    _update(client, keep_id, note="Vorher")

    decision_id = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    ).json()["decision_id"]

    decisions = _decision_rows(client)
    assert len(decisions) == 1
    assert decisions[0].decision == "zusammengefuehrt"
    assert decisions[0].transaction_id == keep_id
    assert decisions[0].other_transaction_id == other_id
    assert decisions[0].source == "ui"

    entries = _audit_rows(client, decision_id)
    assert len(entries) == 1
    assert entries[0].action == "merged"
    assert entries[0].source == "ui"
    assert entries[0].changes is not None
    assert entries[0].changes["keep"]["note"] == "Vorher"
    assert entries[0].changes["keep"]["id"] == keep_id


def test_merge_macht_andere_buchung_unsichtbar(client: TestClient) -> None:
    """F-DUP-04/05: Die zusammengefuehrte Buchung fehlt in Liste, Suche, Salden, Export und Paarliste."""

    keep_id, other_id = _suspected_pair(client)
    account_id = _reload(client, keep_id).account_id

    client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    # Paarliste: das aufgeloeste Paar erscheint nicht mehr.
    assert client.get(f"/api/duplicates?account_id={account_id}").json()["items"] == []

    # Suche.
    search_ids = {
        item["id"] for item in client.get(f"/api/search?scope=account&account_id={account_id}").json()["items"]
    }
    assert other_id not in search_ids
    assert keep_id in search_ids

    # Buchungsliste (Tagesgruppen).
    list_body = client.get(f"/api/accounts/{account_id}/transactions").json()
    listed = {item["id"] for day in list_body["days"] for item in day["transactions"]}
    assert other_id not in listed

    # Abstimmungsuebersicht: nur der Bleiber zaehlt als nicht abgestimmt.
    reconciliation = client.get(f"/api/accounts/{account_id}/reconciliation").json()
    assert reconciliation["unreconciled_count"] == 1
    assert reconciliation["unreconciled_sum_cents"] == 10000

    # Export (JSON).
    export_body = client.post(
        "/api/export?download=false",
        json={"format": "json", "scope": "account", "account_id": account_id, "period": "all"},
    )
    assert export_body.status_code == 200
    exported_ids = {row["id"] for row in json.loads(export_body.text)["transactions"]}
    assert other_id not in exported_ids

    # Fingerprint und Index bleiben belegt (F-DUP-05), nur die Markierung ist neu.
    other = _reload(client, other_id)
    assert other.merged is True
    assert other.fingerprint
    assert other.occurrence_index == 1


def test_merge_ereignis_duplicate_changed(client: TestClient, captured_events: list[Event]) -> None:
    """F-CON-04: Das Zusammenfuehren meldet ``duplicate.changed`` mit IDs."""

    keep_id, other_id = _suspected_pair(client)

    client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    events = [event for event in captured_events if event.type == "duplicate.changed"]
    assert len(events) == 1
    assert events[0].get("keep") == keep_id
    assert events[0].get("other") == other_id


def test_merge_bereits_zusammengefuehrt_ist_422(client: TestClient) -> None:
    """F-DUP-05: Eine bereits zusammengefuehrte Buchung laesst sich nicht erneut zusammenfuehren."""

    keep_id, other_id = _suspected_pair(client)
    versions = {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)}
    client.post("/api/duplicates/merge", json={"keep_id": keep_id, "other_id": other_id, "versions": versions})

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    assert response.status_code == 422


def test_merge_vorgemerkte_buchung_ist_422(client: TestClient) -> None:
    """F-DUP-04: Eine vorgemerkte Buchung laesst sich nicht zusammenfuehren (422)."""

    account_id = _add_account(client)
    keep_id = _add_transaction(client, account_id, origin="fints", counterparty="REWE Markt", purpose="Wocheneinkauf")
    other_id = _add_transaction(
        client,
        account_id,
        origin="csv",
        counterparty="Rewe Markt GmbH",
        purpose="Einkauf 10.01",
        status="vorgemerkt",
    )

    response = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert _decision_rows(client) == []
    assert _reload(client, keep_id).merged is False
    assert _reload(client, other_id).merged is False


def test_reimport_nach_merge_ist_null_neu(client: TestClient) -> None:
    """F-DUP-05: Ein Re-Import nach dem Zusammenfuehren legt nichts neu an."""

    keep_id, other_id = _suspected_pair(client)
    other_snapshot = _reload(client, other_id)
    client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    )

    data = _pair_data_from(other_snapshot)
    with _session(client) as session:
        result = reconcile_multiset(other_snapshot.account_id, [data], session)
        session.commit()

    assert result.inserted_count == 0
    assert result.skipped_count == 1


# ------------------------------------------------------------------- Rueckgaengig


def test_undo_stellt_beide_buchungen_wieder_her(client: TestClient) -> None:
    """F-DUP-05: Das Rueckgaengigmachen stellt Notiz, Kennzeichen und Referenz exakt wieder her."""

    keep_id, other_id = _suspected_pair(client)
    _update(client, keep_id, note="Bank-Notiz")
    _update(client, other_id, note="Datei-Notiz", checked=True, external_ref="REF-1")

    decision_id = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    ).json()["decision_id"]
    assert _reload(client, keep_id).note == "Bank-Notiz\n[CSV] Datei-Notiz"

    response = client.post(f"/api/duplicates/{decision_id}/undo")

    assert response.status_code == 200
    keep = _reload(client, keep_id)
    other = _reload(client, other_id)
    assert keep.note == "Bank-Notiz"
    assert keep.checked is False
    assert keep.external_ref is None
    assert other.merged is False
    assert other.note == "Datei-Notiz"
    assert other.checked is True
    assert other.external_ref == "REF-1"
    # Die Entscheidung ist aufgehoben, das Paar wieder offen.
    assert _decision_rows(client) == []
    account_id = keep.account_id
    assert len(client.get(f"/api/duplicates?account_id={account_id}").json()["items"]) == 1


def test_undo_gibt_409_wenn_bleiber_geaendert(client: TestClient) -> None:
    """F-DUP-05/F-CON-02: Ein seither geaenderter Bleiber ist beim Undo ein Konflikt (409)."""

    keep_id, other_id = _suspected_pair(client)
    decision_id = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    ).json()["decision_id"]

    client.put(
        f"/api/transactions/{keep_id}/note", json={"text": "nach dem Merge", "version": _version(client, keep_id)}
    )

    response = client.post(f"/api/duplicates/{decision_id}/undo")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == keep_id
    assert body["current"]["note"] == "nach dem Merge"
    # Nichts wurde zurueckgerollt.
    assert _reload(client, other_id).merged is True


def test_undo_unbekannt_ist_404(client: TestClient) -> None:
    """F-DUP-05: Ein Rueckgaengigmachen ohne Entscheidung liefert 404."""

    assert client.post("/api/duplicates/9999/undo").status_code == 404


# --------------------------------------------------------------------- Ignorieren


def test_ignore_speichert_keine_dublette_und_versteckt_das_paar(client: TestClient) -> None:
    """F-DUP-05: Ignorieren speichert 'verworfen'; das Paar fehlt danach in der Liste."""

    keep_id, other_id = _suspected_pair(client)
    account_id = _reload(client, keep_id).account_id

    response = client.post(
        "/api/duplicates/ignore",
        json={"keep_id": keep_id, "other_id": other_id},
    )

    assert response.status_code == 200
    decisions = _decision_rows(client)
    assert len(decisions) == 1
    assert decisions[0].decision == "verworfen"
    assert client.get(f"/api/duplicates?account_id={account_id}").json()["items"] == []
    # Der Bestand bleibt unveraendert (nie automatisch).
    assert _reload(client, keep_id).merged is False
    assert _reload(client, other_id).merged is False


def test_ignore_wirkt_beim_erneuten_import(client: TestClient) -> None:
    """F-DUP-05: Ein ignoriertes Paar wird auch bei erneutem Import nicht erneut vorgeschlagen."""

    keep_id, other_id = _suspected_pair(client)
    other_snapshot = _reload(client, other_id)
    client.post("/api/duplicates/ignore", json={"keep_id": keep_id, "other_id": other_id})

    data = _pair_data_from(other_snapshot)
    with _session(client) as session:
        pairs = find_suspected_duplicates(other_snapshot.account_id, [data], session)

    assert pairs == []


def test_ignore_kein_verdachtspaar_ist_422(client: TestClient) -> None:
    """F-DUP-05: Zwei nicht verdaechtige Buchungen lassen sich nicht ignorieren (422)."""

    account_id = _add_account(client)
    first_id = _add_transaction(client, account_id, counterparty="Netto", purpose="gleich")
    second_id = _add_transaction(client, account_id, counterparty="Netto", purpose="gleich")

    response = client.post("/api/duplicates/ignore", json={"keep_id": first_id, "other_id": second_id})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert _decision_rows(client) == []


def test_ignore_vorgemerkte_buchung_ist_422(client: TestClient) -> None:
    """F-DUP-04: Eine vorgemerkte Buchung laesst sich nicht ignorieren (422)."""

    account_id = _add_account(client)
    keep_id = _add_transaction(client, account_id, origin="fints", counterparty="REWE Markt", purpose="Wocheneinkauf")
    other_id = _add_transaction(
        client,
        account_id,
        origin="csv",
        counterparty="Rewe Markt GmbH",
        purpose="Einkauf 10.01",
        status="vorgemerkt",
    )

    response = client.post("/api/duplicates/ignore", json={"keep_id": keep_id, "other_id": other_id})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert _decision_rows(client) == []


def test_ignore_nach_merge_ist_422_und_erhaelt_merge(client: TestClient) -> None:
    """F-DUP-05: Ignorieren ueberschreibt eine Zusammenfuehrung nicht (422)."""

    keep_id, other_id = _suspected_pair(client)
    decision_id = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    ).json()["decision_id"]

    response = client.post("/api/duplicates/ignore", json={"keep_id": keep_id, "other_id": other_id})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    decisions = _decision_rows(client)
    assert len(decisions) == 1
    assert decisions[0].decision == "zusammengefuehrt"
    # Die Zusammenfuehrung bleibt rueckgaengig zu machen.
    assert client.post(f"/api/duplicates/{decision_id}/undo").status_code == 200
    assert _reload(client, other_id).merged is False


def test_undo_rundreise_ohne_datenverlust(client: TestClient) -> None:
    """F-DUP-05: Merge und Undo sind verlustfrei (Kennzeichen-Herkunft und Zeitpunkt bleiben)."""

    keep_id, other_id = _suspected_pair(client)
    client.put(f"/api/transactions/{keep_id}/checked", json={"value": True})
    before = _reload(client, keep_id)

    decision_id = client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": keep_id,
            "other_id": other_id,
            "versions": {str(keep_id): _version(client, keep_id), str(other_id): _version(client, other_id)},
        },
    ).json()["decision_id"]
    client.post(f"/api/duplicates/{decision_id}/undo")

    after = _reload(client, keep_id)
    assert after.checked is True
    assert after.checked_origin == before.checked_origin
    assert after.checked_at is not None
    assert before.checked_at is not None
    assert after.checked_at.replace(microsecond=0) == before.checked_at.replace(microsecond=0)
