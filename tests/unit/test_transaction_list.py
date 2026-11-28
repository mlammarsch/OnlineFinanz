"""Tests der Buchungsliste (T-093).

Referenziert F-BUC-01 (Kopf mit Name, Icon-Verweis, Saldo), F-BUC-02
(Vormerkungen mit Liste, Summe und Saldo inklusive Vormerkungen), F-BUC-03
(gebuchte Umsaetze nach Buchungstag absteigend, Tageskopf mit Tagessumme und
Tagessaldo rueckwaerts vom aktuellen Saldo, Datei-Konten ab Ankersaldo),
F-BUC-04 (Zeilenfelder), F-BUC-05 (Gruppenansicht mit Kontokennzeichnung je
Zeile) und F-BUC-06 (Keyset-Paginierung, Tagessaldo der Folgeseite per
SQL-Aggregat). Dazu F-DUP-04 (zusammengefuehrte Buchungen ausgeschlossen),
F-CON-02 (422 statt 500 bei ungueltigem Cursor/limit) und die API-Konventionen
aus ``docs/api.md``.

Alle Konten und Buchungen sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

import base64
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services import reconciliation
from finue.services import transaction_list as tl
from finue.services.search import SearchScope, TransactionQuery
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


def _add_group(client: TestClient, name: str = "Gruppe") -> int:
    """Legt eine Gruppe an und liefert ihre ID (erfundene Testdaten, D8)."""

    with _session(client) as session:
        group = AccountGroup(name=name)
        session.add(group)
        session.commit()
        return group.id


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


def _get(client: TestClient, path: str, **params: object):  # type: ignore[no-untyped-def]
    """Ruft eine Listenroute mit ``period=all`` auf (Zeitraum neutral)."""

    query = {"period": "all"}
    query.update(params)
    return client.get(path, params=query)


# ------------------------------------------------------------------ F-BUC-01


def test_kopf_mit_name_saldo_und_icon(client: TestClient) -> None:
    """F-BUC-01: Kopf liefert Name, Saldo und Icon-Verweis."""

    account_id = _add_account(client, "Girokonto", icon=b"\x89PNG", opening_balance_cents=100000)
    _add_transaction(client, account_id, amount_cents=2500)

    body = _get(client, f"/api/accounts/{account_id}/transactions").json()

    assert body["head"] == {
        "scope": "account",
        "id": account_id,
        "name": "Girokonto",
        "currency": "EUR",
        "balance_cents": 102500,
        "icon_url": f"/api/accounts/{account_id}/icon",
        "account_count": 1,
    }


def test_zeilenfelder_vollstaendig(client: TestClient) -> None:
    """F-BUC-04: Zeile traegt id, Status, Quelle, Gegenpartei, Zweck, Betrag, Marker und Kennzeichen."""

    account_id = _add_account(client)
    _add_transaction(
        client,
        account_id,
        counterparty="Max Mustermann",
        purpose="Miete Januar",
        amount_cents=-4883,
        note="Nachfragen",
        origin="fints",
        checked=True,
        reconciled=True,
    )

    row = _get(client, f"/api/accounts/{account_id}/transactions").json()["days"][0]["transactions"][0]

    assert row["status"] == "gebucht"
    assert row["origin"] == "fints"
    assert row["counterparty"] == "Max Mustermann"
    assert row["purpose"] == "Miete Januar"
    assert row["amount_cents"] == -4883
    assert row["has_note"] is True
    assert row["checked"] is True
    assert row["reconciled"] is True
    assert row["version"] == 1
    assert row["account_name"] == "Konto"


# ------------------------------------------------------------------ F-BUC-02


def test_vormerkungen_mit_summe_und_saldo(client: TestClient) -> None:
    """F-BUC-02: Vormerkungen mit Liste, Summe und Saldo inklusive Vormerkungen."""

    account_id = _add_account(client, opening_balance_cents=100000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10), amount_cents=5000)
    _add_transaction(
        client,
        account_id,
        booking_date=date(2026, 1, 13),
        amount_cents=-700,
        status="vorgemerkt",
        counterparty="Vorgemerkt",
    )

    body = _get(client, f"/api/accounts/{account_id}/transactions").json()

    assert body["head"]["balance_cents"] == 105000
    pending = body["pending"]
    assert pending["sum_cents"] == -700
    assert pending["total_count"] == 1
    assert pending["balance_with_pending_cents"] == 104300
    assert [row["counterparty"] for row in pending["items"]] == ["Vorgemerkt"]
    # Die Vormerkung ist nicht Teil der Tagesgruppen (nur gebuchte Umsaetze).
    assert [row["status"] for group in body["days"] for row in group["transactions"]] == ["gebucht"]


def test_vormerkungen_nur_auf_der_ersten_seite(client: TestClient) -> None:
    """F-BUC-02: Der Vormerkungsbereich erscheint nur ohne Cursor."""

    account_id = _add_account(client)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10))
    _add_transaction(client, account_id, booking_date=date(2026, 1, 11))
    _add_transaction(client, account_id, booking_date=date(2026, 1, 12), status="vorgemerkt")

    first = _get(client, f"/api/accounts/{account_id}/transactions", limit=1).json()
    assert first["pending"] is not None
    assert first["next_cursor"] is not None

    second = _get(client, f"/api/accounts/{account_id}/transactions", limit=1, cursor=first["next_cursor"]).json()
    assert second["pending"] is None


# ------------------------------------------------------------------ F-BUC-03


def test_tagessaldo_datei_konto_mit_anker(client: TestClient) -> None:
    """F-BUC-03: Datei-Konto bildet den Tagessaldo ab dem Ankersaldo."""

    account_id = _add_account(client, opening_balance_cents=100000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10), amount_cents=10000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 11), amount_cents=-2500)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 12), amount_cents=2000)

    body = _get(client, f"/api/accounts/{account_id}/transactions").json()

    assert body["head"]["balance_cents"] == 109500
    by_day = {group["day"]: group for group in body["days"]}
    assert by_day["2026-01-12"]["sum_cents"] == 2000
    assert by_day["2026-01-12"]["balance_end_cents"] == 109500
    assert by_day["2026-01-11"]["balance_end_cents"] == 107500
    assert by_day["2026-01-10"]["balance_end_cents"] == 110000  # Anker + 10000


def test_saldenkette_ueber_drei_seiten(client: TestClient) -> None:
    """F-BUC-06: Keyset-Paginierung liefert die Saldenkette ueber drei Seiten."""

    account_id = _add_account(client, opening_balance_cents=100000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10), amount_cents=10000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 11), amount_cents=-2500)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 12), amount_cents=2000)

    bodies = []
    cursor: str | None = None
    for _ in range(3):
        params: dict[str, object] = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        body = _get(client, f"/api/accounts/{account_id}/transactions", **params).json()
        bodies.append(body)
        cursor = body["next_cursor"]

    balances = [body["days"][0]["balance_end_cents"] for body in bodies]
    assert balances == [109500, 107500, 110000]
    assert bodies[2]["next_cursor"] is None
    # Die Seiten duplizieren oder ueberspringen keine Buchung.
    ids = [group["transactions"][0]["id"] for body in bodies for group in body["days"]]
    assert len(ids) == len(set(ids)) == 3


def test_filter_veraendert_tagessaldo_nicht(client: TestClient) -> None:
    """F-BUC-03: Ein Filter aendert den Tagessaldo nicht (juengere Buchungen zaehlen mit)."""

    account_id = _add_account(client, opening_balance_cents=100000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10), amount_cents=10000, counterparty="Ziel")
    _add_transaction(client, account_id, booking_date=date(2026, 1, 11), amount_cents=-2500, counterparty="Andere")
    _add_transaction(client, account_id, booking_date=date(2026, 1, 12), amount_cents=2000, counterparty="Andere")

    gefiltert = _get(client, f"/api/accounts/{account_id}/transactions", q="ziel").json()

    assert [group["day"] for group in gefiltert["days"]] == ["2026-01-10"]
    assert gefiltert["days"][0]["balance_end_cents"] == 110000


def test_zusammengefuehrte_buchung_fehlt_und_zaehlt_nicht(client: TestClient) -> None:
    """F-DUP-04: Zusammengefuehrte Buchungen fehlen und zaehlen nicht in den Saldo."""

    account_id = _add_account(client, opening_balance_cents=0)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10), amount_cents=1000)
    _add_transaction(client, account_id, booking_date=date(2026, 1, 10), amount_cents=999999, merged=True)

    body = _get(client, f"/api/accounts/{account_id}/transactions").json()

    assert body["head"]["balance_cents"] == 1000
    rows = body["days"][0]["transactions"]
    assert [row["amount_cents"] for row in rows] == [1000]


# ------------------------------------------------------------------ F-BUC-05


def test_gruppenansicht_mit_kontokennzeichnung(client: TestClient) -> None:
    """F-BUC-05: Buchungen aller Gruppenmitglieder mit Kontokennzeichnung je Zeile."""

    group_id = _add_group(client, "Haushalt")
    first = _add_account(client, "Erstes", group_id=group_id, opening_balance_cents=0)
    second = _add_account(client, "Zweites", group_id=group_id, opening_balance_cents=0)
    _add_transaction(client, first, booking_date=date(2026, 1, 10), amount_cents=1000)
    _add_transaction(client, second, booking_date=date(2026, 1, 10), amount_cents=2000)

    body = _get(client, f"/api/groups/{group_id}/transactions").json()

    assert body["head"]["scope"] == "group"
    assert body["head"]["name"] == "Haushalt"
    assert body["head"]["account_count"] == 2
    assert body["head"]["balance_cents"] == 3000
    assert body["days"][0]["balance_end_cents"] == 3000
    marking = {(row["account_id"], row["account_name"]) for row in body["days"][0]["transactions"]}
    assert marking == {(first, "Erstes"), (second, "Zweites")}


def test_gruppe_zaehlt_fremdwaehrung_nicht_mit(client: TestClient) -> None:
    """F-BUC-05: Der Gruppensaldo summiert nur die Buchwaehrung (Regel T-088)."""

    group_id = _add_group(client, "Gemischt")
    eur = _add_account(client, "Euro", group_id=group_id, opening_balance_cents=0, currency="EUR")
    usd = _add_account(client, "Dollar", group_id=group_id, opening_balance_cents=0, currency="USD")
    _add_transaction(client, eur, booking_date=date(2026, 1, 10), amount_cents=1000, currency="EUR")
    _add_transaction(client, usd, booking_date=date(2026, 1, 10), amount_cents=50000, currency="USD")

    body = _get(client, f"/api/groups/{group_id}/transactions").json()

    assert body["head"]["balance_cents"] == 1000
    assert body["days"][0]["balance_end_cents"] == 1000
    # Beide Zeilen erscheinen; nur die Buchwaehrung zaehlt in den Tagessaldo.
    assert {row["account_id"] for row in body["days"][0]["transactions"]} == {eur, usd}


# ------------------------------------------------------------------ Fehler/Edge


def test_unbekanntes_konto_ist_404(client: TestClient) -> None:
    """F-CON-02: Ein unbekanntes Konto liefert 404 mit deutschem Klartext."""

    response = _get(client, "/api/accounts/9999/transactions")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_unbekannte_gruppe_ist_404(client: TestClient) -> None:
    """F-CON-02: Eine unbekannte Gruppe liefert 404."""

    response = _get(client, "/api/groups/9999/transactions")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("limit", ["0", "201", "abc"])
def test_ungueltiges_limit_ist_422(client: TestClient, limit: str) -> None:
    """F-CON-02/SPEC F-MCP-11: Ein ungueltiges ``limit`` liefert 422."""

    account_id = _add_account(client)
    response = client.get(f"/api/accounts/{account_id}/transactions", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "raw",
    [
        "x2026-01-01:5",  # falsches Praefix
        "kohne-trenner",  # kein Trenner
        "knotadate:5",  # ungueltiges Datum
        "k2026-01-01:abc",  # keine ID
        "k2026-01-01:0",  # ID < 1
        "k2026-01-01:" + "9" * 25,  # ID zu lang
    ],
)
def test_ungueltiger_cursor_ist_422(client: TestClient, raw: str) -> None:
    """F-CON-02: Ein ungueltiger Keyset-Cursor liefert 422 statt 500."""

    account_id = _add_account(client)
    cursor = base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")
    response = client.get(f"/api/accounts/{account_id}/transactions", params={"cursor": cursor})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["details"]["field"] == "cursor"


def test_nicht_base64_cursor_ist_422(client: TestClient) -> None:
    """F-CON-02: Ein nicht-base64-Cursor liefert 422 statt 500."""

    account_id = _add_account(client)
    response = client.get(f"/api/accounts/{account_id}/transactions", params={"cursor": "!!!nicht-base64"})

    assert response.status_code == 422
    assert response.json()["details"]["field"] == "cursor"


# --------------------------------------------------------------- Service-Ebene


def test_cursor_kodierung_und_dekodierung_roundtrip() -> None:
    """F-BUC-06: Kodierung und Dekodierung des Keyset-Cursors sind invers."""

    from finue.api.transaction_list import decode_keyset_cursor, encode_keyset_cursor

    cursor = tl.ListCursor(booking_date=date(2026, 1, 10), transaction_id=42)
    assert decode_keyset_cursor(encode_keyset_cursor(cursor)) == cursor


def test_anlaufregel_paritaet_mit_abstimmung() -> None:
    """F-BUC-03: Der aktuelle Saldo folgt derselben Ankerregel wie die Abstimmung."""

    datei = Account(name="Datei", currency="EUR", source="datei", opening_balance_cents=4000)
    fints = Account(name="FinTS", currency="EUR", source="fints", opening_balance_cents=0)
    booked_sum = 6000

    for account in (datei, fints):
        opening, _derived = reconciliation._effective_opening(account, booked_sum)  # noqa: SLF001
        assert tl._account_current_balance(account, booked_sum) == opening + booked_sum  # noqa: SLF001

    # FinTS-Konto ohne Banksaldo leitet nichts ab; es zaehlt der gebuchte Betrag.
    assert tl._account_current_balance(fints, booked_sum) == booked_sum  # noqa: SLF001


def test_service_lehnt_bereich_ohne_konto_und_gruppe_ab(client: TestClient) -> None:
    """F-BUC-01/05: Der Service kennt nur die Bereiche Konto und Gruppe."""

    with _session(client) as session:
        with pytest.raises(Exception) as excinfo:
            tl.build_transaction_list(session, query=TransactionQuery(scope=SearchScope.ALL), limit=10)
        assert "Bereiche" in str(excinfo.value)


def test_service_lehnt_unbekanntes_konto_ab(client: TestClient) -> None:
    """F-BUC-01: Ein unbekanntes Konto wird als ``AccountNotFound`` gemeldet."""

    with _session(client) as session:
        with pytest.raises(tl.AccountNotFound):
            tl.build_transaction_list(
                session,
                query=TransactionQuery(scope=SearchScope.ACCOUNT, account_id=123456),
                limit=10,
            )
