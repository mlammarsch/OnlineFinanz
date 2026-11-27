"""Tests fuer die lesenden Auftrags-REST-Routen (T-100).

Referenziert F-AUF-02 (Auftraege anzeigen), F-AUF-04 (Auftragsbestand je Konto
mit Zeitstempel), F-AUF-05 (Hinweis bei fehlenden Auftragsdaten, kein Bereich
fuer Datei-Konten) und F-AUF-03 (keine schreibende Route). Die Auftraege werden
direkt ueber den Service gesetzt; es gibt keinen Bankzugriff (D8) und keine
echten Kontodaten.

Abgedeckt:

- Datei-Konto liefert ``none`` ohne Bereich,
- FinTS-Konto ohne Auftraege liefert ``unavailable`` mit festem Klartext,
- FinTS-Konto mit Auftraegen liefert ``available`` mit Zeitstempel und Zeilen,
- die Gesamtliste fuehrt nur FinTS-Konten und paginiert,
- unbekanntes Konto liefert 404,
- unter ``standing-orders`` existiert keine schreibende Route (F-AUF-03).
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from finue.models.account import Account
from finue.services.fints_types import FetchedStandingOrder
from finue.services.standing_orders import ORDERS_UNAVAILABLE_MESSAGE, replace_standing_orders
from finue.timeutil import utc_now


def _create_file_account(client: TestClient, name: str = "Datei") -> int:
    """Legt ein Datei-Konto ueber die API an (F-KTO-05b)."""

    response = client.post("/api/accounts", json={"name": name, "currency": "EUR"})
    assert response.status_code == 201, response.text
    account_id: int = response.json()["id"]
    return account_id


def _create_fints_account(client: TestClient, name: str = "Bank") -> int:
    """Legt ein FinTS-Konto direkt in der Datenbank an (ohne Bankzugriff, D8)."""

    factory = client.app.state.session_factory
    with factory() as session:
        account = Account(name=name, currency="EUR", source="fints")
        session.add(account)
        session.commit()
        account_id: int = account.id
    return account_id


def _add_order(client: TestClient, account_id: int) -> None:
    """Setzt einen Dauerauftrag ueber den Service (F-AUF-04)."""

    factory = client.app.state.session_factory
    with factory() as session:
        replace_standing_orders(
            session,
            account_id,
            [
                FetchedStandingOrder(
                    kind="dauerauftrag",
                    counterparty="Vermieter",
                    counterparty_iban="DE89370400440532013000",
                    purpose="Miete",
                    amount_cents=120000,
                    currency="EUR",
                    interval="monthly",
                    next_execution=date(2026, 12, 1),
                )
            ],
            utc_now(),
        )
        session.commit()


# ------------------------------------------------------------------ je Konto


def test_datei_konto_hat_keinen_auftragsbereich(client: TestClient) -> None:
    """F-AUF-05: ein Datei-Konto liefert ``none`` ohne Auftraege und ohne Hinweis."""

    account_id = _create_file_account(client)

    response = client.get(f"/api/accounts/{account_id}/standing-orders")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "none"
    assert body["orders"] == []
    assert body["message"] is None
    assert body["fetched_at"] is None


def test_fints_konto_ohne_auftraege_liefert_hinweis(client: TestClient) -> None:
    """F-AUF-05: ohne Auftragsdaten der feste Klartext, keine Zeilen."""

    account_id = _create_fints_account(client)

    response = client.get(f"/api/accounts/{account_id}/standing-orders")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["message"] == ORDERS_UNAVAILABLE_MESSAGE
    assert body["orders"] == []


def test_fints_konto_mit_auftraegen_liefert_stand_mit_zeitstempel(client: TestClient) -> None:
    """F-AUF-02/04: Auftraege mit Zeitstempel des letzten Abrufs."""

    account_id = _create_fints_account(client)
    _add_order(client, account_id)

    response = client.get(f"/api/accounts/{account_id}/standing-orders")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["fetched_at"] is not None
    assert body["message"] is None
    assert len(body["orders"]) == 1
    order = body["orders"][0]
    assert order["kind"] == "dauerauftrag"
    assert order["counterparty"] == "Vermieter"
    assert order["amount_cents"] == 120000
    assert order["next_execution"] == "2026-12-01"


def test_unbekanntes_konto_ist_404(client: TestClient) -> None:
    """F-AUF-02: ein unbekanntes Konto liefert 404."""

    response = client.get("/api/accounts/999999/standing-orders")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ------------------------------------------------------------------ Gesamtliste


def test_gesamtliste_fuehrt_nur_fints_konten_und_paginiert(client: TestClient) -> None:
    """F-AUF-04/05: nur FinTS-Konten erscheinen; die Liste paginiert (F-MCP-11)."""

    _create_file_account(client, "Datei")
    first = _create_fints_account(client, "Bank A")
    second = _create_fints_account(client, "Bank B")
    _add_order(client, first)
    _add_order(client, second)

    response = client.get("/api/standing-orders", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is not None

    following = client.get("/api/standing-orders", params={"limit": 1, "cursor": body["next_cursor"]})
    assert following.status_code == 200
    assert len(following.json()["items"]) == 1
    assert following.json()["next_cursor"] is None

    seen = {body["items"][0]["account_id"], following.json()["items"][0]["account_id"]}
    assert seen == {first, second}


def test_gesamtliste_ohne_fints_konten_ist_leer(client: TestClient) -> None:
    """F-AUF-05: nur Datei-Konten ergeben eine leere Gesamtliste, keinen Fehler."""

    _create_file_account(client)

    response = client.get("/api/standing-orders")

    assert response.status_code == 200
    assert response.json()["items"] == []


# ------------------------------------------------------------------ keine Schreibrouten


def test_keine_schreibende_route_unter_standing_orders(client: TestClient) -> None:
    """F-AUF-03: unter ``standing-orders`` gibt es ausschliesslich GET (alle anderen Methoden 405)."""

    account_id = _create_fints_account(client)
    paths = ["/api/standing-orders", f"/api/accounts/{account_id}/standing-orders"]

    for path in paths:
        for method in ("post", "put", "patch", "delete"):
            response = client.request(method.upper(), path, json={})
            assert response.status_code == 405, f"{method.upper()} {path} war erlaubt"
