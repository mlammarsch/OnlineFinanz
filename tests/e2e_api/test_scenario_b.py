"""API-E2E-Szenario Teil 2 gegen einen echten Uvicorn-Prozess (T-115).

Ein eigenstaendiger Durchlauf auf einem frischen Server (Fixtures aus
``tests/e2e_api/conftest.py``, Daten ausschliesslich per API, kein Zustand aus
Szenario Teil 1): FinTS-Anlage-Assistent mit Fake-Bank (Bank anlegen, BLZ-Lookup,
Bankzugang anlegen, Konten entdecken und anlegen), Batch-Sync mit Text-TAN
(SSE meldet ``sync.tan_required``, TAN per API, Auftraege lesbar), Verdachts-
dublette aus CSV neben FinTS (zusammenfuehren, rueckgaengig, erneut
zusammenfuehren), Export JSON/CSV/Markdown mit Re-Import = 0 neu, Kontoloeschung
nach JSON-Export ohne Restzeilen, Sitzungsliste sowie ein Leckage-Scan ueber
Server-Log und DB-Datei. Jeder aendernde Schritt prueft die erwarteten
SSE-Ereignisse (F-CON-04).

Das Fake-FinTS-Skript ist ``sync_tan`` (D8); es verwendet eine formal gueltige,
frei erfundene IBAN, weil der Assistent die IBAN prueft. Es gibt keinen
Netzwerkzugriff und keine echten Zugangsdaten (N-SEC-01/02). Referenzierte
Anforderungen: F-KTO-05a/07/10, F-SYN-01/04, F-AUF-05, F-DUP-03/05,
F-EXP-01..06, F-IMP-03/04, F-CON-02/04/06.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from finue.services.export import CSV_COLUMN_LABELS

if TYPE_CHECKING:
    from tests.e2e_api.conftest import E2EServer, SseListener

pytestmark = pytest.mark.api_e2e

#: Frei erfundene Geheimnis-Marker: sie duerfen nie in Log oder DB auftauchen
#: (N-SEC-02). ``store_pin=false`` haelt die PIN nur im Arbeitsspeicher, die TAN
#: wird nur im Aufruf uebergeben.
PIN_MARKER = "PIN-E2E-0815"
TAN_MARKER = "TAN-E2E-4711"

BLZ = "37040044"
FINTS_URL = "https://fints.example.invalid/fints"
BANK_NAME = "Erfundene Testbank"

#: CSV neben der FinTS-Buchung: gleicher Betrag, Folgetag, abweichender Text
#: ergibt ein Verdachtspaar (F-DUP-03).
DUPLICATE_CSV = ("Datum;Betrag;Gegenpartei;Zweck\n21.11.2026;-42,00;Andere Firma;Anderer Verwendungszweck\n").encode(
    "utf-8"
)
DUPLICATE_MAPPING: dict[str, str] = {
    "Datum": "booking_date",
    "Betrag": "amount_cents",
    "Gegenpartei": "counterparty",
    "Zweck": "purpose",
}

#: Mapping des eigenen CSV-Exports auf die Buchungsfelder (F-EXP-06). Die
#: Quellspalten sind die Kopfzeilen des CSV-Exports; ueber
#: :data:`finue.services.export.CSV_COLUMN_LABELS` bleibt der Abgleich stabil.
OWN_CSV_MAPPING: dict[str, Any] = {
    "fields": {
        "booking_date": CSV_COLUMN_LABELS["booking_date"],
        "value_date": CSV_COLUMN_LABELS["value_date"],
        "amount_cents": CSV_COLUMN_LABELS["amount"],
        "currency": CSV_COLUMN_LABELS["currency"],
        "counterparty": CSV_COLUMN_LABELS["counterparty"],
        "counterparty_iban": CSV_COLUMN_LABELS["counterparty_iban"],
        "purpose": CSV_COLUMN_LABELS["purpose"],
    },
    "kind": "csv",
    "currency_default": "EUR",
}

#: Formatoptionen des eigenen CSV-Exports (Standardprofil, F-EXP-03).
OWN_CSV_FORMAT: dict[str, Any] = {
    "delimiter": ";",
    "decimal_separator": "comma",
    "date_format": "TT.MM.JJJJ",
    "has_header": True,
}

_JOB_TIMEOUT = 25.0


def _expect_event(listener: SseListener, event_type: str, mark: int) -> None:
    """Erwartet ein Ereignis des Typs nach dem Positionsmarker ``mark`` (F-CON-04)."""

    assert listener.wait_for(event_type, since=mark, timeout=10.0), (
        f"Ereignis {event_type!r} fehlt; empfangen: {listener.types_since(mark)}"
    )


def _expect_no_event(listener: SseListener, mark: int) -> None:
    """Stellt sicher, dass ein reiner Lese-Schritt kein Ereignis meldet."""

    time.sleep(1.0)
    assert listener.types_since(mark) == [], f"Unerwartete Ereignisse: {listener.types_since(mark)}"


def _wait_job(api_client: httpx.Client, job_id: str, *, timeout: float = _JOB_TIMEOUT) -> dict[str, Any]:
    """Poliert einen Sync-/Discovery-Job, bis er nicht mehr aktiv ist (F-SYN-04)."""

    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api_client.get(f"/api/sync/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] not in {"laeuft", "wartet_auf_tan"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} blieb aktiv: {body!r}")


def _wait_status(
    api_client: httpx.Client,
    job_id: str,
    wanted: set[str],
    *,
    timeout: float = _JOB_TIMEOUT,
) -> dict[str, Any]:
    """Wartet auf einen konkreten Statuswert eines Jobs."""

    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api_client.get(f"/api/sync/jobs/{job_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in wanted:
            return body
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} erreichte {wanted} nicht: {body!r}")


def _booked_rows(api_client: httpx.Client, account_id: int) -> list[dict[str, Any]]:
    """Liefert die gebuchten Buchungen eines Kontos (F-BUC-04)."""

    response = api_client.get(f"/api/accounts/{account_id}/transactions", params={"period": "all"})
    assert response.status_code == 200, response.text
    data = response.json()
    return [row for day in data["days"] for row in day["transactions"]]


def _read_bytes_with_retry(path: Path, attempts: int = 5) -> bytes:
    """Liest eine Datei, die ein anderer Prozess offen haelt (Leckage-Scan)."""

    last: OSError | None = None
    for _ in range(attempts):
        try:
            return path.read_bytes()
        except OSError as exc:  # noqa: PERF203 - kurzer Wiederholungsversuch (Windows-Lock)
            last = exc
            time.sleep(0.2)
    raise AssertionError(f"Datei {path} nicht lesbar: {last!r}")


def _assert_account_fully_removed(db_path: Path, account_id: int) -> None:
    """Prueft, dass nach der Loeschung keine kontobezogenen Zeilen bleiben (F-KTO-07)."""

    connection = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        for table in ("transaction", "import_run", "standing_order"):
            count = connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE account_id = ?', (account_id,)
            ).fetchone()[0]
            assert count == 0, f"{table}: {count} Restzeilen fuer Konto {account_id}"
        remaining = connection.execute('SELECT COUNT(*) FROM "account" WHERE id = ?', (account_id,)).fetchone()[0]
        assert remaining == 0, f"account: Konto {account_id} existiert noch"
    finally:
        connection.close()


def _assert_no_leakage(server: E2EServer, *markers: str) -> None:
    """Prueft, dass die Geheimnis-Marker nicht in Log oder DB-Dateien stehen (N-SEC-02)."""

    targets = [server.log_path, *[path for path in server.data_dir.rglob("*") if path.is_file()]]
    checked = 0
    for path in targets:
        data = _read_bytes_with_retry(path)
        for marker in markers:
            assert marker.encode("utf-8") not in data, f"Geheimnis {marker!r} in {path.name} gefunden"
        checked += 1
    assert checked >= 2  # mindestens Logdatei und Datenbank


@pytest.mark.parametrize("e2e_server", ["sync_tan"], indirect=True)
def test_scenario_b(e2e_server: E2EServer, api_client: httpx.Client, sse_listener: SseListener) -> None:
    """Fuehrt das Szenario Teil 2 zusammenhaengend aus und prueft die Ereignisse."""

    # --- Leerzustand -----------------------------------------------------
    assert api_client.get("/api/accounts").json()["items"] == []
    assert api_client.get("/api/banks").json()["items"] == []

    # --- Bank anlegen und BLZ nachschlagen (F-KTO-10, F-KTO-05a) ---------
    mark = sse_listener.count
    bank = api_client.post("/api/banks", json={"blz": BLZ, "name": BANK_NAME, "fints_url": FINTS_URL})
    assert bank.status_code == 201, bank.text
    assert bank.json()["fints_url"] == FINTS_URL
    _expect_event(sse_listener, "bank.changed", mark)

    lookup = api_client.get("/api/banks/lookup", params={"blz": BLZ})
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["found"] is True
    assert lookup.json()["fints_configured"] is True

    # --- Bankzugang anlegen (F-KTO-05a, N-SEC-01) ------------------------
    mark = sse_listener.count
    access = api_client.post(
        "/api/bank-accesses",
        json={"blz": BLZ, "login_name": "erfundener-nutzer", "store_pin": False},
    )
    assert access.status_code == 201, access.text
    assert access.json()["store_pin"] is False
    access_id = access.json()["id"]
    _expect_event(sse_listener, "bank_access.changed", mark)

    # --- Konten entdecken (Job des Assistenten, F-KTO-05a) ---------------
    mark = sse_listener.count
    discover = api_client.post(f"/api/bank-accesses/{access_id}/discover", json={"pin": PIN_MARKER})
    assert discover.status_code == 202, discover.text
    discovery_job = _wait_job(api_client, discover.json()["job_id"])
    assert discovery_job["status"] == "erfolgreich", discovery_job
    assert discovery_job["discovery"] is not None
    discovered = discovery_job["discovery"]["accounts"]
    assert len(discovered) == 1
    assert discovered[0]["already_exists"] is False
    giro_iban = discovered[0]["iban"]
    _expect_event(sse_listener, "sync.status", mark)

    # --- Konten anlegen (F-KTO-05a) --------------------------------------
    mark = sse_listener.count
    created = api_client.post(
        f"/api/bank-accesses/{access_id}/accounts",
        json={"accounts": [{"iban": giro_iban, "name": "Erfundenes Girokonto", "account_type": "Girokonto"}]},
    )
    assert created.status_code == 200, created.text
    assert len(created.json()["created"]) == 1
    account_id = created.json()["created"][0]["id"]
    assert created.json()["created"][0]["source"] == "fints"
    _expect_event(sse_listener, "account.changed", mark)

    # --- Batch-Sync mit Text-TAN (F-SYN-01/04) ---------------------------
    mark = sse_listener.count
    sync = api_client.post("/api/sync", json={"bank_access_id": access_id, "pin": PIN_MARKER})
    assert sync.status_code == 202, sync.text
    sync_job_id = sync.json()["jobs"][0]["job_id"]
    waiting = _wait_status(api_client, sync_job_id, {"wartet_auf_tan", "erfolgreich", "fehler"})
    assert waiting["status"] == "wartet_auf_tan", waiting
    assert waiting["tan_kind"] == "text"
    assert waiting["challenge"]
    _expect_event(sse_listener, "sync.tan_required", mark)

    mark = sse_listener.count
    tan = api_client.post(f"/api/sync/jobs/{sync_job_id}/tan", json={"tan": TAN_MARKER})
    assert tan.status_code == 200, tan.text
    finished = _wait_status(api_client, sync_job_id, {"erfolgreich", "teilweise", "fehler"})
    assert finished["status"] == "erfolgreich", finished
    _expect_event(sse_listener, "sync.tan_answered", mark)
    _expect_event(sse_listener, "sync.status", mark)

    # --- Auftraege lesbar (F-AUF-05) -------------------------------------
    orders = api_client.get(f"/api/accounts/{account_id}/standing-orders")
    assert orders.status_code == 200, orders.text
    assert orders.json()["status"] == "available"
    assert len(orders.json()["orders"]) == 1
    assert orders.json()["orders"][0]["kind"] == "dauerauftrag"

    # --- FinTS-Buchung ist angekommen (F-SYN-03) -------------------------
    fintech_rows = _booked_rows(api_client, account_id)
    assert len(fintech_rows) == 1
    assert fintech_rows[0]["origin"] == "fints"
    assert fintech_rows[0]["amount_cents"] == -4200

    # --- CSV daneben importieren (F-DUP-03) ------------------------------
    mark = sse_listener.count
    detected = api_client.post("/api/import/detect", content=DUPLICATE_CSV)
    assert detected.status_code == 200, detected.text
    assert detected.json()["kind"] == "csv"
    token = detected.json()["token"]

    preview = api_client.post(
        "/api/import/preview",
        json={"token": token, "account_id": account_id, "mapping": DUPLICATE_MAPPING},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"]["verdachtsdublette"] == 1

    imported = api_client.post(
        "/api/import/execute",
        json={"token": token, "account_id": account_id, "mapping": DUPLICATE_MAPPING},
    )
    assert imported.status_code == 200, imported.text
    # Die Verdachtszeile wird importiert, aber nach F-IMP-06 getrennt gezaehlt.
    assert imported.json()["suspected_count"] == 1
    assert imported.json()["new_count"] == 0
    _expect_event(sse_listener, "import.finished", mark)
    _expect_event(sse_listener, "transaction.changed", mark)

    # --- Verdachtspaar aufloesen (F-DUP-03/05) ---------------------------
    pairs = api_client.get("/api/duplicates", params={"account_id": account_id})
    assert pairs.status_code == 200, pairs.text
    assert len(pairs.json()["items"]) == 1
    pair = pairs.json()["items"][0]
    assert pair["keep"]["origin"] == "fints"
    assert pair["other"]["origin"] == "csv"
    assert len(_booked_rows(api_client, account_id)) == 2

    mark = sse_listener.count
    merge = api_client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": pair["keep"]["id"],
            "other_id": pair["other"]["id"],
            "versions": {
                str(pair["keep"]["id"]): pair["keep"]["version"],
                str(pair["other"]["id"]): pair["other"]["version"],
            },
        },
    )
    assert merge.status_code == 200, merge.text
    decision_id = merge.json()["decision_id"]
    _expect_event(sse_listener, "duplicate.changed", mark)
    assert len(_booked_rows(api_client, account_id)) == 1

    mark = sse_listener.count
    undo = api_client.post(f"/api/duplicates/{decision_id}/undo")
    assert undo.status_code == 200, undo.text
    _expect_event(sse_listener, "duplicate.changed", mark)
    assert len(_booked_rows(api_client, account_id)) == 2

    pairs_again = api_client.get("/api/duplicates", params={"account_id": account_id}).json()["items"]
    assert len(pairs_again) == 1
    pair_again = pairs_again[0]
    mark = sse_listener.count
    merge_again = api_client.post(
        "/api/duplicates/merge",
        json={
            "keep_id": pair_again["keep"]["id"],
            "other_id": pair_again["other"]["id"],
            "versions": {
                str(pair_again["keep"]["id"]): pair_again["keep"]["version"],
                str(pair_again["other"]["id"]): pair_again["other"]["version"],
            },
        },
    )
    assert merge_again.status_code == 200, merge_again.text
    _expect_event(sse_listener, "duplicate.changed", mark)
    assert len(_booked_rows(api_client, account_id)) == 1

    # --- Export JSON, CSV, Markdown (F-EXP-01..06) -----------------------
    export_body = {"scope": "account", "account_id": account_id, "period": "all"}
    json_export = api_client.post("/api/export", json={**export_body, "format": "json"})
    assert json_export.status_code == 200, json_export.text
    assert json_export.headers["content-type"].startswith("application/json")
    csv_export = api_client.post("/api/export", json={**export_body, "format": "csv"})
    assert csv_export.status_code == 200, csv_export.text
    assert csv_export.headers["content-type"].startswith("text/csv")
    md_export = api_client.post("/api/export", json={**export_body, "format": "md"})
    assert md_export.status_code == 200, md_export.text
    assert md_export.headers["content-type"].startswith("text/markdown")
    assert "Erfundene Beispiel GmbH" in md_export.text

    # --- Re-Import JSON = 0 neu (F-EXP-04/06, F-IMP-04) ------------------
    mark = sse_listener.count
    detected_json = api_client.post("/api/import/detect", content=json_export.content)
    assert detected_json.status_code == 200, detected_json.text
    assert detected_json.json()["kind"] == "own_json"
    reimport_json = api_client.post(
        "/api/import/execute",
        json={"token": detected_json.json()["token"], "account_id": account_id},
    )
    assert reimport_json.status_code == 200, reimport_json.text
    assert reimport_json.json()["new_count"] == 0
    _expect_event(sse_listener, "import.finished", mark)

    # --- Re-Import CSV = 0 neu (F-EXP-06) --------------------------------
    mark = sse_listener.count
    detected_csv = api_client.post("/api/import/detect", content=csv_export.content)
    assert detected_csv.status_code == 200, detected_csv.text
    assert detected_csv.json()["kind"] == "csv"
    reimport_csv = api_client.post(
        "/api/import/execute",
        json={
            "token": detected_csv.json()["token"],
            "account_id": account_id,
            "mapping": OWN_CSV_MAPPING,
            "format": OWN_CSV_FORMAT,
        },
    )
    assert reimport_csv.status_code == 200, reimport_csv.text
    assert reimport_csv.json()["new_count"] == 0
    _expect_event(sse_listener, "import.finished", mark)

    # --- Konto nach dem JSON-Export loeschen (F-KTO-07) ------------------
    version = api_client.get(f"/api/accounts/{account_id}").json()["version"]
    mark = sse_listener.count
    deleted = api_client.delete(f"/api/accounts/{account_id}", params={"version": version})
    assert deleted.status_code == 204, deleted.text
    _expect_event(sse_listener, "account.deleted", mark)
    assert api_client.get(f"/api/accounts/{account_id}").status_code == 404
    assert api_client.get("/api/accounts").json()["items"] == []

    # Keine Restzeilen in der Datenbank (F-KTO-07).
    _assert_account_fully_removed(e2e_server.data_dir / "finue.db", account_id)

    # --- Sitzungsliste abrufbar (F-CON-06) -------------------------------
    mark = sse_listener.count
    sessions = api_client.get("/api/sessions")
    assert sessions.status_code == 200, sessions.text
    assert sessions.json()["items"] == []
    _expect_no_event(sse_listener, mark)

    # --- Leckage-Scan (N-SEC-02) -----------------------------------------
    _assert_no_leakage(e2e_server, PIN_MARKER, TAN_MARKER)
