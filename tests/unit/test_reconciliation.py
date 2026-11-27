"""Tests der Abstimmungsuebersicht (T-095).

Referenziert F-ABS-01 (vorgemerkte Buchungen lassen sich nicht abstimmen),
F-ABS-03 (abgestimmter Saldo, Anzahl/Summe nicht abgestimmter Buchungen,
Differenz zum Banksaldo, je Konto und Gruppe) und N-DB-02 (Betraege in ganzen
Cent). Die Handrechnungen schliessen eine zusammengefuehrte Buchung (F-DUP-04)
und eine Vormerkung ein. Testdaten sind erfunden (D8); es gibt keine echten
Zugangsdaten oder Bankzugriffe.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient

from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.reconciliation import account_reconciliation, group_reconciliation


def _session(client: TestClient):  # type: ignore[no-untyped-def]
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _add_group(client: TestClient, name: str = "Gruppe") -> int:
    with _session(client) as session:
        group = AccountGroup(name=name)
        session.add(group)
        session.commit()
        return group.id


def _add_account(
    client: TestClient,
    *,
    name: str = "Konto",
    source: str = "datei",
    currency: str = "EUR",
    opening_balance_cents: int = 0,
    balance_cents: int | None = None,
    group_id: int | None = None,
    active: bool = True,
) -> int:
    """Legt ein Konto an und liefert seine ID (erfundene Testdaten, D8)."""

    with _session(client) as session:
        account = Account(
            name=name,
            source=source,
            currency=currency,
            opening_balance_cents=opening_balance_cents,
            balance_cents=balance_cents,
            group_id=group_id,
            active=active,
        )
        session.add(account)
        session.commit()
        return account.id


def _add_transaction(
    client: TestClient,
    account_id: int,
    amount_cents: int,
    *,
    status: str = "gebucht",
    reconciled: bool = False,
    merged: bool = False,
    currency: str = "EUR",
) -> None:
    """Legt eine Buchung an (erfundene Testdaten, D8)."""

    with _session(client) as session:
        session.add(
            Transaction(
                account_id=account_id,
                status=status,
                booking_date=date(2026, 1, 1),
                origin="fints",
                fingerprint=uuid4().hex,
                amount_cents=amount_cents,
                currency=currency,
                reconciled=reconciled,
                merged=merged,
            )
        )
        session.commit()


# ------------------------------------------------------------- F-ABS-03 Konto


def test_konto_handrechnung_mit_zusammenfuehrung_und_vormerkung(client: TestClient) -> None:
    """F-ABS-03: abgestimmter Saldo, nicht abgestimmte Summe und Differenz per Handrechnung.

    FinTS-Konto ohne Ankersaldo: Anfangssaldo = Banksaldo minus Summe der
    gebuchten, nicht zusammengefuehrten Buchungen. Die zusammengefuehrte und die
    vorgemerkte Buchung zaehlen nicht mit.
    """

    account_id = _add_account(client, source="fints", balance_cents=10_000)
    _add_transaction(client, account_id, 5_000, reconciled=True)
    _add_transaction(client, account_id, -1_000, reconciled=True)
    _add_transaction(client, account_id, 2_000, reconciled=False)
    _add_transaction(client, account_id, 1_000, reconciled=False, merged=True)
    _add_transaction(client, account_id, 3_000, status="vorgemerkt", reconciled=True)

    response = client.get(f"/api/accounts/{account_id}/reconciliation")

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == account_id
    assert body["opening_balance_derived"] is True
    # Anfangssaldo = 10000 - (5000 - 1000 + 2000) = 4000
    assert body["opening_balance_cents"] == 4_000
    # abgestimmt = 4000 + (5000 - 1000) = 8000
    assert body["reconciled_balance_cents"] == 8_000
    assert body["unreconciled_count"] == 1
    assert body["unreconciled_sum_cents"] == 2_000
    assert body["balance_cents"] == 10_000
    # Differenz = Banksaldo - abgestimmter Saldo = Summe der nicht abgestimmten
    assert body["difference_cents"] == 2_000


def test_datei_konto_nutzt_ankersaldo_nicht_abgeleitet(client: TestClient) -> None:
    """F-ABS-03: Ein Datei-Konto nutzt den gespeicherten Ankersaldo."""

    account_id = _add_account(client, source="datei", opening_balance_cents=1_000, balance_cents=1_700)
    _add_transaction(client, account_id, 500, reconciled=True)
    _add_transaction(client, account_id, 200, reconciled=False)

    body = client.get(f"/api/accounts/{account_id}/reconciliation").json()

    assert body["opening_balance_derived"] is False
    assert body["opening_balance_cents"] == 1_000
    assert body["reconciled_balance_cents"] == 1_500
    assert body["unreconciled_count"] == 1
    assert body["unreconciled_sum_cents"] == 200
    assert body["difference_cents"] == 200


def test_vormerkung_zaehlt_nicht(client: TestClient) -> None:
    """F-ABS-01/F-ABS-03: Vorgemerkte Buchungen zaehlen in keiner Kennzahl."""

    account_id = _add_account(client, source="datei", opening_balance_cents=0, balance_cents=100)
    _add_transaction(client, account_id, 100, reconciled=False)
    _add_transaction(client, account_id, 999, status="vorgemerkt", reconciled=True)
    _add_transaction(client, account_id, 999, status="vorgemerkt", reconciled=False)

    body = client.get(f"/api/accounts/{account_id}/reconciliation").json()

    assert body["unreconciled_count"] == 1
    assert body["unreconciled_sum_cents"] == 100
    assert body["difference_cents"] == 100


def test_zusammengefuehrte_buchung_zaehlt_nicht(client: TestClient) -> None:
    """F-ABS-03/F-DUP-04: Eine zusammengefuehrte Buchung zaehlt nicht mit."""

    account_id = _add_account(client, source="datei", opening_balance_cents=0, balance_cents=100)
    _add_transaction(client, account_id, 100, reconciled=False)
    _add_transaction(client, account_id, 500, reconciled=False, merged=True)
    _add_transaction(client, account_id, 500, reconciled=True, merged=True)

    body = client.get(f"/api/accounts/{account_id}/reconciliation").json()

    assert body["reconciled_balance_cents"] == 0
    assert body["unreconciled_count"] == 1
    assert body["unreconciled_sum_cents"] == 100
    assert body["difference_cents"] == 100


def test_differenz_ist_none_ohne_banksaldo(client: TestClient) -> None:
    """F-ABS-03: Ohne Banksaldo bleibt die Differenz leer."""

    account_id = _add_account(client, source="datei", opening_balance_cents=0, balance_cents=None)
    _add_transaction(client, account_id, 250, reconciled=False)

    body = client.get(f"/api/accounts/{account_id}/reconciliation").json()

    assert body["balance_cents"] is None
    assert body["difference_cents"] is None
    assert body["reconciled_balance_cents"] == 0
    assert body["unreconciled_sum_cents"] == 250


def test_leeres_konto(client: TestClient) -> None:
    """F-ABS-03: Ein Konto ohne Buchungen hat den abgestimmten Saldo gleich dem Anfangssaldo."""

    account_id = _add_account(client, source="datei", opening_balance_cents=4_200, balance_cents=4_200)

    body = client.get(f"/api/accounts/{account_id}/reconciliation").json()

    assert body["reconciled_balance_cents"] == 4_200
    assert body["unreconciled_count"] == 0
    assert body["unreconciled_sum_cents"] == 0
    assert body["difference_cents"] == 0


def test_unbekanntes_konto_liefert_404(client: TestClient) -> None:
    """F-ABS-03: Ein unbekanntes Konto liefert 404 im Fehlerformat."""

    response = client.get("/api/accounts/9999/reconciliation")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ------------------------------------------------------------- F-ABS-03 Gruppe


def test_gruppe_summiert_mitglieder(client: TestClient) -> None:
    """F-ABS-03: Die Gruppe summiert die Kennzahlen ihrer aktiven Mitglieder."""

    group_id = _add_group(client, "Haushalt")
    first = _add_account(
        client, name="Giro", source="datei", opening_balance_cents=1_000, balance_cents=2_700, group_id=group_id
    )
    second = _add_account(
        client, name="Tagesgeld", source="datei", opening_balance_cents=0, balance_cents=400, group_id=group_id
    )
    _add_transaction(client, first, 2_000, reconciled=True)
    _add_transaction(client, first, -300, reconciled=False)
    _add_transaction(client, second, 400, reconciled=False)

    body = client.get(f"/api/groups/{group_id}/reconciliation").json()

    assert body["group_id"] == group_id
    assert body["member_count"] == 2
    # abgestimmt = (1000 + 2000) + (0) = 3000
    assert body["reconciled_balance_cents"] == 3_000
    assert body["unreconciled_count"] == 2
    assert body["unreconciled_sum_cents"] == 100
    assert body["balance_cents"] == 3_100
    assert body["difference_cents"] == 100
    assert body["foreign"] == []


def test_gruppe_trennt_fremdwaehrung(client: TestClient) -> None:
    """F-ABS-03: Die Gruppe addiert Fremdwaehrung nicht mit (Regel in decisions.md)."""

    group_id = _add_group(client, "Gemisch")
    eur_id = _add_account(
        client, name="Euro", source="datei", opening_balance_cents=1_000, balance_cents=1_100, group_id=group_id
    )
    usd_id = _add_account(
        client,
        name="Dollar",
        source="datei",
        currency="USD",
        opening_balance_cents=2_000,
        balance_cents=2_500,
        group_id=group_id,
    )
    _add_transaction(client, eur_id, 100, reconciled=True)
    _add_transaction(client, usd_id, 500, reconciled=False, currency="USD")

    body = client.get(f"/api/groups/{group_id}/reconciliation").json()

    assert body["currency"] == "EUR"
    assert body["reconciled_balance_cents"] == 1_100
    assert body["unreconciled_count"] == 0
    assert len(body["foreign"]) == 1
    usd = body["foreign"][0]
    assert usd["currency"] == "USD"
    assert usd["reconciled_balance_cents"] == 2_000
    assert usd["unreconciled_count"] == 1
    assert usd["unreconciled_sum_cents"] == 500
    assert usd["difference_cents"] == 500


def test_gruppe_ignoriert_archivierte_mitglieder(client: TestClient) -> None:
    """F-ABS-03/F-KTO-06: Archivierte Mitglieder zaehlen nicht mit."""

    group_id = _add_group(client, "Archiv")
    _add_account(client, name="Aktiv", source="datei", group_id=group_id, active=True)
    _add_account(client, name="Archiv", source="datei", group_id=group_id, active=False)

    body = client.get(f"/api/groups/{group_id}/reconciliation").json()

    assert body["member_count"] == 1


def test_unbekannte_gruppe_liefert_404(client: TestClient) -> None:
    """F-ABS-03: Eine unbekannte Gruppe liefert 404 im Fehlerformat."""

    response = client.get("/api/groups/9999/reconciliation")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --------------------------------------------------- Service (MCP-Wiederverwendung)


def test_service_funktionen_sind_direkt_nutzbar(client: TestClient) -> None:
    """F-ABS-03: Die Service-Funktionen liefern dieselben Werte wie die REST-Route (D10, M9)."""

    account_id = _add_account(client, source="datei", opening_balance_cents=500, balance_cents=800)
    _add_transaction(client, account_id, 300, reconciled=True)
    group_id = _add_group(client, "Direkt")
    with _session(client) as session:
        account = session.get(Account, account_id)
        assert account is not None
        account.group_id = group_id
        session.commit()

    with _session(client) as session:
        account_view = account_reconciliation(session, account_id)
        group_view = group_reconciliation(session, group_id)

    assert account_view.totals.reconciled_balance_cents == 800
    assert account_view.totals.difference_cents == 0
    assert group_view.member_count == 1
    assert group_view.totals.reconciled_balance_cents == 800
