"""Tests fuer die Import-REST-Schnittstelle (T-100).

Referenziert F-IMP-01..08 (Datei-Import-Assistent: Formaterkennung, Mapping,
Vorschau, Lauf-Protokoll, Vorlagen, PayPal-Vorverarbeitung), F-SYN-07
(Sync-/Import-Lauf), F-CON-02 (Versionspruefung) und SPEC F-MCP-11
(Groessen-/Seitengrenzen). Keine echten Bank- oder Zugangsdaten (D8); alle
Beispiele sind erfundene Testdaten.

Abgedeckt:

- Formaterkennung CSV und eigenes JSON ueber ``POST /api/import/detect``,
- Vorschau und Lauf ueber die API, Re-Import erzeugt 0 neue Buchungen,
- fehlendes Mapping und unbekannter Upload-Token sind deutsche 422-Fehler,
- Uploadgrenze liefert 413 ``payload_too_large``,
- Vorlagen-CRUD mit Versionskonflikt (409) und Vorschlag je Konto,
- Ereignisse ``import.finished`` und ``transaction.changed``,
- TTL und Verdraengung des Upload-Zwischenspeichers.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from finue.config import Settings
from finue.main import create_app
from finue.services.duplicate import compute_fingerprint
from finue.services.import_uploads import UploadExpired, UploadStore

IBAN = "DE89370400440532013000"

#: Mapping der Test-CSV-Spalten auf Buchungsfelder (F-IMP-03).
CSV_MAPPING: dict[str, str] = {
    "Datum": "booking_date",
    "Betrag": "amount_cents",
    "Gegenpartei": "counterparty",
    "IBAN": "counterparty_iban",
    "Zweck": "purpose",
    "Waehrung": "currency",
}

CSV_CONTENT = (
    "Datum;Betrag;Gegenpartei;IBAN;Zweck;Waehrung\n"
    f"15.01.2026;-48,83;Max Mustermann;{IBAN};SEPA Ueberweisung;EUR\n"
    "01.02.2026;-12,34;Neuer Laden;;Einkauf;EUR\n"
).encode("utf-8")

JSON_CONTENT = json.dumps(
    {
        "format": "finanzuebersicht",
        "version": 1,
        "created_at": "2026-11-26T10:41:13Z",
        "filter": {
            "scope": "account",
            "account_ids": [1],
            "group_ids": None,
            "transaction_ids": None,
            "search": None,
            "checked": None,
            "reconciled": None,
            "with_note": None,
        },
        "period": {"start": "2026-01-01", "end": "2026-11-25"},
        "accounts": [
            {"id": 1, "name": "Testkonto", "iban": None, "currency": "EUR", "account_type": "giro"},
        ],
        "transactions": [
            {
                "id": 201,
                "account_id": 1,
                "status": "gebucht",
                "booking_date": "2026-02-10",
                "value_date": None,
                "counterparty": "Neuer Laden",
                "counterparty_iban": None,
                "purpose": "Einkauf",
                "note": "aus Datei",
                "checked": False,
                "checked_at": None,
                "checked_origin": None,
                "reconciled": False,
                "reconciled_at": None,
                "reconciled_origin": None,
                "origin": "csv",
                "external_ref": None,
                "fingerprint": compute_fingerprint(
                    1, date(2026, 2, 10), None, -1234, "EUR", "Neuer Laden", None, "Einkauf"
                ),
                "occurrence_index": 1,
                "merged": False,
                "amount": "-12.34",
                "currency": "EUR",
            }
        ],
    }
).encode("utf-8")


def _create_account(client: TestClient, name: str = "Giro") -> int:
    """Legt ein Offline-Konto und liefert seine ID (F-KTO-05b)."""

    response = client.post("/api/accounts", json={"name": name, "currency": "EUR"})
    assert response.status_code == 201, response.text
    account_id: int = response.json()["id"]
    return account_id


def _detect(client: TestClient, content: bytes, **params: str) -> dict[str, Any]:
    """Ruft die Formaterkennung auf und liefert die Antwort."""

    response = client.post("/api/import/detect", content=content, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ------------------------------------------------------------------ Erkennung


def test_detect_erkennt_csv_und_liefert_formatvorschlag(client: TestClient) -> None:
    """F-IMP-01/02: CSV wird erkannt; Trennzeichen, Dezimal- und Datumsformat kommen als Vorschlag."""

    body = _detect(client, CSV_CONTENT)

    assert body["kind"] == "csv"
    assert body["token"]
    csv = body["csv"]
    assert csv is not None
    assert csv["delimiter"] == ";"
    assert csv["decimal_separator"] == "comma"
    assert csv["date_format"] == "TT.MM.JJJJ"
    assert csv["has_header"] is True
    assert csv["encoding"] in {"utf-8", "utf-8-sig", "cp1252"}


def test_detect_erkennt_eigenes_json_format(client: TestClient) -> None:
    """F-IMP-04: Ein Dokument mit Kopf ``format=finanzuebersicht`` wird als eigenes Format erkannt."""

    body = _detect(client, JSON_CONTENT)

    assert body["kind"] == "own_json"
    assert body["csv"] is None


def test_detect_korrigierbares_trennzeichen(client: TestClient) -> None:
    """F-IMP-02: ein vorgegebenes Trennzeichen ersetzt die Erkennung."""

    body = _detect(client, CSV_CONTENT, delimiter=",")

    assert body["kind"] == "csv"
    assert body["csv"]["delimiter"] == ","


def test_formatkorrektur_dezimal_und_datum_wirkt(client: TestClient) -> None:
    """F-IMP-02: vorgegebenes Dezimal-/Datumsformat steuern die Auswertung der Zeilen."""

    account_id = _create_account(client)
    content = ("Datum;Betrag;Gegenpartei\n15.01.2026;1.234,56;Max Mustermann\n").encode("utf-8")
    token = _detect(client, content, delimiter=";", decimal_separator="comma", date_format="TT.MM.JJJJ")["token"]

    preview = client.post(
        "/api/import/preview",
        json={
            "token": token,
            "account_id": account_id,
            "mapping": {"Datum": "booking_date", "Betrag": "amount_cents", "Gegenpartei": "counterparty"},
            "format": {"decimal_separator": "comma", "date_format": "TT.MM.JJJJ"},
        },
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"][0]["amount_cents"] == 123456


# ------------------------------------------------------------------ Import


def test_csv_import_ueber_die_api_und_reimport_null_neu(client: TestClient) -> None:
    """F-IMP-06, F-DUP-02: CSV ueber die API importieren; der zweite Lauf legt nichts Neues an."""

    account_id = _create_account(client)
    token = _detect(client, CSV_CONTENT)["token"]

    preview = client.post(
        "/api/import/preview",
        json={"token": token, "account_id": account_id, "mapping": CSV_MAPPING},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"] == {"neu": 2, "vorhanden": 0, "verdachtsdublette": 0}

    first = client.post(
        "/api/import/execute",
        json={"token": token, "account_id": account_id, "mapping": CSV_MAPPING},
    )
    assert first.status_code == 200, first.text
    assert first.json()["new_count"] == 2
    assert first.json()["kind"] == "csv"
    assert first.json()["status"] == "erfolgreich"

    second = client.post(
        "/api/import/execute",
        json={"token": token, "account_id": account_id, "mapping": CSV_MAPPING},
    )
    assert second.status_code == 200, second.text
    assert second.json()["new_count"] == 0
    assert second.json()["skipped_count"] == 2


def test_eigenes_json_import_ohne_mapping_und_mit_notiz(client: TestClient) -> None:
    """F-IMP-04/07: eigenes JSON wird ohne Mapping importiert; die Notiz steht im Lauf."""

    account_id = _create_account(client)
    content = JSON_CONTENT.replace(b'"account_id": 1', f'"account_id": {account_id}'.encode())
    content = content.replace(b'"id": 1, "name"', f'"id": {account_id}, "name"'.encode())

    body = _detect(client, content)
    assert body["kind"] == "own_json"
    token = body["token"]

    run = client.post("/api/import/execute", json={"token": token, "account_id": account_id})
    assert run.status_code == 200, run.text
    assert run.json()["new_count"] == 1

    runs = client.get("/api/import-runs", params={"account_id": account_id})
    assert runs.status_code == 200
    assert runs.json()["items"][0]["new_count"] == 1


def test_fehlendes_mapping_ist_422_domain_error(client: TestClient) -> None:
    """F-IMP-03: ohne Mapping liefert CSV eine deutsche 422-Antwort."""

    account_id = _create_account(client)
    token = _detect(client, CSV_CONTENT)["token"]

    response = client.post("/api/import/preview", json={"token": token, "account_id": account_id})

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


def test_unbekannter_upload_token_ist_422(client: TestClient) -> None:
    """F-IMP-01: ein unbekannter/abgelaufener Token bittet um erneuten Upload."""

    account_id = _create_account(client)

    response = client.post(
        "/api/import/preview",
        json={"token": "gibt-es-nicht", "account_id": account_id, "mapping": CSV_MAPPING},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"
    assert "erneut hochladen" in response.json()["message"]


def test_uploadgrenze_liefert_413(tmp_path: Path) -> None:
    """SPEC F-MCP-11: eine Datei ueber der Grenze liefert 413 payload_too_large."""

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'small.db').as_posix()}",
        data_dir=tmp_path,
        api_max_upload_bytes=16,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/import/detect", content=b"x" * 17)

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_leerer_upload_ist_422(client: TestClient) -> None:
    """F-IMP-01: eine leere Datei ist keine gueltige Eingabe."""

    response = client.post("/api/import/detect", content=b"")

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


def test_import_ereignisse_werden_gemeldet(client: TestClient) -> None:
    """F-CON-04: der Lauf meldet ``import.finished`` und ``transaction.changed``."""

    events: list[str] = []
    client.app.state.services.event_bus.subscribe(lambda event: events.append(event.type))

    account_id = _create_account(client)
    token = _detect(client, CSV_CONTENT)["token"]
    client.post("/api/import/execute", json={"token": token, "account_id": account_id, "mapping": CSV_MAPPING})

    assert "import.finished" in events
    assert "transaction.changed" in events


# ------------------------------------------------------------------ Vorlagen


def test_vorlagen_crud_und_versionskonflikt(client: TestClient) -> None:
    """F-IMP-05, F-CON-02: Anlegen, Auflisten, Pflegen mit 409 bei veralteter Version, Loeschen."""

    account_id = _create_account(client)
    created = client.post(
        "/api/import-templates",
        json={"name": "Meine CSV", "kind": "csv", "mapping": CSV_MAPPING, "account_id": account_id},
    )
    assert created.status_code == 201, created.text
    template = created.json()
    template_id = template["id"]
    assert template["version"] == 1
    assert template["delimiter"] is None

    listed = client.get("/api/import-templates", params={"account_id": account_id})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [template_id]

    updated = client.patch(
        f"/api/import-templates/{template_id}",
        json={"version": 1, "name": "Meine CSV neu", "delimiter": ";"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Meine CSV neu"
    assert updated.json()["version"] == 2

    conflict = client.patch(f"/api/import-templates/{template_id}", json={"version": 1, "name": "zu alt"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "conflict"
    assert conflict.json()["current"]["id"] == template_id

    stale_delete = client.delete(f"/api/import-templates/{template_id}", params={"version": 1})
    assert stale_delete.status_code == 409

    deleted = client.delete(f"/api/import-templates/{template_id}", params={"version": 2})
    assert deleted.status_code == 204


def test_vorlagen_vorschlag_je_konto(client: TestClient) -> None:
    """F-IMP-05: kontospezifische Vorlage steht vor der kontounabhaengigen."""

    account_id = _create_account(client)
    client.post("/api/import-templates", json={"name": "Global", "kind": "csv", "mapping": CSV_MAPPING})
    client.post(
        "/api/import-templates",
        json={"name": "Konto", "kind": "csv", "mapping": CSV_MAPPING, "account_id": account_id},
    )

    listed = client.get("/api/import-templates", params={"account_id": account_id})
    names = [item["name"] for item in listed.json()["items"]]

    assert names == ["Konto", "Global"]


def test_vorlage_unbekanntes_konto_ist_422(client: TestClient) -> None:
    """F-IMP-05: eine Vorlage fuer ein unbekanntes Konto wird abgelehnt."""

    response = client.post(
        "/api/import-templates",
        json={"name": "Kaputt", "kind": "csv", "mapping": CSV_MAPPING, "account_id": 999999},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "domain_error"


# ------------------------------------------------------------ Upload-Speicher


def test_upload_store_ttl_und_verdraengung() -> None:
    """F-IMP-01: der Zwischenspeicher laeuft ab und verdraengt das aelteste bei voller Kapazitaet."""

    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    moment = {"value": now}
    store = UploadStore(clock=lambda: moment["value"], ttl_seconds=60, max_entries=1)

    first = store.put(b"eins")
    assert store.get(first) == b"eins"

    second = store.put(b"zwei")  # verdraengt das aelteste (max_entries=1)
    with pytest.raises(UploadExpired):
        store.get(first)
    assert store.get(second) == b"zwei"

    moment["value"] = now + timedelta(seconds=61)
    with pytest.raises(UploadExpired):
        store.get(second)


def test_upload_store_discard_ist_idempotent() -> None:
    """F-IMP-01: ``discard`` entfernt zuverlaessig und stoert bei Wiederholung nicht."""

    store = UploadStore(clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc))
    token = store.put(b"daten")

    store.discard(token)
    store.discard(token)
    with pytest.raises(UploadExpired):
        store.get(token)
