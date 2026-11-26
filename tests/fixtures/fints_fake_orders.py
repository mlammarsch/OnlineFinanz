"""Erfundene FinTS-Antworten fuer Dauer- und Terminauftraege (D8).

Die Fixtures enthalten ausschliesslich erfundene Konten, Empfaenger und
Betraege. Sie bilden die rein lesenden Antwortsegmente HICDB/HICSB nach und
erlauben Tests ohne Bankzugriff oder Netzverbindung. Der Fake-Client liefert
die internen Helfer, die die lesende Erweiterung aus T-075 benoetigt, und
unterstuetzt eine TAN-Aufforderung mit Pause und Fortsetzung.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any
from xml.sax.saxutils import escape

from fints.client import NeedTANResponse
from fints.exceptions import FinTSUnsupportedOperation
from fints.models import SEPAAccount
from fints.segments.base import FinTS3Segment, SegmentHeader
from fints.types import SegmentSequence

from finue.services.fints_orders import (
    HICDB1,
    HICSB1,
    StandingOrderDetails3,
    StandingOrderTimeUnit1,
    VirtualDate,
)

FAKE_PIN = "erfundene-auftrag-pin"
FAKE_USER_ID = "erfundener-auftrag-nutzer"
FAKE_PRODUCT_ID = "ERFUNDEN-AUFTRAEGE"
FAKE_BANK_IDENTIFIER = "12030000"
FAKE_ORDER_IBAN = "DE02120300000000202051"
FAKE_ORDER_BIC = "BYLADEM1001"
FAKE_COUNTERPARTY_IBAN = "DE44500105175407324931"
FAKE_COUNTERPARTY = "Erfundener Vermieter"
FAKE_PAIN_DESCRIPTOR = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"

FAKE_ORDER_ACCOUNT = SEPAAccount(
    iban=FAKE_ORDER_IBAN,
    bic=FAKE_ORDER_BIC,
    accountnumber="202051",
    subaccount="",
    blz=FAKE_BANK_IDENTIFIER,
)


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    """Antwort einer Auftragsart: Segmente und optionale TAN-Aufforderung."""

    segments: tuple[object, ...] = ()
    tan_required: bool = False


@dataclass(frozen=True, slots=True)
class OrdersScript:
    """Erfundener Bankzustand fuer HKCDB/HKCSB."""

    supported: Mapping[str, bool]
    outcomes: Mapping[str, OrderOutcome]


def pain_message(
    *,
    counterparty: str | None = FAKE_COUNTERPARTY,
    iban: str | None = FAKE_COUNTERPARTY_IBAN,
    amount: str | None = "750.00",
    currency: str = "EUR",
    purposes: tuple[str, ...] = ("Miete", "Erfundene Wohnung"),
    execution_date: date | None = date(2026, 12, 1),
) -> bytes:
    """Baut eine erfundene pain.001-Nachricht mit optionalen Feldern."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<Document xmlns="{FAKE_PAIN_DESCRIPTOR}">',
        "<CstmrCdtTrfInitn>",
        "<PmtInf>",
    ]
    if execution_date is not None:
        parts.append(f"<ReqdExctnDt>{execution_date.isoformat()}</ReqdExctnDt>")
    parts.append("<CdtTrfTxInf>")
    parts.append("<PmtId><EndToEndId>ERFUNDEN-1</EndToEndId></PmtId>")
    if amount is not None:
        parts.append(f'<Amt><InstdAmt Ccy="{escape(currency)}">{escape(amount)}</InstdAmt></Amt>')
    if counterparty is not None:
        parts.append(f"<Cdtr><Nm>{escape(counterparty)}</Nm></Cdtr>")
    if iban is not None:
        parts.append(f"<CdtrAcct><Id><IBAN>{escape(iban)}</IBAN></Id></CdtrAcct>")
    if purposes:
        lines = "".join(f"<Ustrd>{escape(line)}</Ustrd>" for line in purposes)
        parts.append(f"<RmtInf>{lines}</RmtInf>")
    parts.extend(
        [
            "</CdtTrfTxInf>",
            "</PmtInf>",
            "</CstmrCdtTrfInitn>",
            "</Document>",
        ]
    )
    return "".join(parts).encode("utf-8")


def dauer_segment(
    *,
    pain: bytes | None = None,
    first: VirtualDate = VirtualDate(2026, 1, 31),
    last: VirtualDate | None = VirtualDate(2027, 1, 31),
    unit: StandingOrderTimeUnit1 = StandingOrderTimeUnit1.MONTHLY,
    interval: int = 1,
    execution_day: int = 31,
    task_id: str = "ERFUNDEN-DAUER-1",
) -> HICDB1:
    """Baut ein erfundenes HICDB-Segment."""
    details = StandingOrderDetails3(
        first_execution_date=first,
        time_unit=unit,
        interval=interval,
        execution_day=execution_day,
        last_execution_date=last,
    )
    return HICDB1(
        account=None,
        sepa_descriptor=FAKE_PAIN_DESCRIPTOR,
        sepa_pain_message=pain if pain is not None else pain_message(),
        task_id=task_id,
        standing_order_details=details,
        task_changeable=True,
        task_suspendable=True,
        task_cancelable=False,
    )


def termin_segment(
    *,
    pain: bytes | None = None,
    task_id: str = "ERFUNDEN-TERMIN-1",
) -> HICSB1:
    """Baut ein erfundenes HICSB-Segment."""
    return HICSB1(
        account=None,
        sepa_descriptor=FAKE_PAIN_DESCRIPTOR,
        sepa_pain_message=pain if pain is not None else pain_message(),
        task_id=task_id,
        task_cancelable=True,
        task_changeable=True,
    )


def _orders_present() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": True},
        outcomes={
            "dauerauftrag": OrderOutcome(segments=(dauer_segment(),)),
            "terminauftrag": OrderOutcome(segments=(termin_segment(),)),
        },
    )


def _orders_empty() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": True},
        outcomes={},
    )


def _orders_unsupported() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": False, "terminauftrag": False},
        outcomes={},
    )


def _orders_partial() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": False},
        outcomes={
            "dauerauftrag": OrderOutcome(
                segments=(
                    dauer_segment(task_id="ERFUNDEN-DAUER-VOLL"),
                    dauer_segment(
                        pain=pain_message(counterparty=None, iban=None, amount=None),
                        last=None,
                        task_id="ERFUNDEN-DAUER-TEIL",
                    ),
                    dauer_segment(
                        pain=b"<kein-xml",
                        last=None,
                        task_id="ERFUNDEN-DAUER-KAPUTT",
                    ),
                )
            ),
        },
    )


def _orders_tan_required() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": True},
        outcomes={
            "dauerauftrag": OrderOutcome(segments=(dauer_segment(),)),
            "terminauftrag": OrderOutcome(
                segments=(termin_segment(),), tan_required=True
            ),
        },
    )


def _orders_tan_dauer() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": True},
        outcomes={
            "dauerauftrag": OrderOutcome(
                segments=(dauer_segment(),), tan_required=True
            ),
            "terminauftrag": OrderOutcome(segments=(termin_segment(),)),
        },
    )


def _orders_one_sided() -> OrdersScript:
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": False},
        outcomes={"dauerauftrag": OrderOutcome(segments=(dauer_segment(),))},
    )


def _orders_unknown_version() -> OrdersScript:
    unknown = FinTS3Segment(header=SegmentHeader("HICDB", None, 2))
    return OrdersScript(
        supported={"dauerauftrag": True, "terminauftrag": False},
        outcomes={"dauerauftrag": OrderOutcome(segments=(unknown,))},
    )


SCRIPT_FACTORIES: dict[str, Callable[[], OrdersScript]] = {
    "orders_present": _orders_present,
    "orders_empty": _orders_empty,
    "orders_unsupported": _orders_unsupported,
    "orders_partial": _orders_partial,
    "orders_tan_required": _orders_tan_required,
    "orders_tan_dauer": _orders_tan_dauer,
    "orders_one_sided": _orders_one_sided,
    "orders_unknown_version": _orders_unknown_version,
}


class FakeOrdersClient:
    """Fake-Client mit den internen Helfern der lesenden Auftragssegmente."""

    def __init__(self, script: OrdersScript, **kwargs: Any) -> None:
        self.script = script
        self.accounts = list(kwargs.pop("accounts", (FAKE_ORDER_ACCOUNT,)))
        self.bpd = SegmentSequence([])
        self.method_log: list[str] = []
        self.request_log: list[str] = []
        self.requests: dict[str, Any] = {}
        self.pause_count = 0
        self.deconstruct_count = 0
        self.resume_count = 0
        self.send_tan_count = 0
        self._tan_done: set[str] = set()
        self._pending_segments: list[object] = []

    def __enter__(self) -> FakeOrdersClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(script={sorted(self.script.supported)!r})"

    def _get_dialog(self) -> Any:
        return nullcontext(self)

    def get_sepa_accounts(self) -> list[SEPAAccount]:
        self.method_log.append("get_sepa_accounts")
        return list(self.accounts)

    def pause_dialog(self) -> bytes:
        self.pause_count += 1
        return b"fake-order-dialog-state"

    def resume_dialog(self, dialog_data: bytes) -> Any:
        self.resume_count += 1
        return nullcontext()

    def deconstruct(self, including_private: bool = False) -> bytes:
        self.deconstruct_count += 1
        return b"fake-order-client-state"

    def send_tan(self, challenge: NeedTANResponse, tan: str | None = None) -> Any:
        self.send_tan_count += 1
        return list(self._pending_segments)

    def _find_highest_supported_command(self, *segment_classes: Any, **kwargs: Any) -> Any:
        kind = _kind_for_class(segment_classes[0])
        if not self.script.supported.get(kind, False):
            raise FinTSUnsupportedOperation(f"{segment_classes[0].TYPE} nicht unterstuetzt")
        return segment_classes[0]

    def _fetch_with_touchdowns(
        self,
        dialog: object,
        segment_factory: Callable[[str | None], object],
        response_processor: Callable[[object], object],
        *args: object,
        **kwargs: object,
    ) -> object:
        request = segment_factory(None)
        kind = _kind_for_class(type(request))
        self.request_log.append(request.header.type)
        self.requests[kind] = request
        outcome = self.script.outcomes.get(kind, OrderOutcome())
        if outcome.tan_required and kind not in self._tan_done:
            self._tan_done.add(kind)
            self._pending_segments = list(outcome.segments)
            tan_request = SimpleNamespace(
                challenge="Bitte TAN fuer den erfundenen Auftragsabruf eingeben.",
                challenge_hhduc=None,
            )
            return NeedTANResponse(request, tan_request)
        return response_processor(list(outcome.segments))


class FakeOrdersFactory:
    """Baut den Fake-Client und liefert ihn bei ``from_data`` zurueck."""

    def __init__(self, script_name: str) -> None:
        self._script_name = script_name
        self.client: FakeOrdersClient | None = None
        self.init_kwargs: list[dict[str, Any]] = []
        self.restore_states: list[bytes] = []

    def __call__(self, **kwargs: Any) -> FakeOrdersClient:
        if "from_data" in kwargs:
            self.restore_states.append(kwargs["from_data"])
            if self.client is not None:
                return self.client
        client = FakeOrdersClient(
            script=SCRIPT_FACTORIES[self._script_name](), **kwargs
        )
        self.init_kwargs.append(dict(kwargs))
        self.client = client
        return client


def _kind_for_class(segment_class: Any) -> str:
    """Ordnet HKCDB/HKCSB bzw. HICDB/HICSB der Auftragsart zu."""
    type_name = getattr(segment_class, "TYPE", None)
    return "dauerauftrag" if type_name in {"HKCDB", "HICDB"} else "terminauftrag"
