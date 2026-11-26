"""Lesender Abruf von Dauer- und Terminauftraegen ueber python-fints (D2).

Die Erweiterung ergaenzt python-fints 5.0.1 um die rein lesenden Segmente
HKCDB (Dauerauftraege anfordern) und HKCSB (Terminauftraege anfordern) sowie
die zugehoerigen Antwort- und Parametersegmente HICDB/HICSB/HICDBS/HICSBS.
Sie baut ausschliesslich lesende Anfragesegmente und ruft nie einen
schreibenden Geschaeftsvorfall auf (F-AUF-03, F-AUF-07, D2).

Abbildung der Anforderungen:

- F-AUF-02: Antworten werden in :class:`FetchedStandingOrder` mit Empfaenger,
  IBAN, Verwendungszweck, Betrag in ganzen Cent, Art, Intervall sowie ersten,
  naechsten und letzten Ausfuehrungsterminen abgebildet.
- F-AUF-04: Jeder Aufruf liest den aktuellen Auftragsbestand der Bank; die
  Persistenz ersetzt ihn spaeter vollstaendig.
- F-AUF-05: :func:`build_orders_result` unterscheidet ``available``, ``empty``
  und ``unsupported`` ohne Fehler, wenn die Bank keinen Auftragsabruf anbietet
  oder keine Auftraege liefert.
- F-AUF-07: Dauer- und Terminauftraege sind nur lesend; es existiert kein
  Segment zum Anlegen, Aendern, Aussetzen oder Loeschen.
- N-DB-02: Betraege werden als :class:`decimal.Decimal` verarbeitet und als
  ganze Cent ausgegeben; Bruchteile eines Cents werden abgelehnt.
- N-SEC-02: Logmeldungen enthalten keine Kontodaten, Empfaenger, Betraege
  oder Banktexte, sondern nur Anzahl und Zustand.

Die Feldbelegung folgt der FinTS-Protokollvorlage (hbci-300.xml, Stand
2026-11) und dem Umsetzungsweg aus T-060. Die Segmente werden lokal
definiert, weil python-fints 5.0.1 HKCDB/HKCSB nicht mitbringt.
"""

from __future__ import annotations

import calendar
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol, cast

# python-fints liefert keine Typstubs (D2); die Klassen werden zur Laufzeit
# verwendet und ueber die Protokolle unten gekapselt.
from fints.client import NeedTANResponse  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
from fints.exceptions import FinTSUnsupportedOperation  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
from fints.fields import (  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
    CodeField,
    DataElementField,
    DataElementGroupField,
    DateField,
)
from fints.formals import (  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
    Amount1,
    DataElementGroup,
    KTI1,
    SupportedSEPAPainMessages1,
)
from fints.models import SEPAAccount  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
from fints.segments.base import (  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
    FinTS3Segment,
    ParameterSegment,
)
from fints.utils import RepresentableEnum  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)

from finue.services.fints_mapping import FinTSMappingError
from finue.services.fints_types import (
    FetchedStandingOrder,
    OrderKind,
    OrdersResult,
)

__all__ = [
    "HKCDB1",
    "HKCSB1",
    "HICDB1",
    "HICDBS1",
    "HICSB1",
    "HICSBS1",
    "OrderFetch",
    "OrderFetchClient",
    "ParsedTransfer",
    "StandingOrderDetails3",
    "StandingOrderSuspension3",
    "StandingOrderTimeUnit1",
    "VirtualDate",
    "VirtualDateField",
    "build_orders_result",
    "fetch_order_segments",
    "orders_from_segments",
    "parse_pain_transfer",
]

_logger = logging.getLogger(__name__)

#: Standard-Pain-Format, wenn die Bank keinen HISPAS-Parameter liefert.
DEFAULT_PAIN_DESCRIPTORS: Final[tuple[str, ...]] = ("urn:iso:std:iso:20022:tech:xsd:pain.001.001.03",)


class OrderFetchClient(Protocol):
    """Ausschnitt des python-fints-Clients fuer die lesenden Auftragssegmente.

    Die internen Helfer von python-fints liefern absichtlich ``Any``; der
    Rueckgabewert wird zur Laufzeit geprueft. ``_get_dialog`` liefert bei einem
    bereits geoeffneten Client den laufenden Dialog, sodass eine TAN-Pause den
    Dialog nicht schliesst (D3).
    """

    bpd: object

    def _get_dialog(self) -> AbstractContextManager[object]: ...

    def _find_highest_supported_command(self, *segment_classes: type[object]) -> Any: ...

    def _fetch_with_touchdowns(
        self,
        dialog: object,
        segment_factory: Callable[[str | None], object],
        response_processor: Callable[[object], object],
        *args: object,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class VirtualDate:
    """Datum aus einem FinTS-``vdat``-Feld (Jahr, Monat, Tag).

    Das virtuelle Datum erlaubt Angaben wie ``31.04.`` und bedeutet dann den
    letzten Tag des Monats. :meth:`to_date` bildet das auf ein echtes
    :class:`datetime.date` ab.
    """

    year: int
    month: int
    day: int

    def to_date(self) -> date | None:
        """Wandelt das virtuelle Datum in ein echtes Datum um.

        Ungueltige Monate liefern ``None``. Ein zu grosser Tag wird auf den
        letzten Tag des Monats begrenzt; das entspricht der Bedeutung von
        ``vdat`` in FinTS.
        """
        if not 1 <= self.month <= 12:
            return None
        last_day = calendar.monthrange(self.year, self.month)[1]
        if self.day < 1:
            return None
        return date(self.year, self.month, min(self.day, last_day))


class VirtualDateField(DateField):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """FinTS-Feldtyp ``vdat`` fuer :class:`VirtualDate`."""

    type = "vdat"
    _DOC_TYPE = VirtualDate

    def _parse_value(self, value: object) -> VirtualDate:
        if isinstance(value, VirtualDate):
            return value
        if isinstance(value, date):
            return VirtualDate(value.year, value.month, value.day)
        raw = str(value).strip()
        if len(raw) != 8 or not raw.isdigit():
            raise ValueError("Ungueltiges virtuelles Datum")
        return VirtualDate(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))

    def _render_value(self, value: VirtualDate) -> str:
        return f"{value.year:04d}{value.month:02d}{value.day:02d}"


class StandingOrderTimeUnit1(RepresentableEnum):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Zeiteinheit eines Dauerauftrags (M = Monat, W = Woche)."""

    MONTHLY = "M"
    WEEKLY = "W"


class StandingOrderDetails3(DataElementGroup):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Details eines Dauerauftrags (FinTS-DEG ``DauerDetails``, version 3)."""

    first_execution_date = VirtualDateField(_d="Erste Ausfuehrung")
    time_unit = CodeField(enum=StandingOrderTimeUnit1, _d="Zeiteinheit")
    interval = DataElementField(type="num", max_length=2, _d="Turnus")
    execution_day = DataElementField(type="num", max_length=2, _d="Ausfuehrungstag")
    last_execution_date = VirtualDateField(required=False, _d="Letzte Ausfuehrung")


class StandingOrderSuspension3(DataElementGroup):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Aussetzung eines Dauerauftrags (FinTS-DEG ``Aussetzung3``)."""

    annual = DataElementField(type="jn", _d="Jaehrliche Aussetzung")
    start_date = DateField(required=False, _d="Aussetzung ab")
    end_date = DateField(required=False, _d="Aussetzung bis")
    number = DataElementField(type="num", max_length=2, required=False, _d="Anzahl")
    new_value = DataElementGroupField(type=Amount1, required=False, _d="Neuer Betrag")


class HKCDB1(FinTS3Segment):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Dauerauftraege anfordern (lesend), version 1."""

    account = DataElementGroupField(type=KTI1, _d="Konto")
    supported_sepa_pain_messages = DataElementGroupField(
        type=SupportedSEPAPainMessages1, _d="Unterstuetzte pain-Formate"
    )
    task_id = DataElementField(type="an", max_length=99, required=False, _d="Auftragsidentifikation")
    max_number_responses = DataElementField(type="num", max_length=4, required=False, _d="Maximale Anzahl")
    touchdown_point = DataElementField(type="an", max_length=35, required=False, _d="Touchdown-Punkt")


class HICDB1(FinTS3Segment):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Dauerauftraege rueckmelden (lesend), version 1."""

    account = DataElementGroupField(type=KTI1, _d="Konto")
    sepa_descriptor = DataElementField(type="an", max_length=256, required=False, _d="SEPA-Format")
    sepa_pain_message = DataElementField(type="bin", _d="pain-Nachricht")
    task_id = DataElementField(type="an", max_length=99, required=False, _d="Auftragsidentifikation")
    standing_order_details = DataElementGroupField(type=StandingOrderDetails3, _d="Dauerauftragsdetails")
    suspension = DataElementGroupField(type=StandingOrderSuspension3, required=False, _d="Aussetzung")
    task_changeable = DataElementField(type="jn", required=False, _d="Auftrag aenderbar")
    task_suspendable = DataElementField(type="jn", required=False, _d="Auftrag aussetzbar")
    task_cancelable = DataElementField(type="jn", required=False, _d="Auftrag loeschbar")


class QueryStandingOrdersParameter1(DataElementGroup):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Parameter fuer den Dauerauftragsbestand (FinTS-DEG)."""

    maxentries_allowed = DataElementField(type="jn", _d="Maximale Anzahl erlaubt")


class HICDBS1(ParameterSegment):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Parameter Dauerauftraege anfordern, version 1."""

    parameter = DataElementGroupField(type=QueryStandingOrdersParameter1, _d="Parameter")


class HKCSB1(FinTS3Segment):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Terminauftraege anfordern (lesend), version 1."""

    account = DataElementGroupField(type=KTI1, _d="Konto")
    supported_sepa_pain_messages = DataElementGroupField(
        type=SupportedSEPAPainMessages1, _d="Unterstuetzte pain-Formate"
    )
    date_start = DateField(required=False, _d="Zeitraum ab")
    date_end = DateField(required=False, _d="Zeitraum bis")
    max_number_responses = DataElementField(type="num", max_length=4, required=False, _d="Maximale Anzahl")
    touchdown_point = DataElementField(type="an", max_length=35, required=False, _d="Touchdown-Punkt")


class HICSB1(FinTS3Segment):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Terminauftraege rueckmelden (lesend), version 1."""

    account = DataElementGroupField(type=KTI1, _d="Konto")
    sepa_descriptor = DataElementField(type="an", max_length=256, required=False, _d="SEPA-Format")
    sepa_pain_message = DataElementField(type="bin", _d="pain-Nachricht")
    task_id = DataElementField(type="an", max_length=99, required=False, _d="Auftragsidentifikation")
    task_cancelable = DataElementField(type="jn", required=False, _d="Auftrag loeschbar")
    task_changeable = DataElementField(type="jn", required=False, _d="Auftrag aenderbar")


class QueryScheduledTransfersParameter1(DataElementGroup):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Parameter fuer den Terminauftragsbestand (FinTS-DEG)."""

    canmaxentries = DataElementField(type="jn", _d="Maximale Anzahl erlaubt")
    cantimerange = DataElementField(type="jn", _d="Zeitraum erlaubt")


class HICSBS1(ParameterSegment):  # type: ignore[misc]  # python-fints liefert keine Typstubs (D2)
    """Parameter Terminauftraege anfordern, version 1."""

    parameter = DataElementGroupField(type=QueryScheduledTransfersParameter1, _d="Parameter")


@dataclass(frozen=True, slots=True)
class OrderFetch:
    """Ergebnis eines einzelnen Auftragsabrufs je Art.

    ``supported`` ist ``False``, wenn die Bank das Segment nicht anbietet.
    ``tan`` traegt eine TAN-Aufforderung; in diesem Fall bleibt ``segments``
    leer, bis der Dialog fortgesetzt wurde.
    """

    kind: OrderKind
    supported: bool
    segments: tuple[object, ...] = ()
    tan: object | None = None


@dataclass(frozen=True, slots=True)
class ParsedTransfer:
    """Aus einer pain.001-Nachricht gelesene Auftragsdaten.

    Alle Felder sind optional, weil Banken auch unvollstaendige Nachrichten
    liefern. Fehlende Angaben werden nicht erfunden.
    """

    counterparty: str | None = None
    counterparty_iban: str | None = None
    purpose: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    execution_date: date | None = None


def fetch_order_segments(
    client: OrderFetchClient,
    account: SEPAAccount,
    kind: OrderKind,
) -> OrderFetch:
    """Fragt Dauer- oder Terminauftraege rein lesend an (F-AUF-04, F-AUF-07).

    Die Methode sendet genau ein HKCDB- bzw. HKCSB-Segment und wertet die
    HICDB-/HICSB-Antworten aus. Fehlt die Unterstuetzung, liefert sie
    ``supported=False`` ohne Fehler. Verlangt die Bank eine TAN, wird die
    Aufforderung unveraendert zurueckgegeben; der Dialog bleibt fuer die
    Gateway-Pause offen.
    """
    request_class: type[object]
    response_type: str
    if kind == "dauerauftrag":
        request_class = HKCDB1
        response_type = "HICDB"
    else:
        request_class = HKCSB1
        response_type = "HICSB"

    descriptors = _pain_descriptors(client)
    with client._get_dialog() as dialog:
        try:
            command_class = client._find_highest_supported_command(request_class)
        except FinTSUnsupportedOperation:
            return OrderFetch(kind=kind, supported=False)

        if command_class is None:
            return OrderFetch(kind=kind, supported=False)

        def segment_factory(touchdown: str | None) -> object:
            return _build_request(command_class, account, kind, descriptors, touchdown)

        try:
            result = client._fetch_with_touchdowns(dialog, segment_factory, _identity, response_type)
        except FinTSUnsupportedOperation:
            return OrderFetch(kind=kind, supported=False)

    if isinstance(result, NeedTANResponse):
        return OrderFetch(kind=kind, supported=True, tan=result)
    return OrderFetch(kind=kind, supported=True, segments=_as_segments(result))


def orders_from_segments(
    segments: Sequence[object],
    kind: OrderKind,
    *,
    today: date | None = None,
) -> tuple[FetchedStandingOrder, ...]:
    """Bildet HICDB-/HICSB-Segmente auf Anwendungsauftraege ab (F-AUF-02).

    ``today`` ist der Bezugstag fuer die naechste Ausfuehrung eines
    Dauerauftrags. Er ist injizierbar, damit Tests deterministisch bleiben;
    ohne Angabe gilt das aktuelle Datum. Unbekannte Segmentversionen werden
    ohne Datenausgabe gezaehlt und uebersprungen, damit eine Bank mit neuerer
    Version den Sync nicht bricht.
    """
    reference = today if today is not None else date.today()
    orders: list[FetchedStandingOrder] = []
    ignored = 0
    for segment in segments:
        if kind == "dauerauftrag" and isinstance(segment, HICDB1):
            orders.append(_map_standing_order(segment, reference))
        elif kind == "terminauftrag" and isinstance(segment, HICSB1):
            orders.append(_map_scheduled_transfer(segment))
        else:
            ignored += 1
    if ignored:
        _logger.warning(
            "Auftragsantwort: %d Segment(e) ohne unterstuetzte Version ignoriert.",
            ignored,
        )
    return tuple(orders)


def build_orders_result(
    orders: Sequence[FetchedStandingOrder],
    *,
    any_supported: bool,
) -> OrdersResult:
    """Erzeugt den F-AUF-05-Zustand aus Auftraegen und Unterstuetzung.

    ``available`` nur mit mindestens einem Auftrag, sonst ``empty`` bei
    unterstuetztem Abruf und ``unsupported`` ohne Unterstuetzung.
    """
    if orders:
        return OrdersResult(status="available", orders=tuple(orders))
    if any_supported:
        return OrdersResult(status="empty")
    return OrdersResult(status="unsupported")


def parse_pain_transfer(value: object) -> ParsedTransfer:
    """Liest Empfaenger, IBAN, Zweck, Betrag und Ausfuehrung aus pain.001.

    Namespace-agnostisch und tolerant gegenueber fehlenden Feldern. Eine nicht
    lesbare Nachricht liefert leere Felder, damit eine Teilantwort den
    Auftragsabruf nicht abbricht (F-AUF-05).
    """
    raw = _pain_input(value)
    if raw is None:
        return ParsedTransfer()
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        _logger.warning("Auftragsantwort: pain.001 nicht lesbar; Auftrag bleibt teilweise.")
        return ParsedTransfer()

    amount_element = _find_first(root, "InstdAmt")
    if amount_element is None:
        amount_element = _find_first(root, "Amt")

    currency = _currency_code(amount_element)
    amount_cents: int | None = None
    amount_text = _element_text(amount_element)
    if amount_text is not None:
        try:
            amount_cents = _amount_to_cents(amount_text)
        except FinTSMappingError:
            _logger.warning("Auftragsantwort: Betrag ungueltig; Auftrag bleibt teilweise.")

    creditor = _find_first(root, "Cdtr")
    counterparty = _find_text(creditor, "Nm")
    counterparty_iban = _find_text(_find_first(root, "CdtrAcct"), "IBAN")

    purposes = _find_all_texts(root, "Ustrd")
    purpose = "\n".join(purposes) if purposes else None

    return ParsedTransfer(
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        amount_cents=amount_cents,
        currency=currency,
        execution_date=_payment_execution_date(root),
    )


def _build_request(
    command_class: Any,
    account: SEPAAccount,
    kind: OrderKind,
    descriptors: Sequence[str],
    touchdown: str | None,
) -> object:
    """Baut das lesende Anfragesegment mit den Feldern der Protokollvorlage."""
    account_field = command_class._fields["account"]
    account_value = account_field.type.from_sepa_account(account)
    pain_value = SupportedSEPAPainMessages1(sepa_descriptors=list(descriptors))
    if kind == "dauerauftrag":
        return command_class(
            account=account_value,
            supported_sepa_pain_messages=pain_value,
            task_id=None,
            max_number_responses=None,
            touchdown_point=touchdown,
        )
    return command_class(
        account=account_value,
        supported_sepa_pain_messages=pain_value,
        date_start=None,
        date_end=None,
        max_number_responses=None,
        touchdown_point=touchdown,
    )


def _pain_descriptors(client: OrderFetchClient) -> tuple[str, ...]:
    """Liest die pain-Formate aus HISPAS, sonst gilt der Standard (D2)."""
    bpd = getattr(client, "bpd", None)
    finder = getattr(bpd, "find_segment_first", None)
    if callable(finder):
        hispas = finder("HISPAS")
        parameter = getattr(hispas, "parameter", None)
        formats = getattr(parameter, "supported_sepa_formats", None)
        if formats:
            if isinstance(formats, str):
                descriptors: tuple[str, ...] = (formats,) if formats.strip() else ()
            else:
                descriptors = tuple(str(item) for item in formats if str(item).strip())
            if descriptors:
                return descriptors
    return DEFAULT_PAIN_DESCRIPTORS


def _map_standing_order(segment: object, today: date) -> FetchedStandingOrder:
    """Bildet ein HICDB-Segment auf einen Dauerauftrag ab (F-AUF-02)."""
    parsed = parse_pain_transfer(getattr(segment, "sepa_pain_message", None))
    details = getattr(segment, "standing_order_details", None)
    first_execution = _virtual_date(getattr(details, "first_execution_date", None))
    last_execution = _virtual_date(getattr(details, "last_execution_date", None))
    return FetchedStandingOrder(
        kind="dauerauftrag",
        counterparty=parsed.counterparty,
        counterparty_iban=parsed.counterparty_iban,
        purpose=parsed.purpose,
        amount_cents=parsed.amount_cents,
        currency=parsed.currency,
        interval=_format_interval(details),
        next_execution=_next_execution(details, first_execution, last_execution, today),
        first_execution=first_execution,
        last_execution=last_execution,
    )


def _map_scheduled_transfer(segment: object) -> FetchedStandingOrder:
    """Bildet ein HICSB-Segment auf einen Terminauftrag ab (F-AUF-02)."""
    parsed = parse_pain_transfer(getattr(segment, "sepa_pain_message", None))
    execution = parsed.execution_date
    return FetchedStandingOrder(
        kind="terminauftrag",
        counterparty=parsed.counterparty,
        counterparty_iban=parsed.counterparty_iban,
        purpose=parsed.purpose,
        amount_cents=parsed.amount_cents,
        currency=parsed.currency,
        interval=None,
        next_execution=execution,
        first_execution=execution,
        last_execution=execution,
    )


def _format_interval(details: object) -> str | None:
    """Formuliert das Intervall eines Dauerauftrags in Klartext (F-AUF-02)."""
    if details is None:
        return None
    unit = getattr(details, "time_unit", None)
    interval = getattr(details, "interval", None)
    if isinstance(unit, StandingOrderTimeUnit1):
        unit_value = unit.value
    elif isinstance(unit, str):
        unit_value = unit
    else:
        return None
    if not isinstance(interval, int) or interval < 1:
        return None
    if unit_value == "M":
        if interval == 1:
            return "monatlich"
        if interval == 3:
            return "quartalsweise"
        if interval == 6:
            return "halbjaehrlich"
        if interval == 12:
            return "jaehrlich"
        return f"alle {interval} Monate"
    if unit_value == "W":
        if interval == 1:
            return "woechentlich"
        return f"alle {interval} Wochen"
    return None


def _next_execution(
    details: object,
    first_execution: date | None,
    last_execution: date | None,
    today: date,
) -> date | None:
    """Leitet die naechste Ausfuehrung eines Dauerauftrags ab (F-AUF-02).

    Die Bank liefert fuer Dauerauftraege erste und letzte Ausfuehrung, aber
    keinen naechsten Termin. Aus Zeiteinheit, Turnus und Ausfuehrungstag wird
    der erste Termin am oder nach ``today`` berechnet. Ein befristeter Auftrag
    ohne zukuenftigen Termin liefert ``None``.
    """
    if first_execution is None:
        return None
    unit = getattr(details, "time_unit", None)
    interval = getattr(details, "interval", None)
    if isinstance(unit, str):
        unit_value = unit
    elif isinstance(unit, StandingOrderTimeUnit1):
        unit_value = unit.value
    else:
        return None
    if not isinstance(interval, int) or interval < 1:
        return None

    if first_execution >= today:
        return _bounded(first_execution, last_execution)

    if unit_value == "M":
        execution_day = getattr(details, "execution_day", None)
        day = execution_day if isinstance(execution_day, int) and execution_day >= 1 else first_execution.day
        current = first_execution
        for _ in range(2400):
            year, month = _add_months(current.year, current.month, interval)
            day_in_month = min(day, calendar.monthrange(year, month)[1])
            current = date(year, month, day_in_month)
            if last_execution is not None and current > last_execution:
                return None
            if current >= today:
                return current
        return None

    if unit_value == "W":
        step_days = 7 * interval
        elapsed = (today - first_execution).days
        jumps = max(0, elapsed // step_days)
        current = first_execution + timedelta(days=jumps * step_days)
        while current < today:
            current += timedelta(days=step_days)
        return _bounded(current, last_execution)

    return None


def _bounded(candidate: date, last_execution: date | None) -> date | None:
    """Liefert ``candidate`` nur, wenn der Auftrag dann noch laeuft."""
    if last_execution is not None and candidate > last_execution:
        return None
    return candidate


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    """Addiert Monate ohne Tagesarithmetik."""
    zero_based = (year * 12 + (month - 1)) + months
    return zero_based // 12, zero_based % 12 + 1


def _virtual_date(value: object) -> date | None:
    """Wandelt ein VirtualDate oder date in ein echtes Datum um."""
    if isinstance(value, VirtualDate):
        return value.to_date()
    if isinstance(value, date):
        return value
    return None


def _identity(value: object) -> object:
    """Rueckgabeprozessor fuer :meth:`_fetch_with_touchdowns`."""
    return value


def _as_segments(value: object) -> tuple[object, ...]:
    """Prueft die Segmentliste einer python-fints-Antwort."""
    if isinstance(value, (list, tuple)):
        return tuple(cast(Sequence[object], value))
    raise FinTSMappingError("Auftragsabruf lieferte keine Segmentliste.")


def _pain_input(value: object) -> bytes | str | None:
    """Normalisiert die pain-Nachricht aus einem Segmentfeld."""
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, (bytes, str)):
        return value if value else None
    return None


def _amount_to_cents(text: str) -> int:
    """Rechnet einen Dezimaltext in ganze Cent um (N-DB-02)."""
    normalized = text.strip().replace(",", ".")
    if not normalized:
        raise FinTSMappingError("Betrag ist leer")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise FinTSMappingError("Betrag ist kein Zahlenwert") from exc
    if not amount.is_finite():
        raise FinTSMappingError("Betrag ist kein endlicher Zahlenwert")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise FinTSMappingError("Betrag hat mehr als zwei Nachkommastellen")
    return int(cents)


def _currency_code(element: ET.Element | None) -> str | None:
    """Liest den Waehrungscode aus dem Betragselement."""
    if element is None:
        return None
    raw = element.get("Ccy") or element.get("ccy")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().upper()
    if len(normalized) == 3 and normalized.isalpha():
        return normalized
    return None


def _payment_execution_date(root: ET.Element) -> date | None:
    """Liest das angeforderte Ausfuehrungsdatum aus pain.001."""
    for node in root.iter():
        if _local_name(node.tag) != "ReqdExctnDt":
            continue
        text = _element_text(node)
        if text is None:
            for child in node:
                if _local_name(child.tag) in {"Dt", "DtTm"}:
                    text = _element_text(child)
                    break
        parsed = _parse_date_text(text)
        if parsed is not None:
            return parsed
    return None


def _parse_date_text(text: str | None) -> date | None:
    """Liest ein ISO- oder FinTS-Datum ohne Ausnahmen."""
    if text is None:
        return None
    value = text.strip()
    if "T" in value:
        value = value.split("T", 1)[0]
    try:
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
        if len(value) == 8 and value.isdigit():
            return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None
    return None


def _find_first(element: ET.Element | None, name: str) -> ET.Element | None:
    """Findet das erste Nachkommenelement mit lokalem Namen ``name``."""
    if element is None:
        return None
    for node in element.iter():
        if _local_name(node.tag) == name:
            return node
    return None


def _find_text(element: ET.Element | None, name: str) -> str | None:
    """Liest den Text des ersten passenden Nachkommenelements."""
    return _element_text(_find_first(element, name))


def _find_all_texts(element: ET.Element, name: str) -> list[str]:
    """Liest alle nicht leeren Texte mit lokalem Namen ``name``."""
    texts: list[str] = []
    for node in element.iter():
        if _local_name(node.tag) != name:
            continue
        text = _element_text(node)
        if text is not None:
            texts.append(text)
    return texts


def _element_text(element: ET.Element | None) -> str | None:
    """Liefert den getrimmten Text oder ``None``."""
    if element is None:
        return None
    text = (element.text or "").strip()
    return text or None


def _local_name(tag: object) -> str:
    """Entfernt den XML-Namespace von einem Tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]
