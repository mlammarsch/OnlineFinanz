"""Tests der Konten-API (T-088).

Referenziert F-KTO-01 (Kontenliste mit maske, Saldo, letzter Sync), F-KTO-02
(Gesamtsaldo, Fremdwaehrung getrennt), F-KTO-04 (Details pflegen, IBAN
validiert), F-KTO-05b (Offline-Konto), F-KTO-06 (Archivieren/Wiederherstellen),
F-CON-02 (Versionspruefung, 409) und F-CON-04 (Ereignis account.changed).
Die Testdaten sind erfunden (D8); es gibt keine echten Zugangsdaten.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from finue.models.account import Account, AccountGroup
from finue.models.bank import Bank, BankAccess
from finue.models.import_run import ImportRun
from finue.services.accounts import mask_iban, validate_iban
from finue.services.sync_service import _select_accounts

VALID_IBAN = "DE89370400440532013000"


def _session(client: TestClient):  # type: ignore[no-untyped-def]
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _add_account(client: TestClient, **overrides: object) -> int:
    """Legt ein Konto an und liefert seine ID (erfundene Testdaten, D8)."""

    fields: dict[str, object] = {"name": "Testkonto", "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    with _session(client) as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


def _add_group(client: TestClient, name: str = "Gruppe") -> int:
    with _session(client) as session:
        group = AccountGroup(name=name)
        session.add(group)
        session.commit()
        return group.id


def _add_sync_run(client: TestClient, account_id: int, *, status: str, kind: str, finished_at: datetime) -> None:
    with _session(client) as session:
        session.add(
            ImportRun(account_id=account_id, kind=kind, status=status, finished_at=finished_at),
        )
        session.commit()


# ------------------------------------------------------------------ F-KTO-01


def test_listen_liefert_kontofelder_und_maskierte_iban(client: TestClient) -> None:
    """F-KTO-01: Liste zeigt Name, Typ, maskierte IBAN, Waehrung, Saldo, Quelle, version."""

    account_id = _add_account(
        client,
        name="Giro",
        account_type="Girokonto",
        iban=VALID_IBAN,
        balance_cents=123456,
        balance_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    response = client.get("/api/accounts")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == account_id
    assert item["name"] == "Giro"
    assert item["account_type"] == "Girokonto"
    assert item["iban_masked"] == "DE89**************3000"
    assert VALID_IBAN not in response.text  # volle IBAN nicht in der Liste
    assert item["currency"] == "EUR"
    assert item["balance_cents"] == 123456
    assert item["source"] == "datei"
    assert item["include_in_total"] is True
    assert item["has_icon"] is False
    assert item["icon_url"] is None
    assert item["version"] == 1
    assert item["last_sync_at"] is None


def test_liste_zeigt_icon_verweis(client: TestClient) -> None:
    """F-KTO-01: Ein vorhandenes Icon liefert ``has_icon`` und einen Icon-Verweis."""

    account_id = _add_account(client, icon=b"\x89PNG", icon_mime="image/png")
    item = client.get("/api/accounts").json()["items"][0]
    assert item["has_icon"] is True
    assert item["icon_url"] == f"/api/accounts/{account_id}/icon"


def test_letzter_erfolgreicher_sync(client: TestClient) -> None:
    """F-KTO-01: letzter Sync aus ImportRun mit kind=sync und Erfolgsstatus."""

    account_id = _add_account(client)
    _add_sync_run(
        client,
        account_id,
        kind="sync",
        status="erfolgreich",
        finished_at=datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc),
    )
    _add_sync_run(
        client,
        account_id,
        kind="sync",
        status="fehler",
        finished_at=datetime(2026, 1, 6, 8, 0, tzinfo=timezone.utc),
    )
    _add_sync_run(
        client,
        account_id,
        kind="csv",
        status="erfolgreich",
        finished_at=datetime(2026, 1, 7, 8, 0, tzinfo=timezone.utc),
    )

    item = client.get("/api/accounts").json()["items"][0]

    assert item["last_sync_at"].startswith("2026-01-05T08:00:00")


def test_paginierung_mit_limit_und_cursor(client: TestClient) -> None:
    """F-MCP-11: Liste begrenzt mit ``limit`` und liefert einen Cursor."""

    for index in range(3):
        _add_account(client, name=f"Konto {index}")

    first = client.get("/api/accounts", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None

    second = client.get("/api/accounts", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None

    assert client.get("/api/accounts", params={"limit": 0}).status_code == 422


# ------------------------------------------------------------------ F-KTO-02


def test_gesamtsaldo_und_zusammensetzung_trennt_fremdwaehrung(client: TestClient) -> None:
    """F-KTO-02: Gesamtsaldo addiert nur die Buchwaehrung; Fremdwaehrung getrennt."""

    included_eur = _add_account(client, name="Giro", balance_cents=100_000)
    excluded_eur = _add_account(client, name="Darlehen", balance_cents=500_000, include_in_total=False)
    included_usd = _add_account(client, name="USD", currency="USD", balance_cents=25_000)

    total = client.get("/api/accounts").json()["total"]

    assert total["currency"] == "EUR"
    assert total["amount_cents"] == 100_000
    assert [entry["id"] for entry in total["included"]] == [included_eur]
    assert [entry["id"] for entry in total["excluded"]] == [excluded_eur]
    assert total["foreign"] == [{"currency": "USD", "amount_cents": 25_000, "account_ids": [included_usd]}]


# ------------------------------------------------------------------ F-KTO-04


def test_detail_liefert_volle_iban(client: TestClient) -> None:
    """F-KTO-04: Die Detailansicht liefert die volle IBAN zum Pflegen."""

    account_id = _add_account(client, iban=VALID_IBAN, opening_balance_cents=1000)

    response = client.get(f"/api/accounts/{account_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["iban"] == VALID_IBAN
    assert body["opening_balance_cents"] == 1000
    assert body["active"] is True
    assert body["bank_access_id"] is None


def test_detail_unbekanntes_konto(client: TestClient) -> None:
    """F-KTO-04: Ein unbekanntes Konto liefert 404 im Fehlerformat."""

    response = client.get("/api/accounts/9999")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_patch_aendert_kontodetails(client: TestClient) -> None:
    """F-KTO-04: PATCH setzt Name, Typ, Waehrung, Gruppe und include_in_total."""

    account_id = _add_account(client)
    group_id = _add_group(client, "Neue Gruppe")

    response = client.patch(
        f"/api/accounts/{account_id}",
        json={
            "version": 1,
            "name": "Umbenannt",
            "account_type": "Tagesgeld",
            "currency": "EUR",
            "group_id": group_id,
            "include_in_total": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Umbenannt"
    assert body["account_type"] == "Tagesgeld"
    assert body["group_id"] == group_id
    assert body["include_in_total"] is False
    assert body["version"] == 2


def test_patch_loescht_optionale_felder(client: TestClient) -> None:
    """F-KTO-04: Explizites ``null`` leert IBAN, Kontotyp und Gruppe."""

    group_id = _add_group(client)
    account_id = _add_account(client, iban=VALID_IBAN, account_type="Giro", group_id=group_id)

    response = client.patch(
        f"/api/accounts/{account_id}",
        json={"version": 1, "iban": None, "account_type": None, "group_id": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["iban"] is None
    assert body["account_type"] is None
    assert body["group_id"] is None


def test_patch_ungueltige_iban(client: TestClient) -> None:
    """F-KTO-04: Eine ungueltige IBAN wird mit deutschem Klartext abgelehnt."""

    account_id = _add_account(client)

    response = client.patch(
        f"/api/accounts/{account_id}",
        json={"version": 1, "iban": "DE89370400440532013001"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert "Pruefziffer" in response.json()["message"]


def test_patch_versionskonflikt_liefert_stand(client: TestClient) -> None:
    """F-CON-02: Ein veralteter Stand liefert 409 mit dem aktuellen Stand."""

    account_id = _add_account(client, name="Alt")
    client.patch(f"/api/accounts/{account_id}", json={"version": 1, "name": "Neu"})

    response = client.patch(f"/api/accounts/{account_id}", json={"version": 1, "name": "Noch neuer"})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == account_id
    assert body["current"]["version"] == 2
    assert body["current"]["name"] == "Neu"


# ----------------------------------------------------------------- F-KTO-05b


def test_offline_konto_mit_ankersaldo(client: TestClient) -> None:
    """F-KTO-05b: Offline-Konto (source datei) mit Ankersaldo und Datum."""

    response = client.post(
        "/api/accounts",
        json={
            "name": "PayPal",
            "currency": "EUR",
            "iban": VALID_IBAN,
            "opening_balance_cents": 12_345,
            "opening_balance_date": "2026-01-01",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "datei"
    assert body["opening_balance_cents"] == 12_345
    assert body["balance_cents"] == 12_345
    assert body["balance_at"].startswith("2026-01-01T00:00:00")


def test_offline_konto_verlangt_ankersaldo_und_datum_gemeinsam(client: TestClient) -> None:
    """F-KTO-05b: Nur der Betrag ohne Datum wird als Fachfehler abgelehnt."""

    response = client.post("/api/accounts", json={"name": "Nur Betrag", "opening_balance_cents": 100})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


def test_offline_konto_unbekannte_gruppe(client: TestClient) -> None:
    """F-KTO-05b: Eine unbekannte Gruppe wird als Fachfehler abgelehnt."""

    response = client.post("/api/accounts", json={"name": "Konto", "group_id": 999})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


# ------------------------------------------------------------------ F-KTO-06


def test_archivierte_konten_getrennt_abrufbar(client: TestClient) -> None:
    """F-KTO-06: Archivierte Konten fehlen in Liste und Gesamtsaldo, separat abrufbar."""

    _add_account(client, name="Aktiv", balance_cents=1000)
    archived_id = _add_account(client, name="Archiv", balance_cents=9000, active=False)

    default = client.get("/api/accounts").json()
    assert [item["name"] for item in default["items"]] == ["Aktiv"]
    assert default["total"]["amount_cents"] == 1000

    archived = client.get("/api/accounts", params={"archived": True}).json()
    assert [item["id"] for item in archived["items"]] == [archived_id]


def test_archivieren_und_wiederherstellen(client: TestClient) -> None:
    """F-KTO-06: Archivieren blendet aus, Wiederherstellen bringt zurueck."""

    account_id = _add_account(client, name="Konto")

    archived = client.post(f"/api/accounts/{account_id}/archive", json={"version": 1})
    assert archived.status_code == 200
    assert archived.json()["active"] is False
    assert client.get("/api/accounts").json()["items"] == []

    restored = client.post(f"/api/accounts/{account_id}/restore", json={"version": 2})
    assert restored.status_code == 200
    assert restored.json()["active"] is True
    assert len(client.get("/api/accounts").json()["items"]) == 1


def test_archivieren_versionskonflikt(client: TestClient) -> None:
    """F-CON-02: Archivieren mit veralteter Version liefert 409."""

    account_id = _add_account(client)

    response = client.post(f"/api/accounts/{account_id}/archive", json={"version": 99})

    assert response.status_code == 409
    assert response.json()["current"]["id"] == account_id


def test_archiviertes_konto_fehlt_im_batch_sync(client: TestClient) -> None:
    """F-KTO-06: Die Sync-Kontenauswahl laesst archivierte Konten aus."""

    with _session(client) as session:
        bank = Bank(blz="10000000", name="Testbank")
        session.add(bank)
        session.commit()
        access = BankAccess(bank_id=bank.id, login_name="erfunden")
        session.add(access)
        session.commit()
        session.add(
            Account(name="Aktiv", source="fints", bank_access_id=access.id, active=True),
        )
        session.add(
            Account(name="Archiv", source="fints", bank_access_id=access.id, active=False),
        )
        session.commit()
        access_id = access.id

    with _session(client) as session:
        selected = _select_accounts(session, access_id, None)

    assert [account.name for account in selected] == ["Aktiv"]


# ------------------------------------------------------------------ F-CON-04


def test_ereignis_account_changed(client: TestClient) -> None:
    """F-CON-04: Aenderungen veroeffentlichen das Ereignis ``account.changed``."""

    events: list[object] = []
    client.app.state.services.event_bus.subscribe(events.append)

    account_id = _add_account(client)
    client.patch(f"/api/accounts/{account_id}", json={"version": 1, "name": "Neu"})

    types = [getattr(event, "type", None) for event in events]
    assert types.count("account.changed") == 1
    assert getattr(events[0], "get")("account") == account_id


# ------------------------------------------------------- IBAN-Regel (F-KTO-01)


@pytest.mark.parametrize(
    ("iban", "expected"),
    [
        (None, None),
        (VALID_IBAN, "DE89**************3000"),
        ("DE12", "****"),
        ("GB82WEST12345698765432", "GB82**************5432"),
    ],
)
def test_mask_iban_regel(iban: str | None, expected: str | None) -> None:
    """F-KTO-01: Die Maskenregel zeigt die ersten und letzten vier Zeichen."""

    assert mask_iban(iban) == expected


@pytest.mark.parametrize(
    "iban",
    [
        "DE89370400440532013001",  # falsche Pruefziffer
        "DE89370400440532",  # zu kurz
        "DE89-3704-0044",  # Sonderzeichen
        "1X89370400440532013000",  # falscher Aufbau
    ],
)
def test_validate_iban_lehnt_ungueltige_ab(iban: str) -> None:
    """F-KTO-04: Ungueltige IBANs werden abgelehnt."""

    with pytest.raises(ValueError):
        validate_iban(iban)


def test_validate_iban_akzeptiert_gueltige() -> None:
    """F-KTO-04: Eine gueltige IBAN wird normalisiert zurueckgegeben."""

    assert validate_iban(" de89 3704 0044 0532 0130 00 ") == VALID_IBAN
