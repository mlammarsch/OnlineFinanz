"""Tests des FinTS-Anlage-Assistenten im Backend (T-091).

Referenzierte Anforderungen:

- F-KTO-05a: Bank waehlen oder BLZ eingeben, Zugangsdaten, TAN-Verfahren,
  Konten des Zugangs auswaehlen. ``GET /api/banks/lookup`` liefert die
  Stammdaten samt Endpunkt-Kennzeichen, ``POST /api/bank-accesses`` legt den
  Zugang an, ``POST .../discover`` ruft die Konten (SEPA, Kontoinformationen,
  TAN-Verfahren) rein lesend ab, ``POST .../accounts`` legt die Auswahl an,
  ``PATCH`` waehlt das TAN-Verfahren (F-CON-02).
- F-SYN-05: Ein falscher PIN liefert Klartext, aber ohne Geheimnis.
- F-SYN-11: Der Kontenabruf sperrt den Zugang ueber die Sync-Registry.
- N-SEC-01..03: PIN nur verschluesselt bzw. im Arbeitsspeicher, nie in Antwort,
  Log, Ereignis oder DB-Klartext.

Es gibt keine echten Bankzugriffe (D8): Der Fake-Client antwortet aus einem
Skript, alle Konten, Betraege, PIN und TAN sind erfunden. Die IBANs sind
gueltige, aber frei erfundene Testwerte (keine echten Konten).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fints.exceptions import FinTSClientPINError
from fints.models import SEPAAccount

from finue.config import Settings
from finue.main import create_app
from finue.models.account import Account
from finue.services.events import Event
from finue.services.pin_crypto import PinCipher, create_dev_key_file

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_wizard", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FakeAccountInfo = fints_fake.FakeAccountInfo
FakeTanMechanism = fints_fake.FakeTanMechanism
FakeTanChallenge = fints_fake.FakeTanChallenge
FakeTanFlow = fints_fake.FakeTanFlow
FakeBankError = fints_fake.FakeBankError
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_BANK_IDENTIFIER = fints_fake.FAKE_BANK_IDENTIFIER
FAKE_SERVER = fints_fake.FAKE_SERVER
FAKE_BIC = fints_fake.FAKE_BIC
DIALOG_OPERATIONS = fints_fake.DIALOG_OPERATIONS

# Erfundene PIN/TAN-Marker - keine echten Geheimnisse (D8, N-SEC-02).
PIN_MARKER = "PIN-GEHEIM-0815"
TAN_MARKER = "TAN-GEHEIM-4711"


def _valid_iban(country: str, bban: str) -> str:
    """Erzeugt eine IBAN mit gueltiger Pruefziffer aus frei erfundener BBAN."""

    rearranged = bban + country + "00"
    digits = "".join(str(ord(char) - 55) if char.isalpha() else char for char in rearranged)
    check = 98 - (int(digits) % 97)
    return f"{country}{check:02d}{bban}"


# Gueltige, frei erfundene Test-IBANs (keine echten Konten, D8).
VALID_IBAN_GIRO = _valid_iban("DE", "500105170123456789")
VALID_IBAN_CARD = _valid_iban("DE", "100500000987654321")


def _valid_accounts() -> tuple[Any, ...]:
    """Erfundene Konten mit gueltiger Pruefziffer (D8)."""

    return (
        SEPAAccount(
            iban=VALID_IBAN_GIRO,
            bic=FAKE_BIC,
            accountnumber="123456789",
            subaccount="",
            blz=FAKE_BANK_IDENTIFIER,
        ),
        SEPAAccount(
            iban=VALID_IBAN_CARD,
            bic=FAKE_BIC,
            accountnumber="987654321",
            subaccount="",
            blz=FAKE_BANK_IDENTIFIER,
        ),
    )


def _discovery_script(*, accounts_outcome: tuple[Any, ...] = ()) -> FakeScript:
    """Skript fuer den Kontenabruf mit Kontoinformationen und TAN-Verfahren."""

    return FakeScript(
        name="discovery",
        accounts=_valid_accounts(),
        accounts_outcome=accounts_outcome,
        bank_name="Erfundene Testbank",
        account_information=(
            FakeAccountInfo(
                iban=VALID_IBAN_GIRO,
                account_type="Girokonto",
                product_name="Girokonto Online",
                owner_name="Erfundene Person",
            ),
            FakeAccountInfo(
                iban=VALID_IBAN_CARD,
                account_type="Kreditkarte",
                product_name=None,
                owner_name="Erfundene Person",
            ),
        ),
        tan_mechanisms=(
            FakeTanMechanism(security_function="900", name="mobileTAN"),
            FakeTanMechanism(security_function="920", name="BestSign", decoupled=True),
        ),
    )


class SharedClientFactory:
    """Liefert immer denselben Fake-Client (Wiederaufnahme eines TAN-Dialogs)."""

    def __init__(self, script: FakeScript) -> None:
        self._script = script
        self.client: Any | None = None

    def __call__(self, **kwargs: Any) -> Any:
        if self.client is None:
            self.client = FakeClient(script=self._script, **kwargs)
        return self.client


def _settings(tmp_path: Path, name: str, *, key_file: Path | None = None) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "database_url": f"sqlite:///{(tmp_path / (name + '.db')).as_posix()}",
        "data_dir": tmp_path,
        "fints_product_id": FAKE_PRODUCT_ID,
    }
    if key_file is not None:
        kwargs["key_file"] = key_file
    return Settings(**kwargs)


def _make_app(
    tmp_path: Path,
    factory: Callable[..., Any] | None,
    *,
    name: str = "wizard",
    key_file: Path | None = None,
) -> FastAPI:
    return create_app(
        _settings(tmp_path, name, key_file=key_file),
        clock=None,
        sync_client_factory=factory,
    )


def _create_bank(client: TestClient, *, fints_url: str | None = FAKE_SERVER) -> dict[str, Any]:
    response = client.post(
        "/api/banks",
        json={"blz": FAKE_BANK_IDENTIFIER, "name": "Erfundene Testbank", "fints_url": fints_url},
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def _create_access(client: TestClient, *, store_pin: bool = False, pin: str | None = PIN_MARKER) -> dict[str, Any]:
    body: dict[str, Any] = {"blz": FAKE_BANK_IDENTIFIER, "login_name": FAKE_USER_ID, "store_pin": store_pin}
    if pin is not None:
        body["pin"] = pin
    response = client.post("/api/bank-accesses", json=body)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def _wait_status(client: TestClient, job_id: str, statuses: set[str], *, timeout: float = 3.0) -> dict[str, Any]:
    """Wartet in einer beschraenkten Schleife, bis der Job einen Zustand erreicht."""

    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/sync/jobs/{job_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.01)
    raise AssertionError(f"Status {statuses} nicht erreicht, zuletzt: {body!r}")


# ---------------------------------------------------------------- Lookup


def test_bank_lookup_findet_stammdaten_und_endpunkt_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Das Lookup nennt die Bank und ob ein FinTS-Endpunkt hinterlegt ist."""

    app = _make_app(tmp_path, None)
    with TestClient(app) as client:
        _create_bank(client)

        found = client.get(f"/api/banks/lookup?blz={FAKE_BANK_IDENTIFIER}")
        assert found.status_code == 200
        body = found.json()
        assert body["found"] is True
        assert body["name"] == "Erfundene Testbank"
        assert body["fints_configured"] is True

        ohne = client.get("/api/banks/lookup?blz=99999999")
        assert ohne.status_code == 200
        assert ohne.json()["found"] is False
        assert ohne.json()["fints_configured"] is False


def test_bank_lookup_lehnt_ungueltige_blz_ab_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Eine nicht achtstellige BLZ wird als 422 abgelehnt."""

    app = _make_app(tmp_path, None)
    with TestClient(app) as client:
        response = client.get("/api/banks/lookup?blz=123")

        assert response.status_code == 422
        assert response.json()["code"] == "domain_error"


# --------------------------------------------------------------- Zugang


def test_bankzugang_anlegen_ohne_pin_speicherung_n_sec_01(tmp_path: Path) -> None:
    """N-SEC-01: Bei store_pin=false bleibt kein Chiffretext zurueck und keine PIN in der Antwort."""

    app = _make_app(tmp_path, None)
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client, store_pin=False)

        assert access["store_pin"] is False
        assert access["version"] == 1
        assert PIN_MARKER not in str(access)

        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            from finue.models.bank import BankAccess

            row = session.get(BankAccess, access["id"])
            assert row is not None
            assert row.pin_encrypted is None


def test_bankzugang_anlegen_verschluesselt_pin_n_sec_01(tmp_path: Path) -> None:
    """N-SEC-01: Bei store_pin=true liegt nur ein Fernet-Chiffretext vor, nie die PIN."""

    key_file = create_dev_key_file(tmp_path / "key")
    app = _make_app(tmp_path, None, name="enc", key_file=key_file)
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client, store_pin=True)

        assert access["store_pin"] is True
        assert PIN_MARKER not in str(access)

        # Aus der Datenbank geholte Bytes enthalten nicht die PIN; der Cipher
        # entschluesselt sie wieder.
        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            from finue.models.bank import BankAccess

            row = session.get(BankAccess, access["id"])
            assert row is not None
            assert row.pin_encrypted is not None
            assert PIN_MARKER.encode("utf-8") not in row.pin_encrypted
            token = row.pin_encrypted
        assert PinCipher(Settings(_env_file=None, key_file=key_file)).decrypt_pin(token) == PIN_MARKER


def test_tan_verfahren_per_patch_mit_version_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Das TAN-Verfahren laesst sich mit Versionspruefung waehlen."""

    app = _make_app(tmp_path, None)
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)

        gewaehlt = client.patch(f"/api/bank-accesses/{access['id']}", json={"version": 1, "tan_method": "920"})
        assert gewaehlt.status_code == 200
        assert gewaehlt.json()["tan_method"] == "920"
        assert gewaehlt.json()["version"] == 2

        veraltet = client.patch(f"/api/bank-accesses/{access['id']}", json={"version": 1, "tan_method": "900"})
        assert veraltet.status_code == 409
        assert veraltet.json()["code"] == "conflict"
        assert veraltet.json()["current"]["tan_method"] == "920"


# --------------------------------------------------------------- Kontenabruf


def test_kontenabruf_ohne_tan_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Der Abruf liefert Konten mit IBAN, Name, Typ und TAN-Verfahren."""

    app = _make_app(tmp_path, SharedClientFactory(_discovery_script()))
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)

        started = client.post(f"/api/bank-accesses/{access['id']}/discover", json={"pin": PIN_MARKER})
        assert started.status_code == 202, started.text
        job = started.json()
        assert job["discovery"] is None

        finished = _wait_status(client, job["job_id"], {"erfolgreich", "fehler"})
        assert finished["status"] == "erfolgreich"
        discovery = finished["discovery"]
        assert discovery["bank_name"] == "Erfundene Testbank"

        by_iban = {item["iban"]: item for item in discovery["accounts"]}
        giro = by_iban[VALID_IBAN_GIRO]
        assert giro["account_type"] == "Girokonto"
        assert giro["name"] == "Girokonto Online"
        assert giro["already_exists"] is False
        karte = by_iban[VALID_IBAN_CARD]
        assert karte["name"] == "Erfundene Person"

        codes = {method["code"] for method in discovery["tan_methods"]}
        assert codes == {"900", "920"}


def test_kontenabruf_mit_tan_f_ktos_05a_und_syn_04(tmp_path: Path) -> None:
    """F-KTO-05a/F-SYN-04: Eine TAN pausiert den Abruf und setzt ihn fort."""

    flow = FakeTanFlow(
        challenges=(FakeTanChallenge("Bitte TAN eingeben: 123456"),),
        result=list(_valid_accounts()),
    )
    app = _make_app(tmp_path, SharedClientFactory(_discovery_script(accounts_outcome=(flow,))))
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)

        started = client.post(f"/api/bank-accesses/{access['id']}/discover", json={"pin": PIN_MARKER}).json()
        waiting = _wait_status(client, started["job_id"], {"wartet_auf_tan"})
        assert waiting["tan_kind"] == "text"
        assert waiting["challenge"] is not None
        assert TAN_MARKER not in client.get(f"/api/sync/jobs/{started['job_id']}").text

        antwort = client.post(f"/api/sync/jobs/{started['job_id']}/tan", json={"tan": TAN_MARKER})
        assert antwort.status_code == 200
        assert antwort.json()["status"] == "erfolgreich"
        assert len(antwort.json()["discovery"]["accounts"]) == 2


def test_kontenabruf_falsche_pin_klartext_f_syn_05(tmp_path: Path) -> None:
    """F-SYN-05: Ein falscher PIN endet mit Klartext und ohne Geheimnis."""

    error = FakeBankError(FinTSClientPINError, "9942", "9942: PIN falsch (erfundener Testfall)")
    app = _make_app(tmp_path, SharedClientFactory(_discovery_script(accounts_outcome=(error,))))
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)

        started = client.post(f"/api/bank-accesses/{access['id']}/discover", json={"pin": PIN_MARKER}).json()
        finished = _wait_status(client, started["job_id"], {"erfolgreich", "fehler"})

        assert finished["status"] == "fehler"
        assert finished["message"] == "Die Bank hat die PIN abgelehnt."
        assert PIN_MARKER not in str(finished)


def test_kontenabruf_markiert_bereits_angelegtes_konto_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Ein bereits gefuehrtes Konto traegt das Kennzeichen 'bereits angelegt'."""

    app = _make_app(tmp_path, SharedClientFactory(_discovery_script()))
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)
        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            session.add(Account(name="Erfundenes Bestandskonto", iban=VALID_IBAN_GIRO, currency="EUR", source="datei"))
            session.commit()

        started = client.post(f"/api/bank-accesses/{access['id']}/discover", json={"pin": PIN_MARKER}).json()
        finished = _wait_status(client, started["job_id"], {"erfolgreich", "fehler"})

        by_iban = {item["iban"]: item for item in finished["discovery"]["accounts"]}
        assert by_iban[VALID_IBAN_GIRO]["already_exists"] is True
        assert by_iban[VALID_IBAN_CARD]["already_exists"] is False


def test_konten_anlegen_ohne_doppelte_iban_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Die Auswahl legt neue Konten an und ueberspringt vorhandene IBANs."""

    app = _make_app(tmp_path, None)
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)
        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            session.add(Account(name="Erfundenes Bestandskonto", iban=VALID_IBAN_GIRO, currency="EUR", source="datei"))
            session.commit()

        response = client.post(
            f"/api/bank-accesses/{access['id']}/accounts",
            json={
                "accounts": [
                    {"iban": VALID_IBAN_GIRO, "name": "Girokonto Online", "account_type": "Girokonto"},
                    {"iban": VALID_IBAN_CARD, "name": "Kreditkarte", "account_type": "Kreditkarte"},
                    {"iban": VALID_IBAN_CARD, "name": "Kreditkarte doppelt", "account_type": "Kreditkarte"},
                ]
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["name"] for item in body["created"]] == ["Kreditkarte"]
        assert body["skipped"] == [VALID_IBAN_GIRO, VALID_IBAN_CARD]
        assert body["created"][0]["source"] == "fints"

        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            from sqlalchemy import select

            rows = list(session.scalars(select(Account).where(Account.source == "fints")))
            assert len(rows) == 1
            assert rows[0].bank_access_id == access["id"]


def test_konten_anlegen_ohne_auswahl_f_kto_05a(tmp_path: Path) -> None:
    """F-KTO-05a: Eine Auswahl ohne neue IBAN wird als 422 abgelehnt."""

    app = _make_app(tmp_path, None)
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)
        with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
            session.add(Account(name="Erfundenes Bestandskonto", iban=VALID_IBAN_GIRO, currency="EUR", source="datei"))
            session.commit()

        response = client.post(
            f"/api/bank-accesses/{access['id']}/accounts",
            json={"accounts": [{"iban": VALID_IBAN_GIRO}]},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "domain_error"


# ------------------------------------------------------------------ Leckage


def test_pin_nicht_in_antwort_log_ereignis_und_db_n_sec_02(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N-SEC-01..03: Die PIN erscheint in keiner Flaeche des Assistenten."""

    caplog.set_level("DEBUG")
    key_file = create_dev_key_file(tmp_path / "key")
    app = _make_app(tmp_path, SharedClientFactory(_discovery_script()), name="leak", key_file=key_file)
    events: list[Event] = []
    with TestClient(app) as client:
        client.app.state.event_bus.subscribe(events.append)  # type: ignore[attr-defined]
        _create_bank(client)

        angelegt = client.post(
            "/api/bank-accesses",
            json={"blz": FAKE_BANK_IDENTIFIER, "login_name": FAKE_USER_ID, "store_pin": True, "pin": PIN_MARKER},
        )
        assert angelegt.status_code == 201
        assert PIN_MARKER not in angelegt.text
        access = angelegt.json()

        gestartet = client.post(f"/api/bank-accesses/{access['id']}/discover", json={"pin": PIN_MARKER})
        assert gestartet.status_code == 202
        job = gestartet.json()
        _wait_status(client, job["job_id"], {"erfolgreich", "fehler"})
        assert PIN_MARKER not in client.get(f"/api/sync/jobs/{job['job_id']}").text

        assert PIN_MARKER not in caplog.text
        for event in events:
            assert PIN_MARKER not in repr(event.as_dict())


def test_kontenabruf_nutzt_nur_lesende_operationen_d2(tmp_path: Path) -> None:
    """D2: Der Assistent ruft ausschliesslich lesende FinTS-Operationen auf."""

    factory = SharedClientFactory(_discovery_script())
    app = _make_app(tmp_path, factory)
    with TestClient(app) as client:
        _create_bank(client)
        access = _create_access(client)
        started = client.post(f"/api/bank-accesses/{access['id']}/discover", json={"pin": PIN_MARKER}).json()
        _wait_status(client, started["job_id"], {"erfolgreich", "fehler"})

    assert factory.client is not None
    log = set(factory.client.method_log)
    assert log
    assert log <= (fints_fake.FAKE_READ_OPERATIONS | DIALOG_OPERATIONS)
    assert fints_fake.WRITE_OPERATIONS.isdisjoint(log)
