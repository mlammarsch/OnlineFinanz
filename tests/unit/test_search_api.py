"""Tests der Such-API ``GET /api/search`` (T-092).

Referenziert F-SUC-01..07, F-ABS-04/05 und F-CON-04 sowie die API-Konventionen
aus ``docs/api.md`` (Paginierung ``{items, next_cursor}``, deutsche 422-Fehler).
Die Suche liefert zusaetzlich zur Seite die Trefferzahl und die vorzeichen-
behaftete Trefferumme (DoD T-092).

Alle Buchungen und Konten sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

import base64
from datetime import date

from fastapi.testclient import TestClient

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.search_text import build_search_text

IBAN = "DE02 1203 0000 0000 2020 51"

_sequence = 0


def _next_fingerprint() -> str:
    global _sequence
    _sequence += 1
    return f"{_sequence:064x}"


def _session(client: TestClient):  # type: ignore[no-untyped-def]
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
    with _session(client) as session:
        group = AccountGroup(name=name)
        session.add(group)
        session.commit()
        return group.id


def _add_transaction(
    client: TestClient,
    account_id: int,
    *,
    counterparty: str | None = None,
    purpose: str | None = None,
    note: str | None = None,
    counterparty_iban: str | None = None,
    external_ref: str | None = None,
    **overrides: object,
) -> int:
    """Legt eine Testbuchung mit gefuellter Suchspalte an (F-SUC-01)."""

    fields: dict[str, object] = {
        "account_id": account_id,
        "booking_date": date(2026, 5, 1),
        "amount_cents": -1000,
        "currency": "EUR",
        "origin": "csv",
        "status": "gebucht",
        "counterparty": counterparty,
        "purpose": purpose,
        "note": note,
        "counterparty_iban": counterparty_iban,
        "external_ref": external_ref,
        "fingerprint": _next_fingerprint(),
        "occurrence_index": 1,
    }
    fields.update(overrides)
    fields["search_text"] = build_search_text(
        counterparty=counterparty,
        purpose=purpose,
        note=note,
        counterparty_iban=counterparty_iban,
        external_ref=external_ref,
    )
    with _session(client) as session:
        transaction = Transaction(**fields)
        session.add(transaction)
        session.commit()
        return transaction.id


# ------------------------------------------------------------------ Grundfall


def test_suche_liefert_treffer_mit_zahl_und_summe(client: TestClient) -> None:
    """F-SUC-01, DoD: Treffer, Trefferzahl und vorzeichenbehaftete Trefferumme."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Max Mustermann", purpose="Miete", amount_cents=-4883)
    _add_transaction(client, account_id, counterparty="Max Mustermann", purpose="Strom", amount_cents=1000)
    _add_transaction(client, account_id, counterparty="Andere", purpose="Miete", amount_cents=-500)

    response = client.get("/api/search", params={"q": "mustermann"})

    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 2
    assert body["total_cents"] == -3883
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    assert {item["purpose"] for item in body["items"]} == {"Miete", "Strom"}


def test_suche_ohne_treffer(client: TestClient) -> None:
    """F-SUC-01: Ohne Treffer sind Zahl und Summe null."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Max Mustermann")

    body = client.get("/api/search", params={"q": "gibtesnicht"}).json()
    assert body["items"] == []
    assert body["total_count"] == 0
    assert body["total_cents"] == 0


def test_paginierung_mit_limit_und_cursor(client: TestClient) -> None:
    """SPEC F-MCP-11: ``limit`` und Cursor schneiden die Seite heraus."""

    account_id = _add_account(client)
    for day in range(3):
        _add_transaction(client, account_id, counterparty="Treffer", booking_date=date(2026, 5, 1 + day))

    first = client.get("/api/search", params={"q": "treffer", "limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    assert first["total_count"] == 3

    second = client.get("/api/search", params={"q": "treffer", "limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None

    assert client.get("/api/search", params={"limit": 0}).status_code == 422


def test_riesiger_cursor_ist_422(client: TestClient) -> None:
    """F-CON-02: Ein Cursor mit zu grossem Offset liefert 422 statt 500.

    Der Offset bleibt portabel innerhalb der 32-Bit-Grenze; ein base64-kodierter
    Cursor aus ``o`` und 40 Ziffern wuerde sonst die Datenbank ueberlaufen lassen
    (Regression zum Verifierbefund T-092).
    """

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Treffer")

    cursor = base64.urlsafe_b64encode(("o" + "9" * 40).encode("ascii")).decode("ascii").rstrip("=")
    response = client.get("/api/search", params={"q": "treffer", "cursor": cursor})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["details"]["field"] == "cursor"


# ------------------------------------------------------------------ F-SUC-02


def test_betragssuche_im_api(client: TestClient) -> None:
    """F-SUC-02: ``48,83`` trifft den Absolutwert, ``-48,83`` exakt negativ."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Positiv", amount_cents=4883)
    _add_transaction(client, account_id, counterparty="Negativ", amount_cents=-4883)

    absolute = client.get("/api/search", params={"q": "48,83", "period": "all"}).json()
    assert absolute["total_count"] == 2

    signed = client.get("/api/search", params={"q": "-48,83", "period": "all"}).json()
    assert signed["total_count"] == 1
    assert signed["items"][0]["counterparty"] == "Negativ"


# ------------------------------------------------------------------ F-SUC-03


def test_bereich_konto(client: TestClient) -> None:
    """F-SUC-03: ``scope=account`` beschraenkt auf ein Konto."""

    first = _add_account(client, "Erstes")
    second = _add_account(client, "Zweites")
    _add_transaction(client, first, counterparty="Treffer")
    _add_transaction(client, second, counterparty="Treffer")

    body = client.get("/api/search", params={"q": "treffer", "scope": "account", "account_id": first}).json()
    assert [item["account_id"] for item in body["items"]] == [first]


def test_bereich_gruppe(client: TestClient) -> None:
    """F-SUC-03: ``scope=group`` beschraenkt auf die Gruppenmitglieder."""

    group_id = _add_group(client, "Haushalt")
    member = _add_account(client, "Mitglied", group_id=group_id)
    other = _add_account(client, "Anderes")
    _add_transaction(client, member, counterparty="Treffer")
    _add_transaction(client, other, counterparty="Treffer")

    body = client.get("/api/search", params={"q": "treffer", "scope": "group", "group_id": group_id}).json()
    assert [item["account_id"] for item in body["items"]] == [member]


def test_bereich_alle_laesst_archivierte_aus(client: TestClient) -> None:
    """F-SUC-03: ``scope=all`` zaehlt nur aktive Konten."""

    active = _add_account(client, "Aktiv")
    archived = _add_account(client, "Archiv", active=False)
    _add_transaction(client, active, counterparty="Treffer")
    _add_transaction(client, archived, counterparty="Treffer")

    body = client.get("/api/search", params={"q": "treffer"}).json()
    assert [item["account_id"] for item in body["items"]] == [active]


def test_bereich_konto_ohne_id_ist_422(client: TestClient) -> None:
    """F-SUC-03: ``scope=account`` ohne ``account_id`` liefert deutschen 422-Fehler."""

    response = client.get("/api/search", params={"scope": "account"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "account_id" in response.json()["message"]


# ------------------------------------------------------------------ F-SUC-04


def test_standard_zeitraum_ist_365_tage(client: TestClient) -> None:
    """F-SUC-04: Standard sind 365 Tage; altes Jahr faellt heraus."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Neu", booking_date=date.today())
    _add_transaction(client, account_id, counterparty="Alt", booking_date=date(2000, 1, 1))

    body = client.get("/api/search").json()
    assert [item["counterparty"] for item in body["items"]] == ["Neu"]


def test_zeitraum_alle_und_custom(client: TestClient) -> None:
    """F-SUC-04: ``all`` ignoriert den Zeitraum, ``custom`` nutzt von/bis."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Alt", booking_date=date(2000, 1, 1))

    assert client.get("/api/search", params={"period": "all"}).json()["total_count"] == 1
    im_bereich = client.get("/api/search", params={"period": "custom", "from": "1999-01-01", "to": "2000-12-31"}).json()
    assert im_bereich["total_count"] == 1


def test_custom_ohne_grenzen_ist_422(client: TestClient) -> None:
    """F-SUC-04: ``custom`` ohne Grenzen wird abgelehnt."""

    response = client.get("/api/search", params={"period": "custom"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_ungueltiger_zeitraumwert_ist_422(client: TestClient) -> None:
    """F-SUC-04: Ein unbekannter Zeitraumwert wird abgelehnt."""

    response = client.get("/api/search", params={"period": "42"})
    assert response.status_code == 422


# ------------------------------------------------------------------ F-SUC-05


def test_sortierung_nach_betrag(client: TestClient) -> None:
    """F-SUC-05: ``sort=amount`` mit Richtung."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Gross", amount_cents=300)
    _add_transaction(client, account_id, counterparty="Klein", amount_cents=-500)

    auf = client.get("/api/search", params={"period": "all", "sort": "amount", "direction": "asc"}).json()
    ab = client.get("/api/search", params={"period": "all", "sort": "amount", "direction": "desc"}).json()

    assert [item["amount_cents"] for item in auf["items"]] == [-500, 300]
    assert [item["amount_cents"] for item in ab["items"]] == [300, -500]


# ------------------------------------------------------------------ F-SUC-07 / F-ABS-04


def test_filter_geprueft_abgestimmt_notiz(client: TestClient) -> None:
    """F-SUC-07/F-ABS-04: Filter geprueft, abgestimmt und mit Notiz."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Treffer", checked=True, note="Servus")
    _add_transaction(client, account_id, counterparty="Treffer", reconciled=True)

    checked = client.get("/api/search", params={"q": "treffer", "checked": "true"}).json()
    assert checked["total_count"] == 1
    assert checked["items"][0]["checked"] is True

    reconciled = client.get("/api/search", params={"q": "treffer", "reconciled": "true"}).json()
    assert reconciled["total_count"] == 1
    assert reconciled["items"][0]["reconciled"] is True

    with_note = client.get("/api/search", params={"q": "treffer", "with_note": "true"}).json()
    assert with_note["total_count"] == 1
    assert with_note["items"][0]["note"] == "Servus"


def test_zusammengefuehrte_buchung_fehlt(client: TestClient) -> None:
    """F-DUP-04: ``merged``-Buchungen sind nicht Teil der Suche."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Treffer")
    _add_transaction(client, account_id, counterparty="Treffer", merged=True)

    body = client.get("/api/search", params={"q": "treffer"}).json()
    assert body["total_count"] == 1


# ------------------------------------------------------------------ F-ABS-05


def test_externe_referenz_durchsuchbar(client: TestClient) -> None:
    """F-ABS-05: Die externe Referenz ist ueber die Suche auffindbar."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Vermieter", external_ref="TX-42")

    body = client.get("/api/search", params={"q": "tx-42"}).json()
    assert body["total_count"] == 1
    assert body["items"][0]["external_ref"] == "TX-42"


def test_mehrere_textbegriffe_und_verknuepft(client: TestClient) -> None:
    """F-SUC-01: Mehrere Begriffe sind UND-verknuepft, Gross-/Kleinschreibung egal."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Max MUSTERMANN", purpose="Gehalt Juni")
    _add_transaction(client, account_id, counterparty="Max MUSTERMANN", purpose="Miete")

    body = client.get("/api/search", params={"q": "mustermann gehalt"}).json()
    assert body["total_count"] == 1
    assert body["items"][0]["purpose"] == "Gehalt Juni"


def test_notiz_und_iban_sind_durchsuchbar(client: TestClient) -> None:
    """F-SUC-01: Notiz und IBAN sind Teil der Suche."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, counterparty="Vermieter", counterparty_iban=IBAN, note="Kaution")

    assert client.get("/api/search", params={"q": "kaution"}).json()["total_count"] == 1
    assert client.get("/api/search", params={"q": "DE02120300000000202051"}).json()["total_count"] == 1
