"""Tests fuer den lesenden Dauer-/Terminauftragsabruf (T-075).

Referenziert F-AUF-02 (Anzeigefelder), F-AUF-04 (Bestand je Abruf), F-AUF-05
(available/empty/unsupported ohne Fehler), F-AUF-07 (nur lesende Segmente),
F-SYN-04/D3 (TAN-Pause und Fortsetzung), N-DB-02 (Betrag in Cent) und D2
(python-fints, keine schreibenden Geschaeftsvorfaelle). Alle Bankdaten sind
erfunden; es findet kein Bankzugriff und kein Netzverkehr statt (D8).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from finue.config import Settings
from finue.models.bank import Bank
from finue.services.fints_client import FinTSGateway, TanRequired
from finue.services.fints_orders import (
    ParsedTransfer,
    StandingOrderTimeUnit1,
    VirtualDate,
    build_orders_result,
    fetch_order_segments,
    orders_from_segments,
    parse_pain_transfer,
)
from finue.services.fints_types import FetchedStandingOrder, OrdersResult

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake_orders.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_orders", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake_orders.py konnte nicht geladen werden")
fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fake
_SPEC.loader.exec_module(fake)

ENDPOINT = "https://example.invalid/fints"


def _settings() -> Settings:
    return Settings(_env_file=None, fints_product_id=fake.FAKE_PRODUCT_ID)


def _bank() -> Bank:
    return Bank(
        blz=fake.FAKE_BANK_IDENTIFIER,
        name="Erfundene Testbank",
        fints_url=ENDPOINT,
    )


def _orders_gateway(script_name: str) -> tuple[FinTSGateway, Any]:
    factory = fake.FakeOrdersFactory(script_name)
    gateway = FinTSGateway(
        _bank(),
        login_name=fake.FAKE_USER_ID,
        pin=fake.FAKE_PIN,
        settings=_settings(),
        client_factory=factory,
    )
    return gateway, factory


def _fake_client(script_name: str) -> Any:
    return fake.FakeOrdersClient(script=fake.SCRIPT_FACTORIES[script_name]())


def test_virtual_date_clamps_invalid_day() -> None:
    """F-AUF-02: vdat 31.04. bedeutet den letzten Tag des Monats."""
    assert VirtualDate(2026, 4, 31).to_date() == date(2026, 4, 30)
    assert VirtualDate(2026, 2, 30).to_date() == date(2026, 2, 28)
    assert VirtualDate(2024, 2, 30).to_date() == date(2024, 2, 29)
    assert VirtualDate(2026, 13, 1).to_date() is None


def test_parse_pain_transfer_reads_complete_message() -> None:
    """F-AUF-02, N-DB-02: Empfaenger, IBAN, Zweck, Cent und Termin."""
    parsed = parse_pain_transfer(fake.pain_message())

    assert parsed.counterparty == fake.FAKE_COUNTERPARTY
    assert parsed.counterparty_iban == fake.FAKE_COUNTERPARTY_IBAN
    assert parsed.purpose == "Miete\nErfundene Wohnung"
    assert parsed.amount_cents == 75000
    assert parsed.currency == "EUR"
    assert parsed.execution_date == date(2026, 12, 1)


def test_parse_pain_transfer_keeps_partial_fields() -> None:
    """F-AUF-05: fehlende Felder werden nicht erfunden und brechen nicht ab."""
    parsed = parse_pain_transfer(fake.pain_message(counterparty=None, iban=None, amount=None))

    assert parsed.counterparty is None
    assert parsed.counterparty_iban is None
    assert parsed.amount_cents is None
    assert parsed.currency is None
    assert parsed.execution_date == date(2026, 12, 1)

    broken = parse_pain_transfer(b"<kein-xml")
    assert broken == ParsedTransfer()


def test_interval_is_rendered_in_german() -> None:
    """F-AUF-02: Intervall in Klartext."""
    cases = [
        (StandingOrderTimeUnit1.MONTHLY, 1, "monatlich"),
        (StandingOrderTimeUnit1.MONTHLY, 3, "quartalsweise"),
        (StandingOrderTimeUnit1.MONTHLY, 6, "halbjaehrlich"),
        (StandingOrderTimeUnit1.MONTHLY, 12, "jaehrlich"),
        (StandingOrderTimeUnit1.MONTHLY, 2, "alle 2 Monate"),
        (StandingOrderTimeUnit1.WEEKLY, 1, "woechentlich"),
        (StandingOrderTimeUnit1.WEEKLY, 2, "alle 2 Wochen"),
    ]
    for unit, interval, expected in cases:
        orders = orders_from_segments(
            [fake.dauer_segment(unit=unit, interval=interval)],
            "dauerauftrag",
        )
        assert orders[0].interval == expected


def test_dauerauftrag_maps_all_display_fields() -> None:
    """F-AUF-02, F-AUF-04: Dauerauftrag mit allen Anzeigefeldern."""
    orders = orders_from_segments(
        [fake.dauer_segment()],
        "dauerauftrag",
        today=date(2026, 6, 15),
    )

    assert len(orders) == 1
    order = orders[0]
    assert order == FetchedStandingOrder(
        kind="dauerauftrag",
        counterparty=fake.FAKE_COUNTERPARTY,
        counterparty_iban=fake.FAKE_COUNTERPARTY_IBAN,
        purpose="Miete\nErfundene Wohnung",
        amount_cents=75000,
        currency="EUR",
        interval="monatlich",
        next_execution=date(2026, 6, 30),
        first_execution=date(2026, 1, 31),
        last_execution=date(2027, 1, 31),
    )


def test_dauer_next_execution_is_derived() -> None:
    """F-AUF-02: naechste Ausfuehrung aus Turnus und Ausfuehrungstag."""
    monthly = orders_from_segments(
        [fake.dauer_segment(first=VirtualDate(2026, 1, 31))],
        "dauerauftrag",
        today=date(2026, 6, 15),
    )
    assert monthly[0].next_execution == date(2026, 6, 30)

    quarterly = orders_from_segments(
        [
            fake.dauer_segment(
                first=VirtualDate(2026, 1, 15),
                last=None,
                interval=3,
                execution_day=15,
            )
        ],
        "dauerauftrag",
        today=date(2026, 6, 15),
    )
    assert quarterly[0].next_execution == date(2026, 7, 15)

    weekly = orders_from_segments(
        [
            fake.dauer_segment(
                first=VirtualDate(2026, 1, 5),
                last=None,
                unit=StandingOrderTimeUnit1.WEEKLY,
                interval=2,
            )
        ],
        "dauerauftrag",
        today=date(2026, 1, 20),
    )
    assert weekly[0].next_execution == date(2026, 2, 2)

    expired = orders_from_segments(
        [
            fake.dauer_segment(
                first=VirtualDate(2026, 1, 31),
                last=VirtualDate(2026, 2, 28),
            )
        ],
        "dauerauftrag",
        today=date(2026, 6, 15),
    )
    assert expired[0].next_execution is None

    future = orders_from_segments(
        [fake.dauer_segment(first=VirtualDate(2026, 12, 1))],
        "dauerauftrag",
        today=date(2026, 6, 15),
    )
    assert future[0].next_execution == date(2026, 12, 1)


def test_terminauftrag_uses_requested_execution_date() -> None:
    """F-AUF-02: Terminauftrag traegt das angeforderte Ausfuehrungsdatum."""
    orders = orders_from_segments([fake.termin_segment()], "terminauftrag")

    assert len(orders) == 1
    order = orders[0]
    assert order.kind == "terminauftrag"
    assert order.interval is None
    assert order.next_execution == date(2026, 12, 1)
    assert order.first_execution == date(2026, 12, 1)
    assert order.last_execution == date(2026, 12, 1)


def test_partial_response_keeps_parseable_orders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-AUF-05: Teilantwort liefert die lesbaren Auftraege ohne Abbruch."""
    client = _fake_client("orders_partial")
    fetch = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "dauerauftrag")

    assert fetch.supported
    assert fetch.tan is None
    orders = orders_from_segments(fetch.segments, "dauerauftrag")
    assert len(orders) == 3

    full, partial, broken = orders
    assert full.amount_cents == 75000
    assert full.counterparty == fake.FAKE_COUNTERPARTY
    assert partial.amount_cents is None
    assert partial.counterparty is None
    assert partial.counterparty_iban is None
    assert partial.first_execution == date(2026, 1, 31)
    assert broken.amount_cents is None
    assert broken.first_execution == date(2026, 1, 31)
    assert "pain.001 nicht lesbar" in caplog.text

    result = build_orders_result(orders, any_supported=True)
    assert result.status == "available"
    assert len(result.orders) == 3


def test_orders_result_states() -> None:
    """F-AUF-05: available/empty/unsupported folgen der Definition."""
    order = orders_from_segments([fake.dauer_segment()], "dauerauftrag")

    assert build_orders_result(order, any_supported=True) == OrdersResult(status="available", orders=order)
    assert build_orders_result((), any_supported=True) == OrdersResult(status="empty")
    assert build_orders_result((), any_supported=False) == OrdersResult(status="unsupported")
    # Ein Auftrag bleibt auch dann available, wenn ein Teilabruf als
    # nicht unterstuetzt gemeldet wurde.
    assert build_orders_result(order, any_supported=False).status == "available"


def test_fetch_sends_only_read_segments() -> None:
    """F-AUF-07, D2: es werden genau HKCDB und HKCSB gesendet."""
    client = _fake_client("orders_present")

    dauer = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "dauerauftrag")
    termin = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "terminauftrag")

    assert dauer.supported and termin.supported
    assert client.request_log == ["HKCDB", "HKCSB"]
    assert client.requests["dauerauftrag"].header.type == "HKCDB"
    assert client.requests["dauerauftrag"].header.version == 1
    assert client.requests["terminauftrag"].header.type == "HKCSB"
    assert client.requests["terminauftrag"].header.version == 1
    descriptors = list(client.requests["dauerauftrag"].supported_sepa_pain_messages.sepa_descriptors)
    assert descriptors == [fake.FAKE_PAIN_DESCRIPTOR]


def test_unsupported_bank_sends_no_request() -> None:
    """F-AUF-05: ohne HKCDB/HKCSB bleibt der Abruf ohne Anfrage."""
    client = _fake_client("orders_unsupported")

    dauer = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "dauerauftrag")
    termin = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "terminauftrag")

    assert not dauer.supported
    assert not termin.supported
    assert client.request_log == []
    result = build_orders_result((), any_supported=False)
    assert result.status == "unsupported"


def test_unknown_response_version_is_ignored_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F-AUF-05: unbekannte Antwortversion bricht den Abruf nicht."""
    client = _fake_client("orders_unknown_version")
    fetch = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "dauerauftrag")

    assert fetch.supported
    assert orders_from_segments(fetch.segments, "dauerauftrag") == ()
    assert "ohne unterstuetzte Version ignoriert" in caplog.text


def test_tan_required_is_returned_without_segments() -> None:
    """F-SYN-04, D3: TAN-Aufforderung wird unveraendert weitergereicht."""
    client = _fake_client("orders_tan_dauer")
    fetch = fetch_order_segments(client, fake.FAKE_ORDER_ACCOUNT, "dauerauftrag")

    assert fetch.supported
    assert fetch.tan is not None
    assert fetch.segments == ()


def test_tan_mapper_keeps_prior_orders_and_resumes() -> None:
    """F-AUF-04, F-SYN-04, D3: Dauerauftraege bleiben bei Termin-TAN erhalten."""
    gateway, factory = _orders_gateway("orders_tan_required")

    with pytest.raises(TanRequired) as excinfo:
        with gateway:
            account = gateway.get_accounts()[0]
            gateway.fetch_standing_orders(account)

    handoff = excinfo.value.handoff
    assert handoff.challenge.kind == "text"
    assert "TAN" in handoff.challenge.challenge
    assert fake.FAKE_PIN.encode() not in handoff.dialog_state
    assert fake.FAKE_PIN.encode() not in handoff.client_state
    assert factory.client is not None
    assert factory.client.pause_count == 1
    assert factory.client.deconstruct_count == 1

    result = gateway.resume_tan(handoff, "123456")

    assert result.status == "available"
    assert [order.kind for order in result.orders] == [
        "dauerauftrag",
        "terminauftrag",
    ]
    assert factory.client.resume_count == 1
    assert factory.client.send_tan_count == 1


def test_one_sided_support_still_returns_available() -> None:
    """F-AUF-05: nur HKCDB unterstuetzt, trotzdem available."""
    gateway, factory = _orders_gateway("orders_one_sided")

    with gateway:
        account = gateway.get_accounts()[0]
        result = gateway.fetch_standing_orders(account)

    assert result.status == "available"
    assert [order.kind for order in result.orders] == ["dauerauftrag"]
    assert factory.client is not None
    assert factory.client.request_log == ["HKCDB"]
