"""Tests der Gruppen-API (T-088).

Referenziert F-KTO-03 (Gruppen anlegen, umbenennen, sortieren, loeschen;
Mitglieder, Kontenzahl, Summe) und F-CON-02 (Versionspruefung, 409) sowie
F-CON-04 (Ereignis group.changed). Die Summe folgt der Fremdwaehrungsregel
(Fremdwaehrung getrennt, nicht addiert). Testdaten sind erfunden (D8).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from finue.models.account import Account, AccountGroup


def _session(client: TestClient):  # type: ignore[no-untyped-def]
    return client.app.state.session_factory()


def _add_group(client: TestClient, name: str, *, sort_order: int = 0) -> int:
    with _session(client) as session:
        group = AccountGroup(name=name, sort_order=sort_order)
        session.add(group)
        session.commit()
        return group.id


def _add_account(client: TestClient, name: str, *, group_id: int | None = None, **overrides: object) -> int:
    fields: dict[str, object] = {"name": name, "currency": "EUR", "source": "datei", "group_id": group_id}
    fields.update(overrides)
    with _session(client) as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


# ------------------------------------------------------------------ F-KTO-03


def test_gruppe_liefert_mitglieder_kontenzahl_summe(client: TestClient) -> None:
    """F-KTO-03: Eine Gruppe zeigt Mitglieder-IDs, Kontenzahl und Summe."""

    group_id = _add_group(client, "Immo")
    first = _add_account(client, "Haus", group_id=group_id, balance_cents=100_000, icon=b"\x89PNG")
    second = _add_account(client, "Zweitkonto", group_id=group_id, balance_cents=50_000)

    body = client.get("/api/groups").json()
    assert len(body["items"]) == 1
    view = body["items"][0]
    assert view["id"] == group_id
    assert view["name"] == "Immo"
    assert view["member_ids"] == [first, second]
    assert view["account_count"] == 2
    assert view["currency"] == "EUR"
    assert view["amount_cents"] == 150_000
    icon_members = [member for member in view["members"] if member["has_icon"]]
    assert icon_members == [{"id": first, "has_icon": True, "icon_url": f"/api/accounts/{first}/icon"}]


def test_gruppensumme_trennt_fremdwaehrung(client: TestClient) -> None:
    """F-KTO-03/F-KTO-02: Die Gruppensumme addiert Fremdwaehrung nicht mit."""

    group_id = _add_group(client, "Gemisch")
    _add_account(client, "Euro", group_id=group_id, balance_cents=10_000)
    usd_id = _add_account(client, "Dollar", group_id=group_id, currency="USD", balance_cents=5_000)

    view = client.get("/api/groups").json()["items"][0]

    assert view["amount_cents"] == 10_000
    assert view["foreign"] == [{"currency": "USD", "amount_cents": 5_000, "account_ids": [usd_id]}]


def test_gruppe_anlegen(client: TestClient) -> None:
    """F-KTO-03: Eine Gruppe laesst sich anlegen."""

    response = client.post("/api/groups", json={"name": "Neu", "sort_order": 3})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Neu"
    assert body["sort_order"] == 3
    assert body["account_count"] == 0
    assert body["version"] == 1


def test_gruppe_umbenennen(client: TestClient) -> None:
    """F-KTO-03: Eine Gruppe laesst sich umbenennen."""

    group_id = _add_group(client, "Alt")

    response = client.patch(f"/api/groups/{group_id}", json={"version": 1, "name": "Neu"})

    assert response.status_code == 200
    assert response.json()["name"] == "Neu"
    assert response.json()["version"] == 2


def test_gruppe_umbenennen_konflikt(client: TestClient) -> None:
    """F-CON-02: Umbenennen mit veralteter Version liefert 409 mit Stand."""

    group_id = _add_group(client, "Alt")
    client.patch(f"/api/groups/{group_id}", json={"version": 1, "name": "Zwischenstand"})

    response = client.patch(f"/api/groups/{group_id}", json={"version": 1, "name": "Neu"})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == group_id
    assert body["current"]["name"] == "Zwischenstand"


def test_gruppen_neu_ordnen_als_id_liste(client: TestClient) -> None:
    """F-KTO-03: Neuordnung erfolgt als ID-Liste und setzt ``sort_order``."""

    first = _add_group(client, "Erste", sort_order=0)
    second = _add_group(client, "Zweite", sort_order=1)
    third = _add_group(client, "Dritte", sort_order=2)

    response = client.put("/api/groups/order", json={"ids": [third, first, second]})

    assert response.status_code == 200
    order = [(item["id"], item["sort_order"]) for item in response.json()]
    assert order == [(third, 0), (first, 1), (second, 2)]

    listed = [item["id"] for item in client.get("/api/groups").json()["items"]]
    assert listed == [third, first, second]


def test_gruppen_neu_ordnen_doppelte_ids(client: TestClient) -> None:
    """F-KTO-03: Eine Reihenfolge mit doppelten IDs wird abgelehnt."""

    group_id = _add_group(client, "Einzeln")

    response = client.put("/api/groups/order", json={"ids": [group_id, group_id]})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


def test_gruppe_loeschen_behaelt_konten(client: TestClient) -> None:
    """F-KTO-03: Loeschen setzt ``group_id`` der Konten auf NULL."""

    group_id = _add_group(client, "Weg")
    account_id = _add_account(client, "Konto", group_id=group_id)

    response = client.delete(f"/api/groups/{group_id}", params={"version": 1})

    assert response.status_code == 204
    accounts = client.get("/api/accounts").json()["items"]
    assert [item["group_id"] for item in accounts] == [None]
    assert accounts[0]["id"] == account_id
    assert client.get("/api/groups").json()["items"] == []


def test_gruppe_loeschen_konflikt(client: TestClient) -> None:
    """F-CON-02: Loeschen mit veralteter Version liefert 409."""

    group_id = _add_group(client, "Bleibt")

    response = client.delete(f"/api/groups/{group_id}", params={"version": 99})

    assert response.status_code == 409
    assert response.json()["current"]["id"] == group_id


def test_gruppenliste_paginierung(client: TestClient) -> None:
    """F-MCP-11: Gruppenliste begrenzt mit ``limit`` und liefert einen Cursor."""

    for index in range(3):
        _add_group(client, f"Gruppe {index}", sort_order=index)

    first = client.get("/api/groups", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None


# ------------------------------------------------------------------ F-CON-04


def test_ereignis_group_changed(client: TestClient) -> None:
    """F-CON-04: Aenderungen veroeffentlichen das Ereignis ``group.changed``."""

    events: list[object] = []
    client.app.state.services.event_bus.subscribe(events.append)

    group_id = _add_group(client, "Ereignis")
    client.patch(f"/api/groups/{group_id}", json={"version": 1, "name": "Anders"})

    types = [getattr(event, "type", None) for event in events]
    assert types.count("group.changed") == 1
    assert getattr(events[0], "get")("group") == group_id
