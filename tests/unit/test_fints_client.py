"""Tests fuer den lesenden FinTS-Adapter (:mod:`finue.services.fints_client`).

Referenziert F-SYN-01..05 (Konten, Einzelabruf, Saldo/Umsaetze, TAN,
Fehler-Mapping), F-AUF-05 (Auftragsabruf nicht unterstuetzt), F-DUP-04
(Statustrennung), D2/D3 (python-fints, Pause/Resume), N-SEC-02 (keine PIN in
Logs/Fehlern), N-SEC-03 (Dialogzustand nur im Speicher), N-SEC-05 (Endpunkt
nur aus ``Bank.fints_url``) und D8 (nur Fake-Client, kein Netz).

Der Fake wird wie in ``test_fints_fake.py`` direkt aus ``tests/fixtures``
geladen, weil ``tests`` kein Python-Paket ist.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fints.exceptions import FinTSUnsupportedOperation

from finue.config import Settings
from finue.models.bank import Bank
from finue.services.fints_client import (
    FinTSGateway,
    MissingEndpointError,
    MissingProductIdError,
    TanRequired,
    TransactionFetch,
)
from finue.services.fints_types import (
    READ_ONLY_OPERATIONS,
    AccessLockedError,
    BalanceResult,
    FinTSBankError,
    OrdersResult,
    PinWrongError,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
SCRIPT_FACTORIES = fints_fake.SCRIPT_FACTORIES
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
ENDPOINT = "https://example.invalid/fints"

_ORDERS_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake_orders.py"
)
_ORDERS_SPEC = importlib.util.spec_from_file_location(
    "fints_fake_orders", _ORDERS_FIXTURE
)
if _ORDERS_SPEC is None or _ORDERS_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("fints_fake_orders.py konnte nicht geladen werden")
fints_fake_orders = importlib.util.module_from_spec(_ORDERS_SPEC)
sys.modules[_ORDERS_SPEC.name] = fints_fake_orders
_ORDERS_SPEC.loader.exec_module(fints_fake_orders)


class FakeFactory:
    """Baut Fake-Clients und merkt sich Aufbau- und Wiederaufnahmedaten (D8).

    ``from_data`` liefert im Standardfall denselben, bereits pausierten
    Client zurueck. Damit laesst sich der ``from_data``/``resume_dialog``-
    Pfad des Gateways pruefen, ohne dass der Fake seinen TAN-Fortschritt
    serialisieren muesste.
    """

    def __init__(self, script_name: str, *, client_type: type[Any] | None = None) -> None:
        self._script_name = script_name
        self._client_type = client_type if client_type is not None else FakeClient
        self.client: Any | None = None
        self.init_kwargs: list[dict[str, Any]] = []
        self.restore_states: list[bytes] = []

    def __call__(self, **kwargs: Any) -> Any:
        if "from_data" in kwargs:
            self.restore_states.append(kwargs["from_data"])
            if self.client is not None:
                return self.client
        client = self._client_type(script=SCRIPT_FACTORIES[self._script_name](), **kwargs)
        self.init_kwargs.append(dict(kwargs))
        self.client = client
        return client


def _settings(product_id: str | None = FAKE_PRODUCT_ID) -> Settings:
    """Settings ohne .env-Datei, damit der Test isoliert bleibt."""
    return Settings(_env_file=None, fints_product_id=product_id)


def _bank(
    fints_url: str | None = ENDPOINT,
    blz: str = fints_fake.FAKE_BANK_IDENTIFIER,
) -> Bank:
    return Bank(blz=blz, name="Erfundene Testbank", fints_url=fints_url)


def _gateway(
    script_name: str,
    *,
    client_type: type[Any] | None = None,
    settings: Settings | None = None,
) -> tuple[FinTSGateway, FakeFactory]:
    factory = FakeFactory(script_name, client_type=client_type)
    gateway = FinTSGateway(
        _bank(),
        login_name=FAKE_USER_ID,
        pin=FAKE_PIN,
        settings=settings if settings is not None else _settings(),
        client_factory=factory,
    )
    return gateway, factory


def _orders_gateway(script_name: str) -> tuple[FinTSGateway, Any]:
    """Gateway mit dem Auftrags-Fake aus ``fints_fake_orders`` (D8)."""
    factory = fints_fake_orders.FakeOrdersFactory(script_name)
    gateway = FinTSGateway(
        _bank(),
        login_name=fints_fake_orders.FAKE_USER_ID,
        pin=fints_fake_orders.FAKE_PIN,
        settings=_settings(),
        client_factory=factory,
    )
    return gateway, factory


def test_product_id_comes_from_environment_without_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2, N-SEC-05: Settings.fints_product_id stammt aus FINTS_PRODUCT_ID."""
    monkeypatch.delenv("FINTS_PRODUCT_ID", raising=False)
    assert Settings(_env_file=None).fints_product_id is None

    monkeypatch.setenv("FINTS_PRODUCT_ID", FAKE_PRODUCT_ID)
    assert Settings(_env_file=None).fints_product_id == FAKE_PRODUCT_ID


def test_missing_product_id_blocks_before_bank_access() -> None:
    """D2, N-SEC-05: ohne Produkt-ID kein Client und kein Bankzugriff."""
    factory = FakeFactory("happy_path")
    with pytest.raises(MissingProductIdError) as excinfo:
        FinTSGateway(
            _bank(),
            login_name=FAKE_USER_ID,
            pin=FAKE_PIN,
            settings=_settings(None),
            client_factory=factory,
        )
    assert "FINTS_PRODUCT_ID" in str(excinfo.value)
    assert factory.client is None
    assert factory.init_kwargs == []


def test_endpoint_only_from_bank_fints_url() -> None:
    """N-SEC-05: der Client erhaelt ausschliesslich Bank.fints_url als Server."""
    gateway, factory = _gateway("happy_path")
    with gateway:
        gateway.get_accounts()

    assert factory.init_kwargs
    assert factory.init_kwargs[0]["server"] == ENDPOINT
    assert factory.init_kwargs[0]["product_id"] == FAKE_PRODUCT_ID
    assert factory.init_kwargs[0]["bank_identifier"] == fints_fake.FAKE_BANK_IDENTIFIER


def test_missing_fints_url_blocks_before_bank_access() -> None:
    """N-SEC-05: fehlender Endpunkt bricht ohne Clientaufbau ab."""
    factory = FakeFactory("happy_path")
    with pytest.raises(MissingEndpointError) as excinfo:
        FinTSGateway(
            _bank(fints_url=None),
            login_name=FAKE_USER_ID,
            pin=FAKE_PIN,
            settings=_settings(),
            client_factory=factory,
        )
    assert "fints_url" in str(excinfo.value)
    assert factory.client is None


def test_get_accounts_uses_only_the_read_operation() -> None:
    """F-SYN-01, D2: Kontenabruf ruft genau ``get_sepa_accounts`` auf."""
    gateway, factory = _gateway("happy_path")
    with gateway:
        accounts = gateway.get_accounts()

    assert [account.iban for account in accounts] == [
        fints_fake.FAKE_IBAN_GIRO,
        fints_fake.FAKE_IBAN_CARD,
    ]
    assert factory.client is not None
    assert factory.client.method_log == ("get_sepa_accounts",)


def test_get_balance_maps_library_balance() -> None:
    """F-SYN-03, N-DB-02: Saldo in ganzen Cent ohne Float."""
    gateway, factory = _gateway("happy_path")
    with gateway:
        account = gateway.get_accounts()[0]
        balance = gateway.get_balance(account)

    assert balance == BalanceResult(
        amount_cents=123456,
        currency="EUR",
        as_of=date(2026, 11, 25),
    )
    assert factory.client is not None
    assert factory.client.method_log == ("get_sepa_accounts", "get_balance")


def test_camt_is_preferred_and_booked_pending_are_separate() -> None:
    """F-SYN-03, F-DUP-04, D2: camt mit getrennten Streams, kein MT940-Fallback."""
    gateway, factory = _gateway("camt_only")
    with gateway:
        account = gateway.get_accounts()[0]
        fetched = gateway.get_transactions(account)

    assert isinstance(fetched, TransactionFetch)
    assert [row.amount_cents for row in fetched.booked] == [-1000]
    assert [row.amount_cents for row in fetched.pending] == [-500]
    assert [row.status for row in fetched.booked] == ["gebucht"]
    assert [row.status for row in fetched.pending] == ["vorgemerkt"]
    assert fetched.total == 2
    assert factory.client is not None
    assert "get_transactions_xml" in factory.client.method_log
    assert "get_transactions" not in factory.client.method_log


def test_mt940_fallback_keeps_pending_separate() -> None:
    """F-SYN-03, F-DUP-04, D2: MT940-Fallback mit include_pending=True."""
    gateway, factory = _gateway("mt940_pending")
    with gateway:
        account = gateway.get_accounts()[0]
        fetched = gateway.get_transactions(account)

    assert [row.amount_cents for row in fetched.booked] == [-1100]
    assert [row.amount_cents for row in fetched.pending] == [-700]
    assert [row.status for row in fetched.booked] == ["gebucht"]
    assert [row.status for row in fetched.pending] == ["vorgemerkt"]
    assert factory.client is not None
    assert "get_transactions_xml" in factory.client.method_log
    assert "get_transactions" in factory.client.method_log


def test_unsupported_camt_falls_back_to_mt940() -> None:
    """F-SYN-03, D2: fehlendes HKCAZ fuehrt zum MT940-Fallback."""

    class UnsupportedXmlFake(FakeClient):
        def get_transactions_xml(self, *args: Any, **kwargs: Any) -> Any:
            self._log("get_transactions_xml")
            raise FinTSUnsupportedOperation("HKCAZ wird nicht unterstuetzt")

    gateway, factory = _gateway("mt940_pending", client_type=UnsupportedXmlFake)
    with gateway:
        account = gateway.get_accounts()[0]
        fetched = gateway.get_transactions(account)

    assert [row.status for row in fetched.booked] == ["gebucht"]
    assert [row.status for row in fetched.pending] == ["vorgemerkt"]
    assert factory.client is not None
    assert "get_transactions" in factory.client.method_log


def test_time_range_is_passed_to_the_library() -> None:
    """F-SYN-03: Start- und Enddatum erreichen die Bibliothek unveraendert."""
    calls: list[tuple[date | None, date | None]] = []

    class RecordingFake(FakeClient):
        def get_transactions_xml(
            self,
            account: Any,
            start_date: date | None = None,
            end_date: date | None = None,
            supported_camt_messages: Any = None,
        ) -> Any:
            calls.append((start_date, end_date))
            return super().get_transactions_xml(
                account, start_date, end_date, supported_camt_messages
            )

    gateway, _factory = _gateway("camt_only", client_type=RecordingFake)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    with gateway:
        account = gateway.get_accounts()[0]
        gateway.get_transactions(account, start_date=start, end_date=end)

    assert calls == [(start, end)]


def test_invalid_time_range_is_rejected() -> None:
    """F-SYN-03: ein verdrehter Zeitraum wird vor dem Bankzugriff abgelehnt."""
    gateway, factory = _gateway("camt_only")
    with gateway:
        account = gateway.get_accounts()[0]
        with pytest.raises(ValueError):
            gateway.get_transactions(
                account,
                start_date=date(2026, 2, 1),
                end_date=date(2026, 1, 1),
            )

    assert factory.client is not None
    assert "get_transactions_xml" not in factory.client.method_log


def test_missing_booking_date_is_pending_and_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-068-Risiko, F-DUP-04: fehlender Buchungstag wird nie still gebucht."""
    gateway, _factory = _gateway("mt940_pending")
    with caplog.at_level(logging.WARNING, logger="finue.services.fints_client"):
        with gateway:
            account = gateway.get_accounts()[0]
            fetched = gateway.get_transactions(account)

    assert [row.status for row in fetched.pending] == ["vorgemerkt"]
    assert "ohne Buchungstag" in caplog.text
    assert "1 Buchung" in caplog.text
    assert FAKE_PIN not in caplog.text
    assert fints_fake.FAKE_IBAN_GIRO not in caplog.text
    assert "Erfundene MT940-Vormerkung" not in caplog.text


def test_tan_text_flow_pauses_and_resumes() -> None:
    """F-SYN-04, D3: NeedTANResponse -> TanChallenge, Pause, Resume, send_tan."""
    gateway, factory = _gateway("tan_text")
    with pytest.raises(TanRequired) as excinfo:
        with gateway:
            account = gateway.get_accounts()[0]
            gateway.get_balance(account)

    handoff = excinfo.value.handoff
    assert handoff.challenge.kind == "text"
    assert "TAN" in handoff.challenge.challenge
    assert handoff.dialog_state.startswith(b"fake-dialog-state-")
    assert FAKE_PIN.encode() not in handoff.dialog_state
    assert FAKE_PIN.encode() not in handoff.client_state
    assert factory.client is not None
    assert factory.client.pause_count == 1
    assert factory.client.deconstruct_count == 1

    result = gateway.resume_tan(handoff, "123456")
    assert result.amount_cents == 123456
    assert factory.restore_states == [handoff.client_state]
    assert factory.client.resume_count == 1
    assert factory.client.send_tan_count == 1


def test_app_approval_is_polled_until_success() -> None:
    """F-SYN-04, D3: mehrere App-Freigaben, dann Erfolg ohne TAN-Eingabe."""
    gateway, factory = _gateway("tan_app")
    with pytest.raises(TanRequired) as first:
        with gateway:
            account = gateway.get_accounts()[0]
            gateway.get_balance(account)

    first_handoff = first.value.handoff
    assert first_handoff.challenge.kind == "app"
    assert "1/2" in first_handoff.challenge.challenge
    assert first_handoff.is_app_approval

    with pytest.raises(TanRequired) as second:
        gateway.poll_app_approval(first_handoff)
    second_handoff = second.value.handoff
    assert second_handoff.challenge.kind == "app"
    assert "2/2" in second_handoff.challenge.challenge

    result = gateway.poll_app_approval(second_handoff)
    assert result.amount_cents == 123456
    assert factory.client is not None
    assert factory.client.resume_count == 2
    assert factory.client.send_tan_count == 2


def test_text_tan_requires_a_tan_value() -> None:
    """F-SYN-04: textuelle TAN ohne Eingabe wird abgelehnt."""
    gateway, _factory = _gateway("tan_text")
    with pytest.raises(TanRequired) as excinfo:
        with gateway:
            account = gateway.get_accounts()[0]
            gateway.get_balance(account)

    with pytest.raises(ValueError):
        gateway.resume_tan(excinfo.value.handoff)


@pytest.mark.parametrize(
    ("script_name", "expected_type", "code"),
    [
        ("consors_9800", PinWrongError, "9800"),
        ("wrong_pin", PinWrongError, "9942"),
        ("locked", AccessLockedError, "3938"),
        ("consors_9010", FinTSBankError, "9010"),
    ],
)
def test_bank_errors_are_translated_with_code(
    script_name: str,
    expected_type: type[FinTSBankError],
    code: str,
) -> None:
    """F-SYN-05, N-SEC-02: Code und Originaltext, keine PIN in der Meldung."""
    gateway, factory = _gateway(script_name)
    with pytest.raises(expected_type) as excinfo:
        with gateway:
            account = gateway.get_accounts()[0]
            gateway.get_balance(account)

    error = excinfo.value
    assert type(error) is expected_type
    assert error.code == code
    assert error.original_text.startswith(code)
    assert FAKE_PIN not in str(error)
    assert FAKE_PIN not in repr(error)
    assert factory.client is not None
    assert set(factory.client.method_log) == {"get_sepa_accounts", "get_balance"}


def test_standing_orders_are_reported_as_unsupported() -> None:
    """F-AUF-05: Bank ohne HKCDB/HKCSB liefert unsupported ohne Fehler."""
    gateway, factory = _orders_gateway("orders_unsupported")

    with gateway:
        account = gateway.get_accounts()[0]
        result = gateway.fetch_standing_orders(account)

    assert result == OrdersResult(status="unsupported")
    assert result.orders == ()
    assert factory.client is not None
    assert factory.client.request_log == []


def test_standing_orders_are_fetched_and_mapped() -> None:
    """F-AUF-02, F-AUF-04: HKCDB/HKCSB werden gelesen und abgebildet."""
    gateway, factory = _orders_gateway("orders_present")

    with gateway:
        account = gateway.get_accounts()[0]
        result = gateway.fetch_standing_orders(account)

    assert result.status == "available"
    assert [order.kind for order in result.orders] == [
        "dauerauftrag",
        "terminauftrag",
    ]
    assert factory.client is not None
    assert factory.client.request_log == ["HKCDB", "HKCSB"]


def test_standing_orders_empty_is_not_an_error() -> None:
    """F-AUF-05: unterstuetzter Abruf ohne Auftraege liefert empty."""
    gateway, factory = _orders_gateway("orders_empty")

    with gateway:
        account = gateway.get_accounts()[0]
        result = gateway.fetch_standing_orders(account)

    assert result == OrdersResult(status="empty")
    assert factory.client is not None
    assert factory.client.request_log == ["HKCDB", "HKCSB"]


def test_standing_orders_tan_requires_handoff() -> None:
    """F-SYN-04, D3: TAN-Aufforderung beim Auftragsabruf pausiert den Dialog."""
    gateway, factory = _orders_gateway("orders_tan_required")

    with pytest.raises(TanRequired) as excinfo:
        with gateway:
            account = gateway.get_accounts()[0]
            gateway.fetch_standing_orders(account)

    handoff = excinfo.value.handoff
    assert handoff.challenge.kind == "text"
    assert "TAN" in handoff.challenge.challenge
    assert fints_fake_orders.FAKE_PIN.encode() not in handoff.dialog_state
    assert fints_fake_orders.FAKE_PIN.encode() not in handoff.client_state
    assert factory.client is not None
    assert factory.client.pause_count == 1

    result = gateway.resume_tan(handoff, "123456")
    assert result.status == "available"
    assert [order.kind for order in result.orders] == [
        "dauerauftrag",
        "terminauftrag",
    ]


def test_only_read_only_operations_are_used() -> None:
    """D2: alle Bankoperationen stammen aus der Read-only-Whitelist."""
    gateway, factory = _gateway("happy_path")
    with gateway:
        account = gateway.get_accounts()[0]
        gateway.get_balance(account)
        gateway.get_transactions(account)

    assert factory.client is not None
    bank_operations = {
        operation
        for operation in factory.client.method_log
        if operation.startswith("get_")
    }
    assert bank_operations <= READ_ONLY_OPERATIONS
    assert not bank_operations & fints_fake.WRITE_OPERATIONS


def test_pin_never_appears_in_logs_or_repr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N-SEC-02: PIN steht weder im Log noch in repr/Fehlertexten."""
    gateway, factory = _gateway("tan_text")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TanRequired) as excinfo:
            with gateway:
                account = gateway.get_accounts()[0]
                gateway.get_balance(account)
        handoff = excinfo.value.handoff
        gateway.resume_tan(handoff, "123456")

    assert FAKE_PIN not in caplog.text
    assert FAKE_PIN not in repr(gateway)
    assert FAKE_PIN not in repr(handoff)
    assert FAKE_PIN not in str(excinfo.value)
    assert factory.client is not None
    assert FAKE_PIN not in repr(factory.client)
