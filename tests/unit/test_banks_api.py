"""Tests der Banken-Stammdaten- und Icon-API (T-090).

Referenziert F-KTO-10 (BLZ, Name, FinTS-Endpunkt nur https, Website, Icon;
keine Vorbelegung ohne belegte Quelle), F-KTO-11 (Bank-Icon ueber denselben
Upload-Weg) und F-CON-02 (Versionspruefung, 409 mit Stand). Die Bankzugaenge
sind erfunden (D8); es gibt keine echten Zugangsdaten.
"""

from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from finue.models.bank import BankAccess


def _png(size: tuple[int, int] = (80, 40)) -> bytes:
    """Erzeugt ein erfundenes Testbild (D8)."""

    buffer = BytesIO()
    Image.new("RGBA", size, (10, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _session(client: TestClient):  # type: ignore[no-untyped-def]
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _create_bank(client: TestClient, **overrides: object) -> dict[str, object]:
    """Legt eine Bank ueber die API an (F-KTO-10)."""

    payload: dict[str, object] = {"blz": "10000000", "name": "Testbank"}
    payload.update(overrides)
    response = client.post("/api/banks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _add_access(client: TestClient, bank_id: int) -> None:
    """Haengt einen erfundenen Bankzugang an (Loeschschutz, F-KTO-10)."""

    with _session(client) as session:
        session.add(BankAccess(bank_id=bank_id, login_name="erfunden"))
        session.commit()


# ------------------------------------------------------------------ F-KTO-10


def test_liste_startet_leer_ohne_vorbelegung(client: TestClient) -> None:
    """F-KTO-10: es werden keine Banken vorbelegt (nur Endpunkte mit belegter Quelle)."""

    response = client.get("/api/banks")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_anlegen_und_auflisten(client: TestClient) -> None:
    """F-KTO-10: POST legt eine Bank an, GET listet sie mit Version."""

    created = _create_bank(client, name="ING", website="https://www.ing.de", fints_url="https://fints.ing.de")

    assert created["blz"] == "10000000"
    assert created["name"] == "ING"
    assert created["website"] == "https://www.ing.de"
    assert created["fints_url"] == "https://fints.ing.de"
    assert created["has_icon"] is False
    assert created["icon_url"] is None
    assert created["version"] == 1

    listed = client.get("/api/banks").json()["items"]
    assert [item["name"] for item in listed] == ["ING"]


def test_anlegen_ungueltige_blz_abgelehnt(client: TestClient) -> None:
    """F-KTO-10: die BLZ muss aus genau acht Ziffern bestehen."""

    response = client.post("/api/banks", json={"blz": "1234567a", "name": "Kaputt"})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert "acht Ziffern" in response.json()["message"]


def test_fints_endpunkt_nur_https(client: TestClient) -> None:
    """F-KTO-10/N-SEC-05: ein FinTS-Endpunkt ohne HTTPS wird abgelehnt."""

    response = client.post(
        "/api/banks",
        json={"blz": "10000000", "name": "Bank", "fints_url": "http://fints.example"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert "https" in response.json()["message"]


def test_website_ungueltiges_schema_abgelehnt(client: TestClient) -> None:
    """F-KTO-10: die Website muss ein http(s)-Ziel mit Host sein."""

    response = client.post(
        "/api/banks",
        json={"blz": "10000000", "name": "Bank", "website": "javascript:alert(1)"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


def test_detail_unbekannte_bank(client: TestClient) -> None:
    """F-KTO-10: eine unbekannte Bank liefert 404."""

    response = client.get("/api/banks/9999")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_patch_mit_versionspruefung(client: TestClient) -> None:
    """F-KTO-10/F-CON-02: PATCH aendert die Stammdaten und prueft die Version."""

    bank = _create_bank(client)

    updated = client.patch(f"/api/banks/{bank['id']}", json={"version": 1, "name": "Neuer Name"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Neuer Name"
    assert updated.json()["version"] == 2

    conflict = client.patch(f"/api/banks/{bank['id']}", json={"version": 1, "name": "Zu spaet"})
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == "conflict"
    assert body["current"]["version"] == 2
    assert body["current"]["name"] == "Neuer Name"


def test_patch_ungueltiger_endpunkt(client: TestClient) -> None:
    """F-KTO-10: ein nachtraeglich gesetzter http-Endpunkt wird abgelehnt."""

    bank = _create_bank(client)

    response = client.patch(
        f"/api/banks/{bank['id']}",
        json={"version": 1, "fints_url": "ftp://fints.example"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


def test_loeschen_ohne_zugaenge(client: TestClient) -> None:
    """F-KTO-10: eine Bank ohne Bankzugaenge laesst sich loeschen."""

    bank = _create_bank(client)

    response = client.delete(f"/api/banks/{bank['id']}", params={"version": 1})

    assert response.status_code == 204
    assert client.get(f"/api/banks/{bank['id']}").status_code == 404


def test_loeschen_mit_bankzugang_verboten(client: TestClient) -> None:
    """F-KTO-10: eine Bank mit Bankzugaengen wird mit 409 im Klartext abgelehnt."""

    bank = _create_bank(client)
    _add_access(client, int(bank["id"]))

    response = client.delete(f"/api/banks/{bank['id']}", params={"version": 1})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert "Bankzugaenge" in body["message"]
    assert body["current"]["id"] == bank["id"]


def test_loeschen_versionskonflikt(client: TestClient) -> None:
    """F-CON-02: Loeschen mit veralteter Version liefert 409 mit Stand."""

    bank = _create_bank(client)

    response = client.delete(f"/api/banks/{bank['id']}", params={"version": 99})

    assert response.status_code == 409
    assert response.json()["current"]["id"] == bank["id"]


def test_paginierung_bankliste(client: TestClient) -> None:
    """F-MCP-11: die Bankenliste begrenzt mit ``limit`` und liefert einen Cursor."""

    _create_bank(client, blz="10000001", name="Bank A")
    _create_bank(client, blz="10000002", name="Bank B")
    _create_bank(client, blz="10000003", name="Bank C")

    first = client.get("/api/banks", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None

    assert client.get("/api/banks", params={"limit": 0}).status_code == 422


def test_ereignis_bank_changed(client: TestClient) -> None:
    """F-CON-04: Aenderungen melden das Ereignis ``bank.changed``."""

    events: list[object] = []
    client.app.state.services.event_bus.subscribe(events.append)

    bank = _create_bank(client)
    client.patch(f"/api/banks/{bank['id']}", json={"version": 1, "name": "Neu"})

    types = [getattr(event, "type", None) for event in events]
    assert types.count("bank.changed") == 2
    assert getattr(events[0], "get")("bank") == bank["id"]


# --------------------------------------------------- Bank-Icon ueber T-090


def test_bank_icon_upload_auslieferung_und_loeschen(client: TestClient) -> None:
    """F-KTO-11: Bank-Icon hochladen, als PNG ausliefern mit ETag, wieder entfernen."""

    bank = _create_bank(client)
    bank_id = bank["id"]

    uploaded = client.put(
        f"/api/banks/{bank_id}/icon",
        params={"version": 1},
        content=_png((100, 50)),
        headers={"Content-Type": "image/png"},
    )

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["has_icon"] is True
    assert body["version"] == 2
    assert (body["width"], body["height"]) == (100, 50)
    assert body["icon_url"] == f"/api/banks/{bank_id}/icon"

    fetched = client.get(f"/api/banks/{bank_id}/icon")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["etag"]

    assert client.delete(f"/api/banks/{bank_id}/icon", params={"version": 2}).status_code == 204
    assert client.get(f"/api/banks/{bank_id}/icon").status_code == 404


def test_bank_icon_versionskonflikt(client: TestClient) -> None:
    """F-CON-02: ein veralteter Stand beim Bank-Icon liefert 409 mit dem Stand."""

    bank = _create_bank(client)
    client.put(f"/api/banks/{bank['id']}/icon", params={"version": 1}, content=_png())

    response = client.put(f"/api/banks/{bank['id']}/icon", params={"version": 1}, content=_png())

    assert response.status_code == 409
    assert response.json()["current"]["id"] == bank["id"]
    assert response.json()["current"]["version"] == 2
