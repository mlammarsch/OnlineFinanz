"""API-E2E-Szenario Teil 1 gegen einen echten Uvicorn-Prozess (T-113).

Ein zusammenhaengender Durchlauf auf einem frischen Server (Fixtures aus
``tests/e2e_api/conftest.py``): Leerzustand, Gruppe und Offline-Konto anlegen,
CSV-Import (Erkennen, Vorschau, Lauf), Buchungsliste mit Vormerkungen und
Tagessalden, Suche (Text, Betrag, Zeitraum, Filter), Kennzeichen, Notiz,
externe Referenz und Batch mit provoziertem Konflikt, Abstimmungsuebersicht
gegen Handrechnung, Konto archivieren und wiederherstellen sowie Icon-Upload
und -Auslieferung. Jeder aendernde Schritt prueft die erwarteten SSE-Ereignisse
(F-CON-04).

Der FinTS-Client ist durch den Fake ersetzt (D8); es gibt keinen Netzwerk-
zugriff und keine echten Kontodaten (N-SEC-01/02). Referenzierte Anforderungen:
F-KTO-01..06, F-IMP-01..04/06, F-BUC-01..06, F-SUC-01/02/04/07, F-ABS-01..05,
F-CON-01..03, F-DET-01, F-KTO-11, F-CON-04.
"""

from __future__ import annotations

import io
import json
import time
from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest
from PIL import Image

from finue.services.duplicate import compute_fingerprint

if TYPE_CHECKING:
    from tests.e2e_api.conftest import SseListener

pytestmark = pytest.mark.api_e2e

IBAN_HINT = "DE89370400440532013000"
OPENING_CENTS = 100_000
ANCHOR_DATE = "2026-01-01"

#: Mapping der Test-CSV-Spalten auf Buchungsfelder (F-IMP-03).
CSV_MAPPING: dict[str, str] = {
    "Datum": "booking_date",
    "Betrag": "amount_cents",
    "Gegenpartei": "counterparty",
    "Zweck": "purpose",
}

#: Drei gebuchte Buchungen (eine Ausgabe, ein Eingang, eine Ausgabe); Betraege
#: in deutscher Schreibweise (Komma), Datum TT.MM.JJJJ (F-IMP-02).
CSV_CONTENT = (
    "Datum;Betrag;Gegenpartei;Zweck\n"
    "10.02.2026;-48,83;Max Mustermann;Miete\n"
    "12.02.2026;2500,00;Arbeitgeber;Gehalt\n"
    "15.02.2026;-12,34;Supermarkt;Einkauf\n"
).encode("utf-8")

#: Erwartete Summen nach dem Import (Handrechnung in den Asserts).
BOOKED_SUM_CENTS = -4883 + 250_000 - 1234
EXPECTED_BALANCE_CENTS = OPENING_CENTS + BOOKED_SUM_CENTS  # 343883


def _expect_event(listener: SseListener, event_type: str, mark: int) -> None:
    """Erwartet ein Ereignis des Typs nach dem Positionsmarker ``mark`` (F-CON-04)."""

    assert listener.wait_for(event_type, since=mark, timeout=10.0), (
        f"Ereignis {event_type!r} fehlt; empfangen: {listener.types_since(mark)}"
    )


def _expect_no_event(listener: SseListener, mark: int) -> None:
    """Stellt sicher, dass ein reiner Lese-Schritt kein Ereignis meldet."""

    time.sleep(1.0)
    assert listener.types_since(mark) == [], f"Unerwartete Ereignisse: {listener.types_since(mark)}"


def _detect(api_client: httpx.Client, content: bytes) -> dict[str, object]:
    """Ruft die Formaterkennung auf und liefert die Antwort (F-IMP-01/04)."""

    response = api_client.post("/api/import/detect", content=content)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _pending_document(account_id: int, account_name: str) -> dict[str, object]:
    """Baut ein eigenes JSON-Dokument mit genau einer vorgemerkten Buchung (F-IMP-04).

    Die CSV-Zuordnung kann keine Vormerkung anlegen (kein Status-Zielfeld,
    F-IMP-03); fuer den Vormerkungsbereich der Buchungsliste wird deshalb eine
    vorgemerkte Buchung ueber das dokumentierte eigene JSON-Format importiert.
    """

    fingerprint = compute_fingerprint(
        account_id, date(2026, 2, 20), None, -5000, "EUR", "Vormerker", None, "Vorgemerkt"
    )
    return {
        "format": "finanzuebersicht",
        "version": 1,
        "created_at": "2026-11-26T10:41:13Z",
        "filter": {
            "scope": "account",
            "account_ids": [account_id],
            "group_ids": None,
            "transaction_ids": None,
            "search": None,
            "checked": None,
            "reconciled": None,
            "with_note": None,
        },
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
        "accounts": [{"id": account_id, "name": account_name, "iban": None, "currency": "EUR", "account_type": "giro"}],
        "transactions": [
            {
                "id": account_id * 1000 + 1,
                "account_id": account_id,
                "status": "vorgemerkt",
                "booking_date": "2026-02-20",
                "value_date": None,
                "counterparty": "Vormerker",
                "counterparty_iban": None,
                "purpose": "Vorgemerkt",
                "note": None,
                "checked": False,
                "checked_at": None,
                "checked_origin": None,
                "reconciled": False,
                "reconciled_at": None,
                "reconciled_origin": None,
                "origin": "json",
                "external_ref": None,
                "fingerprint": fingerprint,
                "occurrence_index": 1,
                "merged": False,
                "amount": "-50.00",
                "currency": "EUR",
            }
        ],
    }


def _png_bytes() -> bytes:
    """Erzeugt ein kleines, gueltiges PNG fuer den Icon-Upload (F-KTO-11)."""

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_scenario_a(api_client: httpx.Client, sse_listener: SseListener) -> None:
    """Fuehrt das Szenario Teil 1 zusammenhaengend aus und prueft die Ereignisse."""

    # --- Leerzustand -----------------------------------------------------
    accounts = api_client.get("/api/accounts")
    assert accounts.status_code == 200, accounts.text
    assert accounts.json()["items"] == []
    assert accounts.json()["total"]["amount_cents"] == 0
    assert api_client.get("/api/groups").json()["items"] == []

    # --- Gruppe anlegen --------------------------------------------------
    mark = sse_listener.count
    created_group = api_client.post("/api/groups", json={"name": "Haushalt"})
    assert created_group.status_code == 201, created_group.text
    group_id = created_group.json()["id"]
    _expect_event(sse_listener, "group.changed", mark)

    # --- Offline-Konto anlegen ------------------------------------------
    mark = sse_listener.count
    created_account = api_client.post(
        "/api/accounts",
        json={
            "name": "Giro Test",
            "currency": "EUR",
            "account_type": "giro",
            "iban": IBAN_HINT,
            "opening_balance_cents": OPENING_CENTS,
            "opening_balance_date": ANCHOR_DATE,
            "group_id": group_id,
        },
    )
    assert created_account.status_code == 201, created_account.text
    account_id = created_account.json()["id"]
    _expect_event(sse_listener, "account.changed", mark)

    listing = api_client.get("/api/accounts").json()
    assert [account["id"] for account in listing["items"]] == [account_id]
    assert listing["total"]["amount_cents"] == OPENING_CENTS
    assert listing["items"][0]["iban_masked"] is not None

    # --- CSV hochladen, erkennen, Vorschau, importieren ------------------
    mark = sse_listener.count
    detected = _detect(api_client, CSV_CONTENT)
    assert detected["kind"] == "csv"
    token = detected["token"]

    preview = api_client.post(
        "/api/import/preview",
        json={"token": token, "account_id": account_id, "mapping": CSV_MAPPING},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"] == {"neu": 3, "vorhanden": 0, "verdachtsdublette": 0}

    imported = api_client.post(
        "/api/import/execute",
        json={"token": token, "account_id": account_id, "mapping": CSV_MAPPING},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["new_count"] == 3
    assert imported.json()["kind"] == "csv"
    assert imported.json()["status"] == "erfolgreich"
    _expect_event(sse_listener, "import.finished", mark)
    _expect_event(sse_listener, "transaction.changed", mark)

    # --- Vormerkung ueber das eigene JSON-Format importieren -------------
    mark = sse_listener.count
    detected_pending = _detect(api_client, json.dumps(_pending_document(account_id, "Giro Test")).encode("utf-8"))
    assert detected_pending["kind"] == "own_json"
    pending_run = api_client.post(
        "/api/import/execute",
        json={"token": detected_pending["token"], "account_id": account_id},
    )
    assert pending_run.status_code == 200, pending_run.text
    assert pending_run.json()["new_count"] == 1
    _expect_event(sse_listener, "import.finished", mark)
    _expect_event(sse_listener, "transaction.changed", mark)

    # --- Buchungsliste mit Vormerkungen und Tagessalden ------------------
    mark = sse_listener.count
    listing_response = api_client.get(f"/api/accounts/{account_id}/transactions", params={"period": "all"})
    assert listing_response.status_code == 200, listing_response.text
    booked = listing_response.json()
    assert booked["head"]["name"] == "Giro Test"
    assert booked["head"]["balance_cents"] == EXPECTED_BALANCE_CENTS
    assert booked["pending"]["total_count"] == 1
    assert booked["pending"]["sum_cents"] == -5000
    assert booked["pending"]["balance_with_pending_cents"] == EXPECTED_BALANCE_CENTS - 5000
    assert [day["day"] for day in booked["days"]] == ["2026-02-15", "2026-02-12", "2026-02-10"]
    # Tagessaldo rueckwaerts vom aktuellen Saldo (F-BUC-03).
    assert [day["balance_end_cents"] for day in booked["days"]] == [343883, 345117, 95117]
    assert [day["sum_cents"] for day in booked["days"]] == [-1234, 250000, -4883]
    _expect_no_event(sse_listener, mark)

    # Einzelne Buchungen fuer die spaeteren Schritte greifen (F-BUC-04).
    tx_miete = booked["days"][2]["transactions"][0]
    tx_gehalt = booked["days"][1]["transactions"][0]
    tx_einkauf = booked["days"][0]["transactions"][0]
    assert tx_miete["amount_cents"] == -4883 and tx_miete["origin"] == "csv"

    # --- Gruppenansicht mit Kontokennzeichnung je Zeile (F-BUC-05) -------
    group_list = api_client.get(f"/api/groups/{group_id}/transactions", params={"period": "all"})
    assert group_list.status_code == 200, group_list.text
    group_booked = group_list.json()
    assert group_booked["head"]["scope"] == "group"
    assert group_booked["head"]["balance_cents"] == EXPECTED_BALANCE_CENTS
    assert group_booked["days"][0]["transactions"][0]["account_name"] == "Giro Test"

    # --- Suche: Text, Betrag, Zeitraum, Filter (F-SUC-01/02/04/07) -------
    text_hits = api_client.get("/api/search", params={"q": "Miete", "period": "all"}).json()
    assert text_hits["total_count"] == 1
    assert text_hits["items"][0]["purpose"] == "Miete"

    amount_hits = api_client.get("/api/search", params={"q": "48,83", "period": "all"}).json()
    assert amount_hits["total_count"] == 1
    assert amount_hits["items"][0]["amount_cents"] == -4883
    assert amount_hits["total_cents"] == -4883

    signed_hits = api_client.get("/api/search", params={"q": "-48,83", "period": "all"}).json()
    assert signed_hits["total_count"] == 1

    period_hits = api_client.get(
        "/api/search",
        params={"period": "custom", "from": "2026-02-11", "to": "2026-02-16"},
    ).json()
    assert period_hits["total_count"] == 2

    filter_hits = api_client.get("/api/search", params={"status": "vorgemerkt", "period": "all"}).json()
    assert filter_hits["total_count"] == 1
    assert filter_hits["items"][0]["status"] == "vorgemerkt"

    # --- Kennzeichen, Notiz, Referenz (F-ABS-01/02/05, F-CON-01/02) ------
    mark = sse_listener.count
    flagged = api_client.put(f"/api/transactions/{tx_miete['id']}/checked", json={"value": True})
    assert flagged.status_code == 200, flagged.text
    assert flagged.json()["checked"] is True
    assert flagged.json()["checked_origin"] == "ui"
    _expect_event(sse_listener, "transaction.changed", mark)
    version = flagged.json()["version"]

    mark = sse_listener.count
    noted = api_client.put(
        f"/api/transactions/{tx_miete['id']}/note",
        json={"text": "Erste Notiz", "version": version},
    )
    assert noted.status_code == 200, noted.text
    assert noted.json()["note"] == "Erste Notiz"
    _expect_event(sse_listener, "transaction.changed", mark)
    version = noted.json()["version"]

    mark = sse_listener.count
    appended = api_client.post(f"/api/transactions/{tx_miete['id']}/note/append", json={"text": "Zweite Zeile"})
    assert appended.status_code == 200, appended.text
    assert appended.json()["note"] == "Erste Notiz\nZweite Zeile"
    _expect_event(sse_listener, "transaction.changed", mark)
    version = appended.json()["version"]

    mark = sse_listener.count
    referenced = api_client.put(
        f"/api/transactions/{tx_miete['id']}/external-ref",
        json={"value": "REF-2026-001", "version": version},
    )
    assert referenced.status_code == 200, referenced.text
    assert referenced.json()["external_ref"] == "REF-2026-001"
    _expect_event(sse_listener, "transaction.changed", mark)

    mark = sse_listener.count
    reconciled = api_client.put(f"/api/transactions/{tx_gehalt['id']}/reconciled", json={"value": True})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["reconciled"] is True
    _expect_event(sse_listener, "transaction.changed", mark)

    # --- Batch mit provoziertem Konflikt (F-CON-03) ----------------------
    detail_einkauf = api_client.get(f"/api/transactions/{tx_einkauf['id']}").json()
    mark = sse_listener.count
    batch = api_client.post(
        "/api/transactions/batch",
        json={
            "ids": [tx_miete["id"], tx_einkauf["id"]],
            "action": "checked",
            "value": True,
            "versions": {str(tx_miete["id"]): 1, str(tx_einkauf["id"]): detail_einkauf["version"]},
        },
    )
    assert batch.status_code == 200, batch.text
    results = batch.json()["results"]
    assert [result["outcome"] for result in results] == ["konflikt", "geaendert"]
    assert results[0]["current"] is not None
    assert results[0]["current"]["checked"] is True
    _expect_event(sse_listener, "transaction.changed", mark)

    # --- Abstimmungsuebersicht gegen Handrechnung (F-ABS-03) -------------
    account_recon = api_client.get(f"/api/accounts/{account_id}/reconciliation")
    assert account_recon.status_code == 200, account_recon.text
    recon = account_recon.json()
    assert recon["opening_balance_cents"] == OPENING_CENTS
    assert recon["reconciled_balance_cents"] == OPENING_CENTS + 250_000
    assert recon["unreconciled_count"] == 2
    assert recon["unreconciled_sum_cents"] == -4883 - 1234
    assert recon["balance_cents"] == OPENING_CENTS
    assert recon["difference_cents"] == OPENING_CENTS - (OPENING_CENTS + 250_000)
    assert recon["opening_balance_derived"] is False

    group_recon = api_client.get(f"/api/groups/{group_id}/reconciliation").json()
    assert group_recon["member_count"] == 1
    assert group_recon["reconciled_balance_cents"] == OPENING_CENTS + 250_000

    # --- Konto archivieren und wiederherstellen (F-KTO-06, F-KTO-02) -----
    before_archive = api_client.get(f"/api/accounts/{account_id}").json()
    mark = sse_listener.count
    archived = api_client.post(
        f"/api/accounts/{account_id}/archive",
        json={"version": before_archive["version"]},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["active"] is False
    _expect_event(sse_listener, "account.changed", mark)

    active_after = api_client.get("/api/accounts").json()
    assert active_after["items"] == []
    assert active_after["total"]["amount_cents"] == 0
    archived_listing = api_client.get("/api/accounts", params={"archived": "true"}).json()
    assert [account["id"] for account in archived_listing["items"]] == [account_id]

    mark = sse_listener.count
    restored = api_client.post(
        f"/api/accounts/{account_id}/restore",
        json={"version": archived.json()["version"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["active"] is True
    _expect_event(sse_listener, "account.changed", mark)
    assert api_client.get("/api/accounts").json()["total"]["amount_cents"] == OPENING_CENTS

    # --- Icon hochladen und abrufen (F-KTO-11) ---------------------------
    current = api_client.get(f"/api/accounts/{account_id}").json()
    mark = sse_listener.count
    uploaded = api_client.put(
        f"/api/accounts/{account_id}/icon",
        params={"version": current["version"]},
        content=_png_bytes(),
        headers={"content-type": "image/png"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_icon"] is True
    assert uploaded.json()["icon_url"] == f"/api/accounts/{account_id}/icon"
    _expect_event(sse_listener, "account.changed", mark)

    icon = api_client.get(f"/api/accounts/{account_id}/icon")
    assert icon.status_code == 200, icon.text
    assert icon.headers["content-type"] == "image/png"
    etag = icon.headers["etag"]
    not_modified = api_client.get(f"/api/accounts/{account_id}/icon", headers={"if-none-match": etag})
    assert not_modified.status_code == 304
    assert api_client.get("/api/accounts").json()["items"][0]["has_icon"] is True
