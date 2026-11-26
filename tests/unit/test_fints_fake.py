"""Selbsttest des skriptgesteuerten Fake-Clients fuer python-fints (D8).

Prueft den Fake aus ``tests/fixtures/fints_fake.py`` gegen die in T-060
belegte Bibliothek (python-fints 5.0.1) und deckt die fuer M3 geforderten
Skripte ab:

- F-SYN-01..06: Konten, Saldo, camt/MT940, TAN, App-Freigabe, Bankcodes
- F-SYN-10: Abbruch-/Dialogmethoden werden simuliert und gezaehlt
- F-AUF-05: Auftragsbestand vorhanden, leer, nicht unterstuetzt (Skript-Option)
- N-SEC-02: PIN erscheint nicht in ``repr``, Protokoll oder ``deconstruct``
- N-SEC-03: Dialogdaten bleiben im Arbeitsspeicher
- D8: keine echten Bankzugaenge, kein Netzwerk, nur erfundene Daten

Der Test laedt das Fixture-Modul direkt aus ``tests/fixtures``, weil
``tests`` kein Python-Paket ist.
"""

from __future__ import annotations

import importlib.util
import inspect
import socket
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import mt940
import pytest
from fints.client import NeedTANResponse, camt053_to_dict
from fints.exceptions import FinTSClientError, FinTSClientPINError, FinTSClientTemporaryAuthError
from fints.models import SEPAAccount

from finue.services.fints_types import READ_ONLY_OPERATIONS

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient


def _client(name: str) -> Any:
    """Baut einen Fake-Client mit dem benannten Skript."""
    return FakeClient(script=fints_fake.SCRIPT_FACTORIES[name]())


def _account(client: Any) -> SEPAAccount:
    accounts = client.get_sepa_accounts()
    assert accounts
    return accounts[0]


def test_required_scripts_and_read_only_whitelist() -> None:
    """D8, F-SYN-01..06, F-AUF-05: alle geforderten Skripte sind vorhanden."""
    required = {
        "happy_path",
        "tan_text",
        "tan_app",
        "ing_90_days",
        "consors_9800",
        "consors_9010",
        "wrong_pin",
        "locked",
        "balance_only",
        "camt_only",
        "mt940_pending",
        "orders_present",
        "orders_empty",
        "orders_unsupported",
    }
    assert required <= set(fints_fake.SCRIPT_FACTORIES)
    assert fints_fake.FAKE_READ_OPERATIONS == READ_ONLY_OPERATIONS
    assert fints_fake.WRITE_OPERATIONS.isdisjoint(fints_fake.FAKE_READ_OPERATIONS)

    for name, factory in fints_fake.SCRIPT_FACTORIES.items():
        script = factory()
        assert script.name == name
        assert script.accounts


def test_client_mirrors_library_names_and_signatures() -> None:
    """D8, T-060: gleiche Methodennamen und Parameter wie die Bibliothek."""
    expected = {
        "get_sepa_accounts": ["self"],
        "get_balance": ["self", "account"],
        "get_transactions_xml": ["self", "account", "start_date", "end_date", "supported_camt_messages"],
        "get_transactions": ["self", "account", "start_date", "end_date", "include_pending"],
        "pause_dialog": ["self"],
        "resume_dialog": ["self", "dialog_data"],
        "deconstruct": ["self", "including_private"],
        "send_tan": ["self", "challenge", "tan"],
    }
    for method_name, parameters in expected.items():
        method = getattr(FakeClient, method_name)
        assert list(inspect.signature(method).parameters) == parameters

    client = FakeClient()
    assert client.init_tan_response is None
    assert not callable(client.init_tan_response)
    # Schreibende Bibliotheksmethoden gibt es am Fake bewusst nicht.
    for name in fints_fake.WRITE_OPERATIONS:
        assert not hasattr(client, name)


def test_happy_path_returns_library_types() -> None:
    """F-SYN-03, D2: Saldo, camt und MT940 in den Rueckgabetypen der Bibliothek."""
    client = _client("happy_path")
    account = _account(client)

    balance = client.get_balance(account)
    assert isinstance(balance, mt940.models.Balance)
    assert balance.status == "C"
    assert balance.amount.amount == Decimal("1234.56")
    assert balance.amount.currency == "EUR"

    booked, pending = client.get_transactions_xml(account)
    assert isinstance(booked, list) and isinstance(pending, list)
    assert booked and pending
    assert all(isinstance(stream, bytes) for stream in [*booked, *pending])
    rows = camt053_to_dict(booked[0])
    assert rows[0]["applicant_name"] == "Erfundene Beispiel GmbH"
    assert rows[0]["purpose"] == "Erfundene Rechnung 2026-001"

    transactions = client.get_transactions(account)
    assert isinstance(transactions, list)
    assert len(transactions) == 2
    assert all(isinstance(tx, mt940.models.Transaction) for tx in transactions)
    assert transactions[0].data["amount"].amount == Decimal("-42.00")


def test_happy_path_log_contains_read_only_operations() -> None:
    """D2, D8: das Methodenprotokoll beweist ausschliesslich lesende Aufrufe."""
    client = _client("happy_path")
    account = _account(client)
    client.get_balance(account)
    client.get_transactions_xml(account)
    client.get_transactions(account)

    log = client.method_log
    assert set(log) == fints_fake.FAKE_READ_OPERATIONS
    assert fints_fake.WRITE_OPERATIONS.isdisjoint(log)
    assert all(operation in READ_ONLY_OPERATIONS for operation in log)


def test_tan_text_flow_counts_pause_resume_deconstruct() -> None:
    """F-SYN-04, D3: NeedTANResponse, Pause, Resume, TAN und gezaehlte Aufrufe."""
    client = _client("tan_text")
    account = _account(client)

    challenge = client.get_balance(account)
    assert isinstance(challenge, NeedTANResponse)
    assert challenge.decoupled is False
    assert challenge.challenge == "Bitte TAN eingeben: 123456"
    assert challenge.resume_method == "send_tan"

    dialog_data = client.pause_dialog()
    assert client.pause_count == 1
    assert dialog_data.startswith(b"fake-dialog-state-")
    assert fints_fake.FAKE_PIN.encode() not in dialog_data

    with client.resume_dialog(dialog_data):
        assert client.resume_count == 1
        result = client.send_tan(challenge, "123456")

    assert isinstance(result, mt940.models.Balance)
    assert client.send_tan_count == 1
    assert client.deconstruct_count == 0
    blob = client.deconstruct()
    assert client.deconstruct_count == 1
    assert fints_fake.FAKE_PIN.encode() not in blob


def test_app_approval_flow_with_two_challenges() -> None:
    """F-SYN-04, D3: mehrere App-Freigaben, dann Erfolg; keine TAN noetig."""
    client = _client("tan_app")
    account = _account(client)

    first = client.get_balance(account)
    assert isinstance(first, NeedTANResponse)
    assert first.decoupled is True
    assert "1/2" in first.challenge

    with client.resume_dialog(client.pause_dialog()):
        second = client.send_tan(first, "")
    assert isinstance(second, NeedTANResponse)
    assert second.decoupled is True
    assert "2/2" in second.challenge

    with client.resume_dialog(client.pause_dialog()):
        result = client.send_tan(second, "")
    assert isinstance(result, mt940.models.Balance)
    assert client.pause_count == 2
    assert client.resume_count == 2
    assert client.send_tan_count == 2


def test_ing_90_days_uses_app_approval() -> None:
    """F-SYN-05: ING 90-Tage-Regel als App-Freigabe vor dem Saldo."""
    client = _client("ing_90_days")
    account = _account(client)

    challenge = client.get_balance(account)
    assert isinstance(challenge, NeedTANResponse)
    assert challenge.decoupled is True
    assert "ING" in challenge.challenge
    assert "90" in challenge.challenge

    with client.resume_dialog(client.pause_dialog()):
        result = client.send_tan(challenge, "")
    assert isinstance(result, mt940.models.Balance)


@pytest.mark.parametrize(
    ("script_name", "exception_type", "code", "pin_blocked"),
    [
        ("consors_9800", FinTSClientPINError, "9800", True),
        ("consors_9010", FinTSClientError, "9010", False),
        ("wrong_pin", FinTSClientPINError, "9942", True),
        ("locked", FinTSClientTemporaryAuthError, "3938", True),
    ],
)
def test_bank_error_scripts(
    script_name: str,
    exception_type: type[FinTSClientError],
    code: str,
    pin_blocked: bool,
) -> None:
    """F-SYN-05: Bankcodes 9800, 9010, falsche PIN und gesperrter Zugang."""
    client = _client(script_name)
    account = _account(client)

    with pytest.raises(exception_type) as excinfo:
        client.get_balance(account)

    error = excinfo.value
    assert getattr(error, "bank_code") == code
    assert getattr(error, "original_text").startswith(code)
    assert client.pin.blocked is pin_blocked
    assert "00000" not in str(error)
    assert set(client.method_log) == {"get_sepa_accounts", "get_balance"}


def test_balance_without_transactions() -> None:
    """F-SYN-03: Saldo ohne Umsaetze liefert leere Listen, keinen Fehler."""
    client = _client("balance_only")
    account = _account(client)

    balance = client.get_balance(account)
    assert balance.amount.amount == Decimal("0.00")
    assert client.get_transactions_xml(account) == ([], [])
    assert client.get_transactions(account) == []


def test_camt_delivery_separates_booked_and_pending() -> None:
    """F-SYN-03, D2: camt liefert gebuchte und vorgemerkte Streams getrennt."""
    client = _client("camt_only")
    account = _account(client)

    booked, pending = client.get_transactions_xml(account)
    assert len(booked) == 1
    assert len(pending) == 1

    booked_row = camt053_to_dict(booked[0])[0]
    pending_row = camt053_to_dict(pending[0])[0]
    assert booked_row["amount"].amount == Decimal("-10.00")
    assert pending_row["amount"].amount == Decimal("-5.00")
    assert booked_row["applicant_name"] == "Erfundene camt GmbH"
    assert pending_row["purpose"] == "Erfundene camt-Vormerkung"
    assert booked_row["date"] == date(2026, 11, 20)


def test_mt940_pending_only_with_flag() -> None:
    """F-SYN-03, D2: MT940-Vormerkungen nur mit ``include_pending=True``."""
    client = _client("mt940_pending")
    account = _account(client)

    booked_only = client.get_transactions(account)
    assert len(booked_only) == 1
    assert "entry_date" in booked_only[0].data

    with_pending = _client("mt940_pending")
    account_with_pending = _account(with_pending)
    transactions = with_pending.get_transactions(account_with_pending, include_pending=True)
    assert len(transactions) == 2
    pending = transactions[-1]
    assert pending.data["purpose"] == "Erfundene MT940-Vormerkung"
    assert "entry_date" not in pending.data
    assert pending.data["amount"].amount == Decimal("-7.00")


@pytest.mark.parametrize(
    ("script_name", "state", "expected_orders"),
    [
        ("orders_present", "available", 2),
        ("orders_empty", "empty", 0),
        ("orders_unsupported", "unsupported", 0),
    ],
)
def test_orders_script_option(script_name: str, state: str, expected_orders: int) -> None:
    """F-AUF-05: Auftragsbestand nur als Skript-Option (Abruf folgt in T-075)."""
    client = _client(script_name)
    assert client.orders_state == state
    assert len(client.orders) == expected_orders
    if state == "available":
        assert client.orders[0].kind == "dauerauftrag"
        assert client.orders[0].counterparty == "Erfundene Vermietung GmbH"


def test_orders_script_rejects_contradictory_states() -> None:
    """F-AUF-05: Zustand und Auftragsliste duerfen sich nicht widersprechen."""
    accounts = fints_fake.fake_accounts()
    with pytest.raises(ValueError):
        fints_fake.FakeScript(name="leer", accounts=accounts, orders_state="available")
    order = fints_fake.FakeStandingOrder(
        kind="dauerauftrag",
        counterparty="Erfundene Beispiel GmbH",
        counterparty_iban=fints_fake.FAKE_IBAN_CARD,
        purpose="Erfundene Miete",
        amount="1,00",
    )
    with pytest.raises(ValueError):
        fints_fake.FakeScript(name="widerspruch", accounts=accounts, orders_state="empty", orders=(order,))


def test_deconstruct_restores_script_without_secrets() -> None:
    """N-SEC-02/03, D3: deconstruct/from_data ohne PIN oder Kontodaten."""
    client = _client("happy_path")
    blob = client.deconstruct()
    assert client.deconstruct_count == 1
    assert fints_fake.FAKE_PIN.encode() not in blob
    assert b"DE00" not in blob
    assert b"transfer" not in blob.lower()

    restored = FakeClient(from_data=blob)
    assert restored.script.name == "happy_path"
    balance = restored.get_balance(_account(restored))
    assert isinstance(balance, mt940.models.Balance)

    with pytest.raises(ValueError):
        FakeClient(from_data=b"kaputt")
    with pytest.raises(ValueError):
        FakeClient(from_data=blob, script=fints_fake.script_happy_path())
    assert fints_fake.FAKE_PIN not in repr(client)


def test_dialog_lifecycle_errors() -> None:
    """D3: Pause ohne offenen TAN-Dialog und Resume ohne Pause sind Fehler."""
    client = _client("balance_only")
    with pytest.raises(RuntimeError):
        client.pause_dialog()

    account = _account(client)
    client.get_balance(account)
    with pytest.raises(RuntimeError):
        client.pause_dialog()

    with pytest.raises(RuntimeError):
        with client.resume_dialog(b"fake-dialog-state-1"):
            pass
    with pytest.raises(ValueError):
        with client.resume_dialog(b"falsch"):
            pass


def test_script_exhaustion_and_unknown_account() -> None:
    """D8: ein erschoepftes Skript und ein fremdes Konto schlagen kontrolliert fehl."""
    client = _client("balance_only")
    account = _account(client)
    client.get_balance(account)
    with pytest.raises(AssertionError):
        client.get_balance(account)

    foreign = SEPAAccount(
        iban="DE00111122223333444455",
        bic=fints_fake.FAKE_BIC,
        accountnumber="1111111111",
        subaccount="",
        blz="11111111",
    )
    with pytest.raises(ValueError):
        client.get_balance(foreign)


@pytest.mark.parametrize("script_name", sorted(fints_fake.SCRIPT_FACTORIES))
def test_every_script_stays_read_only(script_name: str) -> None:
    """D2, D8: jedes Skript ruft nur lesende Operationen auf."""
    client = _client(script_name)
    account = _account(client)
    try:
        client.get_balance(account)
    except FinTSClientError:
        pass

    allowed = fints_fake.FAKE_READ_OPERATIONS | fints_fake.DIALOG_OPERATIONS
    assert set(client.method_log) <= allowed
    assert fints_fake.WRITE_OPERATIONS.isdisjoint(client.method_log)
    assert all(operation in READ_ONLY_OPERATIONS for operation in client.method_log if operation.startswith("get_"))


def test_no_network_and_only_invented_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """D8, N-SEC-05: kein Netzwerk und ausschliesslich erfundene Testdaten."""
    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Der Fake darf keine Netzwerkverbindung oeffnen")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    client = _client("happy_path")
    accounts = client.get_sepa_accounts()
    assert all(account.iban.startswith("DE00") for account in accounts)
    balance = client.get_balance(accounts[0])
    assert isinstance(balance, mt940.models.Balance)
    assert "00000" not in repr(client)
    assert all(operation not in fints_fake.WRITE_OPERATIONS for operation in client.method_log)
