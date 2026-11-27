"""Tests fuer die CSV-Exportprofile (T-098).

Referenziert F-EXP-03 (speicherbare Profile, Standardprofil "Finanzblick-
angelehnt") und F-CON-02 (Versionspruefung, 409 mit aktuellem Stand). Abgedeckt:

- Idempotentes Seeding genau eines Standardprofils.
- CRUD ueber ``/api/export-profiles`` inklusive Anlage vergebener Namen,
  ungueltiger Optionen, Versionskonflikte beim Aendern und Loeschen.
- Hoechstens ein Standardprofil.
- Ein Profil steuert den CSV-Export (Trennzeichen, Spalten, Dezimal- und
  Datumsformat).

Alle Testdaten sind erfunden (D8); keine echten Zugangsdaten oder externen
Schreibaktionen.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.export_profile import ExportProfile
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.export import DEFAULT_CSV_COLUMNS
from finue.services.export_profiles import (
    DEFAULT_PROFILE_NAME,
    csv_options_from_profile,
    get_default_profile,
    list_profiles,
    seed_default_profile,
)


def _session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def _add_account(client: TestClient, name: str = "Konto") -> int:
    with _session(client) as session:
        account = Account(name=name, currency="EUR", source="datei")
        session.add(account)
        session.commit()
        return account.id


def _add_transaction(
    client: TestClient,
    account_id: int,
    *,
    booking_date: date = date(2026, 1, 15),
    amount_cents: int = -4883,
    counterparty: str = "Markt",
) -> int:
    fingerprint = compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=None,
        amount_cents=amount_cents,
        currency="EUR",
        counterparty=counterparty,
        counterparty_iban=None,
        purpose=None,
    )
    with _session(client) as session:
        transaction = Transaction(
            account_id=account_id,
            status="gebucht",
            booking_date=booking_date,
            amount_cents=amount_cents,
            currency="EUR",
            counterparty=counterparty,
            origin="csv",
            fingerprint=fingerprint,
            occurrence_index=0,
            search_text="",
        )
        session.add(transaction)
        session.commit()
        return transaction.id


# ----------------------------------------------------------------- Standardprofil


def test_seed_default_profile_idempotent(client: TestClient) -> None:
    """F-EXP-03: Genau ein Standardprofil, zweiter Aufruf legt nichts an."""

    with _session(client) as session:
        first = seed_default_profile(session)
        assert first.name == DEFAULT_PROFILE_NAME
        assert first.is_default is True
        assert first.options["delimiter"] == ";"
        assert tuple(first.columns) == DEFAULT_CSV_COLUMNS

    with _session(client) as session:
        second = seed_default_profile(session)
        assert second.id == first.id
        profiles = list_profiles(session)
        assert len(profiles) == 1
        assert get_default_profile(session) is not None


def test_csv_options_from_profile(client: TestClient) -> None:
    """F-EXP-03: Ein Profil wird in validierte CSV-Optionen uebersetzt."""

    with _session(client) as session:
        profile = ExportProfile(
            name="Komma-Export",
            is_default=False,
            options={"delimiter": ",", "decimal_separator": "point", "date_format": "JJJJ-MM-TT", "encoding": "utf-8"},
            columns=["amount", "booking_date"],
        )
        session.add(profile)
        session.commit()
        options = csv_options_from_profile(profile)

    assert options.delimiter == ","
    assert options.decimal_separator == "point"
    assert options.date_format == "JJJJ-MM-TT"
    assert options.columns == ("amount", "booking_date")


# ----------------------------------------------------------------------- CRUD


def test_crud_ueber_api(client: TestClient) -> None:
    """F-EXP-03/F-CON-02: Anlegen, Lesen, Aendern und Loeschen mit Version."""

    created = client.post(
        "/api/export-profiles",
        json={"name": "Sparkasse", "options": {"delimiter": ","}, "columns": ["booking_date", "amount"]},
    )
    assert created.status_code == 201
    profile = created.json()
    profile_id = profile["id"]
    assert profile["version"] == 1
    assert profile["is_default"] is False

    listed = client.get("/api/export-profiles")
    assert listed.status_code == 200
    assert any(item["id"] == profile_id for item in listed.json()["items"])

    fetched = client.get(f"/api/export-profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Sparkasse"

    updated = client.patch(
        f"/api/export-profiles/{profile_id}",
        json={"version": 1, "name": "Sparkasse neu", "columns": ["amount"]},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Sparkasse neu"
    assert updated.json()["columns"] == ["amount"]
    assert updated.json()["version"] == 2

    deleted = client.delete(f"/api/export-profiles/{profile_id}?version=2")
    assert deleted.status_code == 204

    missing = client.get(f"/api/export-profiles/{profile_id}")
    assert missing.status_code == 404


def test_patch_mit_veralteter_version_409(client: TestClient) -> None:
    """F-CON-02: Aenderung mit veralteter Version liefert 409 und den aktuellen Stand."""

    created = client.post("/api/export-profiles", json={"name": "Profil"}).json()
    profile_id = created["id"]

    client.patch(f"/api/export-profiles/{profile_id}", json={"version": 1, "name": "Erste"})
    conflict = client.patch(f"/api/export-profiles/{profile_id}", json={"version": 1, "name": "Zweite"})

    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == profile_id
    assert body["current"]["version"] == 2
    assert body["current"]["name"] == "Erste"


def test_delete_mit_veralteter_version_409(client: TestClient) -> None:
    """F-CON-02: Loeschen mit veralteter Version liefert 409."""

    created = client.post("/api/export-profiles", json={"name": "Profil"}).json()
    profile_id = created["id"]
    client.patch(f"/api/export-profiles/{profile_id}", json={"version": 1, "name": "Geaendert"})

    conflict = client.delete(f"/api/export-profiles/{profile_id}?version=1")
    assert conflict.status_code == 409
    assert conflict.json()["current"]["version"] == 2


def test_vergebener_name_422(client: TestClient) -> None:
    """F-EXP-03: Ein doppelter Profilname wird abgelehnt."""

    assert client.post("/api/export-profiles", json={"name": "Doppelt"}).status_code == 201
    second = client.post("/api/export-profiles", json={"name": "Doppelt"})
    assert second.status_code == 422
    assert second.json()["code"] == "domain_error"


def test_mehrere_standardprofile_nur_eines(client: TestClient) -> None:
    """F-EXP-03: Ein neues Standardprofil loescht das Kennzeichen der anderen."""

    first = client.post("/api/export-profiles", json={"name": "Erstes", "is_default": True}).json()
    second = client.post("/api/export-profiles", json={"name": "Zweites", "is_default": True}).json()

    with _session(client) as session:
        defaults = [profile for profile in list_profiles(session) if profile.is_default]

    assert [profile.id for profile in defaults] == [second["id"]]
    refreshed = client.get(f"/api/export-profiles/{first['id']}").json()
    assert refreshed["is_default"] is False


def test_ungueltige_option_422(client: TestClient) -> None:
    """F-EXP-03: Unbekannte Trennzeichen/Spalten werden abgelehnt."""

    bad_option = client.post("/api/export-profiles", json={"name": "A", "options": {"delimiter": "|"}})
    assert bad_option.status_code == 422

    bad_column = client.post("/api/export-profiles", json={"name": "B", "columns": ["unbekannt"]})
    assert bad_column.status_code == 422


# ------------------------------------------------------------------- Export


def test_export_mit_profil_steuert_csv(client: TestClient) -> None:
    """F-EXP-03: Ein Profil steuert Spalten, Trennzeichen und Formate des Exports."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, booking_date=date(2026, 1, 15), amount_cents=-4883, counterparty="Markt")

    profile = client.post(
        "/api/export-profiles",
        json={
            "name": "Komma-Profil",
            "options": {"delimiter": ",", "decimal_separator": "point", "date_format": "JJJJ-MM-TT"},
            "columns": ["booking_date", "counterparty", "amount"],
        },
    ).json()

    response = client.post(
        "/api/export",
        json={
            "format": "csv",
            "scope": "account",
            "account_id": account_id,
            "period": "all",
            "profile_id": profile["id"],
        },
    )

    assert response.status_code == 200
    lines = response.text.strip().splitlines()
    assert lines[0] == "Buchungstag,Gegenpartei,Betrag"
    assert lines[1] == "2026-01-15,Markt,-48.83"


def test_export_mit_unbekanntem_profil_422(client: TestClient) -> None:
    """F-EXP-03: Ein unbekanntes Profil wird als ungueltige Anfrage gemeldet."""

    account_id = _add_account(client, "Girokonto")
    response = client.post(
        "/api/export",
        json={"format": "csv", "scope": "account", "account_id": account_id, "period": "all", "profile_id": 999},
    )
    assert response.status_code == 422
