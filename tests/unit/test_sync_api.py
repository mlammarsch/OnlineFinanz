"""Tests des Sync-REST, der TAN-Endpunkte und des Sync-Protokolls (T-112).

Referenzierte Anforderungen:

- F-SYN-01: ``POST /api/sync`` startet den Batch-Sync und kehrt sofort mit der
  Job-ID zurueck; der Lauf laeuft im Hintergrund (D3).
- F-SYN-02: ``POST /api/accounts/{id}/sync`` synchronisiert ein einzelnes Konto.
- F-SYN-04: ``GET /api/sync/jobs/{id}`` zeigt Status und Challenge; ``POST
  .../tan`` und ``POST .../approve-poll`` setzen den Dialog fort. Die zweite
  TAN-Antwort liefert 409. Der 5-Minuten-Timeout raeumt ab.
- F-SYN-07: ``GET /api/sync/runs`` listet das Protokoll, paginiert und je Konto
  filterbar.
- F-SYN-08: Bei ``store_pin = false`` kommt die PIN im Anfragekoerper.
- F-SYN-10: ``POST .../cancel`` bricht den Lauf ab.
- F-SYN-11: Der Doppelstart liefert den laufenden Job, ohne ihn zu verdoppeln.
- F-CON-04: ``sync.status``, ``sync.tan_required`` und ``sync.tan_answered``
  werden ueber den Ereignisbus gemeldet.
- N-SEC-01..03: PIN und TAN kommen nur im Arbeitsspeicher vor und erscheinen in
  keiner Antwort, keinem Log und keinem Ereignis.
- N-DB-04: Waehrend ``AWAITING_TAN`` ist keine DB-Session und keine Verbindung
  offen (Verbindungspool leer).

Es gibt keine echten Bankzugriffe (D8): Der Fake-Client antwortet aus einem
Skript, alle Konten, Betraege, PIN und TAN sind frei erfunden.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finue.config import Settings
from finue.main import create_app
from finue.models.account import Account
from finue.services.bank_access import create_access
from finue.services.bank_error_defaults import seed_defaults
from finue.services.events import Event

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_api", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_BANK_IDENTIFIER = fints_fake.FAKE_BANK_IDENTIFIER
FAKE_SERVER = fints_fake.FAKE_SERVER
FAKE_IBAN_GIRO = fints_fake.FAKE_IBAN_GIRO
FAKE_IBAN_CARD = fints_fake.FAKE_IBAN_CARD
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
script_happy_path = fints_fake.script_happy_path
script_tan_app = fints_fake.script_tan_app
script_tan_text = fints_fake.script_tan_text

UTC = timezone.utc
# Offensichtliche, erfundene Testmuster - keine echten Geheimnisse (D8).
PIN_MARKER = "PIN-GEHEIM-0815"
TAN_MARKER = "TAN-GEHEIM-4711"


# ------------------------------------------------------------------ Helfer


class FakeClock:
    """Injizierbare Uhr fuer den deterministischen 5-Minuten-Timeout."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _batch_script() -> FakeScript:
    """Zwei Konten, je ein Saldo; keine Umsaetze."""

    return FakeScript(name="api_batch", accounts=fake_accounts(), balance=(fake_balance(), fake_balance()))


class RecordingTanClient(FakeClient):
    """Fake-Client, der Text-TANs mitschreibt (Beweis der Zustellung)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tan_values: list[str] = []

    def send_tan(self, challenge: Any, tan: str | None = None) -> Any:
        if tan:
            self.tan_values.append(tan)
        return super().send_tan(challenge, tan)


class SharedClientFactory:
    """Liefert immer denselben Fake-Client fuer alle Dialoge eines Tests."""

    def __init__(self, script: FakeScript, *, client_type: type[Any] = FakeClient) -> None:
        self._script = script
        self._client_type = client_type
        self.client: Any | None = None

    def __call__(self, **kwargs: Any) -> Any:
        if self.client is None:
            self.client = self._client_type(script=self._script, **kwargs)
        return self.client


class GatedClient(FakeClient):
    """Fake-Client, der jede Saldoabfrage bis zum Oeffnen des Tors blockiert."""

    def __init__(self, *args: Any, gate: threading.Event, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate

    def get_balance(self, account: Any) -> Any:
        self._gate.wait(timeout=5.0)
        return super().get_balance(account)


class GatedClientFactory:
    """Fabrik fuer einen blockierenden Fake-Client (Doppelstart)."""

    def __init__(self, script: FakeScript, gate: threading.Event) -> None:
        self._script = script
        self._gate = gate
        self.client: Any | None = None

    def __call__(self, **kwargs: Any) -> Any:
        if self.client is None:
            self.client = GatedClient(script=self._script, gate=self._gate, **kwargs)
        return self.client


def _settings(tmp_path: Path, name: str, *, auth_required: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / (name + '.db')).as_posix()}",
        data_dir=tmp_path,
        fints_product_id=FAKE_PRODUCT_ID,
        auth_required=auth_required,
    )


def _make_app(
    tmp_path: Path,
    factory: Callable[..., Any],
    *,
    name: str = "sync",
    clock: Callable[[], datetime] | None = None,
    auth_required: bool = False,
) -> FastAPI:
    return create_app(
        _settings(tmp_path, name, auth_required=auth_required),
        clock=clock,
        sync_client_factory=factory,
    )


def _seed(client: TestClient, *, second_account: bool = True) -> tuple[int, list[int]]:
    """Legt einen Zugang mit ein oder zwei aktiven FinTS-Konten an (store_pin=False)."""

    with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
        access = create_access(
            session,
            blz=FAKE_BANK_IDENTIFIER,
            login_name=FAKE_USER_ID,
            store_pin=False,
            bank_name="Erfundene Testbank",
        )
        access.bank.fints_url = FAKE_SERVER
        first = Account(
            name="Erfundenes Girokonto",
            iban=FAKE_IBAN_GIRO,
            currency="EUR",
            source="fints",
            bank_access_id=access.id,
            active=True,
        )
        session.add(first)
        session.flush()
        account_ids = [first.id]
        if second_account:
            second = Account(
                name="Erfundene Kreditkarte",
                iban=FAKE_IBAN_CARD,
                currency="EUR",
                source="fints",
                bank_access_id=access.id,
                active=True,
            )
            session.add(second)
            session.flush()
            account_ids.append(second.id)
        seed_defaults(session)
        session.commit()
        return access.id, account_ids


def _wait_status(client: TestClient, job_id: str, statuses: set[str], *, timeout: float = 3.0) -> dict[str, Any]:
    """Wartet, bis der Job einen der Zustaende erreicht (beschraenkte Schleife)."""

    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/sync/jobs/{job_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.01)
    raise AssertionError(f"Status {statuses} nicht erreicht, zuletzt: {body!r}")


def _start_batch(client: TestClient, access_id: int, *, pin: str | None = PIN_MARKER) -> dict[str, Any]:
    payload: dict[str, Any] = {"bank_access_id": access_id}
    if pin is not None:
        payload["pin"] = pin
    response = client.post("/api/sync", json=payload)
    assert response.status_code == 202, response.text
    jobs = response.json()["jobs"]
    assert len(jobs) == 1
    result: dict[str, Any] = jobs[0]
    return result


def _start_account(client: TestClient, account_id: int, *, pin: str = PIN_MARKER) -> dict[str, Any]:
    response = client.post(f"/api/accounts/{account_id}/sync", json={"pin": pin})
    assert response.status_code == 202, response.text
    result: dict[str, Any] = response.json()
    return result


# ------------------------------------------------------------------ Tests


def test_batch_start_kehrt_sofort_mit_job_id_zurueck_f_syn_01(tmp_path: Path) -> None:
    """F-SYN-01: Der Batch-Sync startet sofort mit Job-ID und laeuft im Hintergrund."""

    app = _make_app(tmp_path, SharedClientFactory(_batch_script()))
    with TestClient(app) as client:
        access_id, account_ids = _seed(client)

        job = _start_batch(client, access_id)

        assert isinstance(job["job_id"], str) and job["job_id"]
        assert job["bank_access_id"] == access_id
        assert job["status"] in {"laeuft", "erfolgreich"}

        finished = _wait_status(client, job["job_id"], {"erfolgreich", "teilweise", "fehler"})
        assert finished["status"] == "erfolgreich"
        result_ids = {item["account_id"] for item in finished["account_results"]}
        assert result_ids == set(account_ids)


def test_sync_runs_listet_protokoll_je_konto_f_syn_07(tmp_path: Path) -> None:
    """F-SYN-07: Das Sync-Protokoll listet die Laeufe, paginiert und je Konto."""

    app = _make_app(tmp_path, SharedClientFactory(_batch_script()))
    with TestClient(app) as client:
        access_id, account_ids = _seed(client)
        job = _start_batch(client, access_id)
        _wait_status(client, job["job_id"], {"erfolgreich", "teilweise", "fehler"})

        alle = client.get("/api/sync/runs").json()
        assert {item["account_id"] for item in alle["items"]} == set(account_ids)
        assert all(item["kind"] == "sync" for item in alle["items"])

        gefiltert = client.get(f"/api/sync/runs?account_id={account_ids[0]}&limit=1").json()
        assert len(gefiltert["items"]) == 1
        assert gefiltert["items"][0]["account_id"] == account_ids[0]


def test_einzel_sync_ueber_konto_route_f_syn_02(tmp_path: Path) -> None:
    """F-SYN-02: ``POST /api/accounts/{id}/sync`` synchronisiert genau ein Konto."""

    app = _make_app(tmp_path, SharedClientFactory(script_happy_path()))
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])

        finished = _wait_status(client, job["job_id"], {"erfolgreich", "teilweise", "fehler"})
        assert finished["status"] == "erfolgreich"
        assert [item["account_id"] for item in finished["account_results"]] == [account_ids[0]]


def test_tan_flow_text_f_syn_04(tmp_path: Path) -> None:
    """F-SYN-04: Challenge sichtbar, TAN-Eingabe setzt den Lauf fort."""

    factory = SharedClientFactory(script_tan_text(), client_type=RecordingTanClient)
    app = _make_app(tmp_path, factory)
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])

        waiting = _wait_status(client, job["job_id"], {"wartet_auf_tan"})
        assert waiting["tan_kind"] == "text"
        assert waiting["challenge"] is not None and "TAN" in waiting["challenge"]
        assert TAN_MARKER not in client.get(f"/api/sync/jobs/{job['job_id']}").text

        response = client.post(f"/api/sync/jobs/{job['job_id']}/tan", json={"tan": TAN_MARKER})

        assert response.status_code == 200
        assert response.json()["status"] == "erfolgreich"
        assert factory.client is not None
        assert factory.client.tan_values == [TAN_MARKER]


def test_zweite_tan_antwort_liefert_409_f_syn_04(tmp_path: Path) -> None:
    """F-SYN-04: Die zweite TAN-Antwort wird mit 409 und aktuellem Stand abgelehnt."""

    app = _make_app(tmp_path, SharedClientFactory(script_tan_text()))
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])
        _wait_status(client, job["job_id"], {"wartet_auf_tan"})

        assert client.post(f"/api/sync/jobs/{job['job_id']}/tan", json={"tan": TAN_MARKER}).status_code == 200
        zweite = client.post(f"/api/sync/jobs/{job['job_id']}/tan", json={"tan": TAN_MARKER})

        assert zweite.status_code == 409
        body = zweite.json()
        assert body["code"] == "conflict"
        assert "current" in body


def test_app_freigabe_per_abfrage_f_syn_04(tmp_path: Path) -> None:
    """F-SYN-04: Die App-Freigabe wird per Intervallabfrage fortgesetzt."""

    app = _make_app(tmp_path, SharedClientFactory(script_tan_app()))
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])

        waiting = _wait_status(client, job["job_id"], {"wartet_auf_tan"})
        assert waiting["tan_kind"] == "app"

        erste = client.post(f"/api/sync/jobs/{job['job_id']}/approve-poll")
        assert erste.status_code == 200
        assert erste.json()["status"] == "wartet_auf_tan"
        assert erste.json()["approval_polls"] == 1

        zweite = client.post(f"/api/sync/jobs/{job['job_id']}/approve-poll")
        assert zweite.status_code == 200
        assert zweite.json()["status"] == "erfolgreich"


def test_abbruch_waehrend_tan_pause_f_syn_10(tmp_path: Path) -> None:
    """F-SYN-10: ``POST .../cancel`` bricht den wartenden Lauf ab."""

    app = _make_app(tmp_path, SharedClientFactory(script_tan_text()))
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])
        _wait_status(client, job["job_id"], {"wartet_auf_tan"})

        response = client.post(f"/api/sync/jobs/{job['job_id']}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == "abgebrochen"
        assert client.get(f"/api/sync/jobs/{job['job_id']}").json()["status"] == "abgebrochen"


def test_timeout_nach_fuenf_minuten_f_syn_04(tmp_path: Path) -> None:
    """F-SYN-04: Der Waechter verwirft den Dialog nach fuenf Minuten."""

    clock = FakeClock(datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    app = _make_app(tmp_path, SharedClientFactory(script_tan_text()), clock=clock)
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])
        _wait_status(client, job["job_id"], {"wartet_auf_tan"})

        runner = client.app.state.services.sync_runner  # type: ignore[attr-defined]
        runner.check_timeouts()
        assert client.get(f"/api/sync/jobs/{job['job_id']}").json()["status"] == "wartet_auf_tan"

        clock.advance(timedelta(minutes=5))
        expired = runner.check_timeouts()

        assert expired == 1
        assert client.get(f"/api/sync/jobs/{job['job_id']}").json()["status"] == "zeitueberschreitung"


def test_keine_session_waehrend_tan_pause_n_db_04(tmp_path: Path) -> None:
    """N-DB-04: Waehrend AWAITING_TAN ist keine Verbindung des Pools belegt."""

    app = _make_app(tmp_path, SharedClientFactory(script_tan_text()))
    with TestClient(app) as client:
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])
        _wait_status(client, job["job_id"], {"wartet_auf_tan"})

        engine = client.app.state.engine  # type: ignore[attr-defined]
        assert engine.pool.checkedout() == 0

        started = time.monotonic()
        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            row = session.get(Account, account_ids[0])
            assert row is not None
            row.balance_cents = 4242
            session.commit()
        assert time.monotonic() - started < 2.0
        assert engine.pool.checkedout() == 0

        client.post(f"/api/sync/jobs/{job['job_id']}/cancel")


def test_doppelstart_liefert_laufenden_job_f_syn_11(tmp_path: Path) -> None:
    """F-SYN-11: Der zweite Anstoss liefert denselben laufenden Job."""

    gate = threading.Event()
    factory = GatedClientFactory(script_happy_path(), gate)
    app = _make_app(tmp_path, factory)
    with TestClient(app) as client:
        access_id, _account_ids = _seed(client, second_account=False)

        erster = _start_batch(client, access_id)
        assert erster["status"] == "laeuft"
        zweiter = _start_batch(client, access_id)

        assert zweiter["job_id"] == erster["job_id"]
        assert zweiter["status"] == "laeuft"

        gate.set()
        finished = _wait_status(client, erster["job_id"], {"erfolgreich", "teilweise", "fehler"})
        assert finished["status"] == "erfolgreich"


def test_pin_fehlend_liefert_422_f_syn_08(tmp_path: Path) -> None:
    """F-SYN-08: Ohne PIN bei store_pin=false antwortet die Route 422."""

    app = _make_app(tmp_path, SharedClientFactory(script_happy_path()))
    with TestClient(app) as client:
        access_id, _account_ids = _seed(client)

        response = client.post("/api/sync", json={"bank_access_id": access_id})

        assert response.status_code == 422
        assert response.json()["code"] == "domain_error"


def test_unbekannter_job_und_zugang_liefern_404(tmp_path: Path) -> None:
    """F-SYN-04: Unbekannte Job-IDs und Bankzugaenge liefern 404."""

    app = _make_app(tmp_path, SharedClientFactory(script_happy_path()))
    with TestClient(app) as client:
        assert client.get("/api/sync/jobs/gibt-es-nicht").status_code == 404
        assert client.post("/api/sync", json={"bank_access_id": 999999, "pin": PIN_MARKER}).status_code == 404


def test_nicht_fints_konto_liefert_422_f_syn_02(tmp_path: Path) -> None:
    """F-SYN-02: Ein Konto ohne FinTS-Zugang ist kein Sync-Ziel."""

    app = _make_app(tmp_path, SharedClientFactory(script_happy_path()))
    with TestClient(app) as client:
        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            offline = Account(name="Erfundenes Barkonto", currency="EUR", source="datei", active=True)
            session.add(offline)
            session.commit()
            offline_id = offline.id

        response = client.post(f"/api/accounts/{offline_id}/sync", json={"pin": PIN_MARKER})

        assert response.status_code == 422
        assert response.json()["code"] == "domain_error"


def test_auth_naht_erzwingt_401_n_sec_04(tmp_path: Path) -> None:
    """N-SEC-04: Mit aktivierter Anmeldung liefern die Sync-Routen 401."""

    app = _make_app(tmp_path, SharedClientFactory(script_happy_path()), name="auth", auth_required=True)
    with TestClient(app) as client:
        assert client.post("/api/sync", json={}).status_code == 401
        assert client.get("/api/sync/runs").status_code == 401


def test_keine_pin_keine_tan_in_antwort_log_und_ereignissen_n_sec_02(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N-SEC-01..03: PIN und TAN erscheinen in keiner Flaeche."""

    caplog.set_level("DEBUG")
    app = _make_app(tmp_path, SharedClientFactory(script_tan_text()))
    events: list[Event] = []
    with TestClient(app) as client:
        client.app.state.event_bus.subscribe(events.append)  # type: ignore[attr-defined]
        _access_id, account_ids = _seed(client)

        start = client.post(f"/api/accounts/{account_ids[0]}/sync", json={"pin": PIN_MARKER})
        job = start.json()
        assert PIN_MARKER not in start.text

        _wait_status(client, job["job_id"], {"wartet_auf_tan"})
        tan_antwort = client.post(f"/api/sync/jobs/{job['job_id']}/tan", json={"tan": TAN_MARKER})

        assert PIN_MARKER not in client.get(f"/api/sync/jobs/{job['job_id']}").text
        assert TAN_MARKER not in tan_antwort.text
        assert PIN_MARKER not in caplog.text
        assert TAN_MARKER not in caplog.text
        for event in events:
            serialized = repr(event.as_dict())
            assert PIN_MARKER not in serialized
            assert TAN_MARKER not in serialized

    assert events, "Es wurden Sync-Ereignisse erwartet."


def test_ereignisse_sync_status_tan_required_tan_answered_f_con_04(tmp_path: Path) -> None:
    """F-CON-04: Status, TAN-Aufforderung und TAN-Antwort werden gemeldet."""

    app = _make_app(tmp_path, SharedClientFactory(script_tan_text()))
    events: list[Event] = []
    with TestClient(app) as client:
        client.app.state.event_bus.subscribe(events.append)  # type: ignore[attr-defined]
        _access_id, account_ids = _seed(client)
        job = _start_account(client, account_ids[0])
        _wait_status(client, job["job_id"], {"wartet_auf_tan"})
        client.post(f"/api/sync/jobs/{job['job_id']}/tan", json={"tan": TAN_MARKER})
        _wait_status(client, job["job_id"], {"erfolgreich"})

    types = [event.type for event in events]
    assert "sync.status" in types
    assert "sync.tan_required" in types
    assert "sync.tan_answered" in types

    tan_required = next(event for event in events if event.type == "sync.tan_required")
    assert tan_required.get("job") == job["job_id"]
    assert tan_required.get("bank_access") == job["bank_access_id"]
