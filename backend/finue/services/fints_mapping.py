"""Abbildung von camt- und MT940-Umsaetzen auf persistierbare Buchungsdaten.

Die Funktionen uebersetzen ausschliesslich die bereits von python-fints
gelieferten Ergebnisse in die Anwendungs-Typen. Sie oeffnen keinen Dialog und
fuehren keinen Bankzugriff aus (D2, D8).

Abbildung der Anforderungen:

- F-SYN-03: camt.053 (gebucht) und camt.052 (vorgemerkt) werden getrennt
  eingelesen. MT940-Transaktionen aus ``get_transactions(include_pending=True)``
  werden als Buchungen uebernommen; der Status kommt aus dem Aufruf oder aus
  einem expliziten Vormerk-Kennzeichen.
- F-DUP-01: Die erzeugten :class:`~finue.services.duplicate.TransactionData`
  tragen alle Fingerprint-Felder (Buchungstag, Wertstellung, Betrag, Waehrung,
  Gegenpartei, IBAN, Verwendungszweck). Der Fingerprint selbst wird nicht hier,
  sondern in :mod:`finue.services.duplicate` berechnet.
- F-DUP-04: ``status`` trennt gebuchte und vorgemerkte Buchungen, damit die
  Sync-Persistenz Vormerkungen spaeter gezielt ersetzen kann.
- N-DB-02: Betraege werden als :class:`decimal.Decimal` verarbeitet und als
  ganze Cent ausgegeben. Float-Eingaben und Betraege mit mehr als zwei
  Nachkommastellen werden abgelehnt, damit keine stillen Rundungsfehler
  entstehen.

Die Parser sind namespace-agnostisch und benoetigen keine feste Dokumentwurzel.
Damit funktionieren camt.052 (``BkToCstmrDbtCdtNtfctn``) und camt.053
(``BkToCstmrStmt``) mit derselben Implementierung.

MT940 kennt kein eigenes Vormerk-Kennzeichen. ``map_mt940_transactions`` nimmt
deshalb den Status als Argument entgegen. Zusaetzlich gelten Transaktionen mit
``pending=True``, mit ``status`` ``PDNG``/``PENDING`` oder ohne Buchungstag
(``entry_date``) als vorgemerkt.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, Protocol

from finue.services.duplicate import TransactionData
from finue.services.fints_types import BalanceResult, TransactionStatus

__all__ = [
    "FinTSMappingError",
    "map_balance",
    "map_camt_transactions",
    "map_mt940_transactions",
]

# camt-Kennungen in der Kurz- und Langform.
_CREDIT_INDICATORS: Final[frozenset[str]] = frozenset({"CRDT", "C"})
_DEBIT_INDICATORS: Final[frozenset[str]] = frozenset({"DBIT", "D"})

# Werte, die "keine Referenz geliefert" bedeuten.
_EMPTY_REFERENCES: Final[frozenset[str]] = frozenset(
    {"", "NOTPROVIDED", "NONREF", "N/A", "NA"}
)

# Statuswerte, die python-fints/mt940 fuer Vormerkungen verwenden koennen.
_PENDING_MARKERS: Final[frozenset[str]] = frozenset({"PDNG", "PENDING"})


class FinTSMappingError(ValueError):
    """Die gelieferten FinTS-Daten lassen sich nicht eindeutig abbilden.

    Die Meldung nennt nur den betroffenen Sachverhalt und enthaelt weder
    Kontodaten noch Banktexte (N-SEC-02).
    """


class MT940TransactionLike(Protocol):
    """Minimalform einer python-fints-/mt940-Transaktion.

    Die Bibliothek liefert benannte Tupel mit einem ``data``-Dict; Tests
    duerfen stattdessen direkt ein Mapping uebergeben.
    """

    data: Mapping[str, object]


def map_camt_transactions(
    booked_streams: Iterable[bytes | bytearray | str],
    pending_streams: Iterable[bytes | bytearray | str] = (),
) -> list[TransactionData]:
    """Wandelt die camt-Streams aus ``get_transactions_xml`` in Buchungen um.

    Args:
        booked_streams: XML-Streams der gebuchten Umsaetze (camt.053).
        pending_streams: XML-Streams der vorgemerkten Umsaetze (camt.052).

    Returns:
        Gebuchte Buchungen zuerst, danach vorgemerkte; die Reihenfolge der
        Streams und Eintraege bleibt erhalten.

    Raises:
        FinTSMappingError: Ein Stream ist kein lesbares XML oder ein Eintrag
            enthaelt keinen verwertbaren Betrag, keine Waehrung oder kein
            Datum.
    """
    transactions: list[TransactionData] = []
    for stream in booked_streams:
        transactions.extend(_map_camt_stream(stream, default_status="gebucht"))
    for stream in pending_streams:
        transactions.extend(_map_camt_stream(stream, default_status="vorgemerkt"))
    return transactions


def map_mt940_transactions(
    transactions: Iterable[MT940TransactionLike | Mapping[str, object]],
    *,
    status: TransactionStatus = "gebucht",
) -> list[TransactionData]:
    """Wandelt MT940-Transaktionen in Buchungen um (Fallback, F-SYN-03).

    Args:
        transactions: Transaktionen aus ``get_transactions(..., include_pending=True)``
            oder gleichwertige ``data``-Mappings.
        status: Status fuer Transaktionen ohne eigenes Vormerk-Kennzeichen.

    Returns:
        Buchungen in der Reihenfolge der Lieferung.
    """
    if status not in {"gebucht", "vorgemerkt"}:
        raise FinTSMappingError("Unbekannter Buchungsstatus")
    return [
        _map_mt940_transaction(_transaction_data(transaction), default_status=status)
        for transaction in transactions
    ]


def map_balance(
    balance: object,
    *,
    fallback_currency: str | None = None,
) -> BalanceResult:
    """Wandelt einen mt940-Saldo aus ``get_balance`` in ein ``BalanceResult``.

    Args:
        balance: Saldo-Objekt mit ``status`` und ``amount``; ``amount`` ist
            ein ``mt940.models.Amount``, ``Decimal``, ``int`` oder Text.
        fallback_currency: Waehrung, falls das Saldo-Objekt keine liefert.

    Returns:
        Saldo in ganzen Cent; ``D`` (Debit) wird negativ, ``C`` (Credit)
        positiv.

    Raises:
        FinTSMappingError: Betrag oder Waehrung fehlen bzw. sind nicht
            eindeutig lesbar.
    """
    amount_value = getattr(balance, "amount", None)
    if amount_value is None:
        raise FinTSMappingError("Saldo ohne Betrag")
    amount_cents = _decimal_to_cents(_to_decimal(amount_value))

    status = getattr(balance, "status", None)
    if isinstance(status, str):
        normalized_status = status.strip().upper()
        if normalized_status in _DEBIT_INDICATORS:
            amount_cents = -abs(amount_cents)
        elif normalized_status in _CREDIT_INDICATORS:
            amount_cents = abs(amount_cents)

    currency = (
        _currency_of(amount_value)
        or _currency_of(balance)
        or _normalized_currency(fallback_currency)
    )
    if currency is None:
        raise FinTSMappingError("Saldo ohne Waehrung")

    return BalanceResult(
        amount_cents=amount_cents,
        currency=currency,
        as_of=_optional_date(getattr(balance, "date", None)),
    )


def _map_camt_stream(
    stream: bytes | bytearray | str,
    *,
    default_status: TransactionStatus,
) -> list[TransactionData]:
    """Liest einen camt-Stream und bildet alle enthaltenen Eintraege ab."""
    try:
        root = ET.fromstring(stream)
    except ET.ParseError as exc:
        raise FinTSMappingError("camt-XML ist nicht lesbar") from exc
    _strip_namespaces(root)
    return [
        _map_camt_entry(entry, default_status=default_status)
        for entry in root.iter("Ntry")
    ]


def _map_camt_entry(
    entry: ET.Element,
    *,
    default_status: TransactionStatus,
) -> TransactionData:
    """Bildet einen camt-``Ntry`` auf ``TransactionData`` ab."""
    tx_details = entry.findall("NtryDtls/TxDtls")
    if not tx_details:
        tx_details = entry.findall(".//TxDtls")

    amount_element = entry.find("Amt")
    indicator = _find_text(entry, "CdtDbtInd")
    for detail in tx_details:
        if amount_element is None:
            amount_element = detail.find("Amt")
        if indicator is None:
            indicator = _find_text(detail, "CdtDbtInd")
    if amount_element is None or not (amount_element.text or "").strip():
        raise FinTSMappingError("camt-Buchung ohne Betrag")
    if indicator is None or indicator.strip().upper() not in (
        _CREDIT_INDICATORS | _DEBIT_INDICATORS
    ):
        raise FinTSMappingError("camt-Buchung ohne gueltige Soll/Haben-Kennung")
    normalized_indicator = indicator.strip().upper()

    currency = _camt_currency(amount_element, tx_details)
    amount_cents = _decimal_to_cents(_parse_decimal(amount_element.text or ""))
    if normalized_indicator in _DEBIT_INDICATORS:
        amount_cents = -abs(amount_cents)
    else:
        amount_cents = abs(amount_cents)

    booking_date = _camt_date(entry, "BookgDt")
    value_date = _camt_date(entry, "ValDt")
    if booking_date is None:
        booking_date = value_date
    if booking_date is None:
        raise FinTSMappingError("camt-Buchung ohne Buchungs- oder Wertstellungsdatum")

    status_marker = _find_text(entry, "Sts")
    status: TransactionStatus = default_status
    if status_marker is not None:
        normalized_status = status_marker.strip().upper()
        if normalized_status.startswith("PDNG"):
            status = "vorgemerkt"
        elif normalized_status.startswith("BOOK"):
            status = "gebucht"

    party_side = "Dbtr" if normalized_indicator in _CREDIT_INDICATORS else "Cdtr"
    counterparty, counterparty_iban = _camt_party(tx_details, party_side)

    return TransactionData(
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=currency,
        origin="fints",
        value_date=value_date,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=_camt_purpose(entry, tx_details),
        status=status,
        external_ref=_camt_external_reference(entry, tx_details),
    )


def _camt_currency(
    amount_element: ET.Element,
    tx_details: list[ET.Element],
) -> str:
    """Liest die Waehrung aus dem Betrag oder einem Detail-Betrag."""
    currency = _normalized_currency(amount_element.get("Ccy"))
    if currency is not None:
        return currency
    for detail in tx_details:
        detail_amount = detail.find("Amt")
        if detail_amount is not None:
            currency = _normalized_currency(detail_amount.get("Ccy"))
            if currency is not None:
                return currency
    raise FinTSMappingError("camt-Buchung ohne Waehrung")


def _camt_party(
    tx_details: list[ET.Element],
    side: str,
) -> tuple[str | None, str | None]:
    """Liest Gegenpartei und IBAN der passenden Seite (Dbtr/Cdtr)."""
    for detail in tx_details:
        name = _find_text(detail, f"RltdPties/{side}/Nm") or _find_text(
            detail, f"RltdPties/{side}/Pty/Nm"
        )
        iban = _find_text(detail, f"RltdPties/{side}Acct/Id/IBAN")
        if name is not None or iban is not None:
            return name, iban
    return None, None


def _camt_purpose(entry: ET.Element, tx_details: list[ET.Element]) -> str | None:
    """Sammelt alle Verwendungszweck-Zeilen und normalisiert sie zeilenweise."""
    lines: list[str] = []
    for detail in tx_details:
        for element in detail.findall("RmtInf/Ustrd"):
            lines.extend(_clean_lines(element.text or ""))
    if not lines:
        additional = _find_text(entry, "AddtlNtryInf")
        if additional is not None:
            lines.extend(_clean_lines(additional))
    return "\n".join(lines) if lines else None


def _camt_external_reference(
    entry: ET.Element,
    tx_details: list[ET.Element],
) -> str | None:
    """Liefert die End-to-End-Referenz, sonst die Bankreferenz, falls vorhanden."""
    for detail in tx_details:
        reference = _find_text(detail, "Refs/EndToEndId")
        if _is_reference(reference):
            return _normalized_reference(reference)
    for detail in tx_details:
        reference = _find_text(detail, "Refs/AcctSvcrRef")
        if _is_reference(reference):
            return _normalized_reference(reference)
    reference = _find_text(entry, "AcctSvcrRef")
    if _is_reference(reference):
        return _normalized_reference(reference)
    return None


def _camt_date(entry: ET.Element, tag: str) -> date | None:
    """Liest ``BookgDt``/``ValDt`` samt ``Dt``- oder ``DtTm``-Kind."""
    container = entry.find(tag)
    if container is None:
        return None
    for child_tag in ("Dt", "DtTm"):
        child = container.find(child_tag)
        if child is not None and child.text and child.text.strip():
            return _parse_date(child.text.strip())
    return None


def _map_mt940_transaction(
    data: Mapping[str, object],
    *,
    default_status: TransactionStatus,
) -> TransactionData:
    """Bildet eine MT940-``data``-Struktur auf ``TransactionData`` ab."""
    amount_value = data.get("amount")
    if amount_value is None:
        raise FinTSMappingError("MT940-Buchung ohne Betrag")
    amount_cents = _decimal_to_cents(_to_decimal(amount_value))

    currency = _normalized_currency(data.get("currency")) or _currency_of(
        amount_value
    )
    if currency is None:
        raise FinTSMappingError("MT940-Buchung ohne Waehrung")

    entry_date = _optional_date(data.get("entry_date"))
    value_date = _optional_date(data.get("date"))
    booking_date = entry_date or value_date
    if booking_date is None:
        raise FinTSMappingError("MT940-Buchung ohne Buchungs- oder Wertstellungsdatum")

    details = _detail_mappings(data)
    status = _mt940_status(data, default_status, entry_date=entry_date)

    return TransactionData(
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=currency,
        origin="fints",
        value_date=value_date,
        counterparty=_mt940_counterparty(data, details),
        counterparty_iban=_mt940_iban(data, details),
        purpose=_mt940_purpose(data, details),
        status=status,
        external_ref=_mt940_external_reference(data, details),
    )


def _mt940_status(
    data: Mapping[str, object],
    default_status: TransactionStatus,
    *,
    entry_date: date | None,
) -> TransactionStatus:
    """Bestimmt den Status aus Kennzeichen, Buchungstag und Vorgabe."""
    pending_flag = data.get("pending")
    status_marker = data.get("status")
    if pending_flag is True:
        return "vorgemerkt"
    if isinstance(status_marker, str) and status_marker.strip().upper() in _PENDING_MARKERS:
        return "vorgemerkt"
    if default_status == "gebucht" and entry_date is None:
        # Vormerkungen koennen den Buchungstag nicht enthalten (F-SYN-03).
        return "vorgemerkt"
    return default_status


def _mt940_counterparty(
    data: Mapping[str, object],
    details: list[Mapping[str, object]],
) -> str | None:
    """Liefert die Gegenpartei, richtungsabhaengig und mit Detail-Fallback."""
    direction = data.get("status")
    keys = (
        ("recipient_name", "applicant_name")
        if isinstance(direction, str) and direction.strip().upper() == "D"
        else ("applicant_name", "recipient_name")
    )
    for key in keys:
        value = _text_value(data.get(key))
        if value is not None:
            return value
    for detail in details:
        for key in keys:
            value = _text_value(detail.get(key))
            if value is not None:
                return value
    return None


def _mt940_iban(
    data: Mapping[str, object],
    details: list[Mapping[str, object]],
) -> str | None:
    """Liefert die Gegenpartei-IBAN, falls die Quelle sie enthaelt."""
    for key in ("applicant_iban", "gvc_applicant_iban", "recipient_iban"):
        value = _text_value(data.get(key))
        if value is not None:
            return value
    for detail in details:
        for key in ("applicant_iban", "gvc_applicant_iban", "recipient_iban"):
            value = _text_value(detail.get(key))
            if value is not None:
                return value
    return None


def _mt940_purpose(
    data: Mapping[str, object],
    details: list[Mapping[str, object]],
) -> str | None:
    """Liefert den Verwendungszweck mehrzeilig und ohne Leerzeilen."""
    lines: list[str] = []
    top_level = _text_value(data.get("purpose"))
    if top_level is not None:
        lines.extend(_clean_lines(top_level))
    additional = _text_value(data.get("additional_purpose"))
    if additional is not None:
        lines.extend(_clean_lines(additional))
    for detail in details:
        detail_purpose = _text_value(detail.get("purpose"))
        if detail_purpose is not None:
            lines.extend(_clean_lines(detail_purpose))
        detail_additional = _text_value(detail.get("additional_purpose"))
        if detail_additional is not None:
            lines.extend(_clean_lines(detail_additional))
    if not lines:
        unstructured = data.get("transaction_details")
        if isinstance(unstructured, str):
            lines.extend(_clean_lines(unstructured))
    return "\n".join(lines) if lines else None


def _mt940_external_reference(
    data: Mapping[str, object],
    details: list[Mapping[str, object]],
) -> str | None:
    """Liefert die externe Referenz nur, wenn die Bank eine geliefert hat."""
    for key in ("end_to_end_reference", "customer_reference"):
        value = _text_value(data.get(key))
        if _is_reference(value):
            return value
    for detail in details:
        for key in ("end_to_end_reference", "customer_reference"):
            value = _text_value(detail.get(key))
            if _is_reference(value):
                return value
    return None


def _detail_mappings(data: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Liefert die Detail-Dicts aelterer mt940-Versionen, sonst eine leere Liste."""
    details = data.get("transaction_details")
    if isinstance(details, list):
        return [detail for detail in details if isinstance(detail, Mapping)]
    return []


def _transaction_data(
    transaction: MT940TransactionLike | Mapping[str, object],
) -> Mapping[str, object]:
    """Liest das ``data``-Mapping aus einer Transaktion oder direkt aus ihr."""
    if isinstance(transaction, Mapping):
        return transaction
    data = getattr(transaction, "data", None)
    if isinstance(data, Mapping):
        return data
    raise FinTSMappingError("MT940-Transaktion ohne Datenfeld")


def _strip_namespaces(root: ET.Element) -> None:
    """Entfernt XML-Namespaces, ohne Attribute oder Reihenfolge zu aendern."""
    for element in root.iter():
        tag = element.tag
        if isinstance(tag, str) and "}" in tag:
            element.tag = tag.split("}", 1)[1]


def _find_text(parent: ET.Element, path: str) -> str | None:
    """Liefert den getrimmten Text des ersten Treffers oder ``None``."""
    element = parent.find(path)
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None


def _parse_date(text: str) -> date:
    """Liest ein camt-/MT940-Datum in ISO- oder kompakter Schreibweise."""
    candidate = text.strip()
    try:
        return date.fromisoformat(candidate[:10])
    except ValueError:
        pass
    if len(candidate) >= 8 and candidate[:8].isdigit():
        try:
            return datetime.strptime(candidate[:8], "%Y%m%d").date()
        except ValueError:
            pass
    raise FinTSMappingError("Datum konnte nicht gelesen werden")


def _optional_date(value: object) -> date | None:
    """Wandelt ein optionales Datum um; ``None`` und Leertext bleiben leer."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return _parse_date(text)
    raise FinTSMappingError("Datum konnte nicht gelesen werden")


def _to_decimal(value: object) -> Decimal:
    """Wandelt Betragsobjekte, Decimal, int oder Text in ``Decimal`` um.

    Floats werden bewusst abgelehnt (N-DB-02): Sie koennen Cent-Betraege
    unbemerkt veraendern.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise FinTSMappingError("Betrag ist kein Zahlenwert")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise FinTSMappingError("Betrag darf kein Float sein")
    if isinstance(value, str):
        return _parse_decimal(value)
    inner = getattr(value, "amount", None)
    if inner is not None and inner is not value:
        return _to_decimal(inner)
    raise FinTSMappingError("Betrag ist kein Zahlenwert")


def _parse_decimal(text: str) -> Decimal:
    """Liest einen Dezimaltext mit Punkt oder Komma als Trenner."""
    normalized = text.strip().replace(",", ".")
    if not normalized:
        raise FinTSMappingError("Betrag ist leer")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise FinTSMappingError("Betrag ist kein Zahlenwert") from exc


def _decimal_to_cents(amount: Decimal) -> int:
    """Rechnet in ganze Cent um und lehnt Bruchteile eines Cents ab."""
    if not amount.is_finite():
        raise FinTSMappingError("Betrag ist kein endlicher Zahlenwert")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise FinTSMappingError("Betrag hat mehr als zwei Nachkommastellen")
    return int(cents)


def _currency_of(value: object) -> str | None:
    """Liest eine Waehrung aus einem Betrags- oder Saldo-Objekt."""
    currency = getattr(value, "currency", None)
    if isinstance(currency, str):
        return _normalized_currency(currency)
    return None


def _normalized_currency(value: object) -> str | None:
    """Trimmt und schreibt eine Waehrung gross; leere Werte werden ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text or None


def _text_value(value: object) -> str | None:
    """Liefert getrimmten Text oder ``None`` fuer leere/nicht-textliche Werte."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_lines(text: str) -> list[str]:
    """Zerlegt Text in getrimmte, nicht-leere Zeilen."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _is_reference(value: str | None) -> bool:
    """Prueft, ob eine gelieferte Referenz inhaltlich brauchbar ist."""
    return value is not None and value.strip().upper() not in _EMPTY_REFERENCES


def _normalized_reference(value: str | None) -> str | None:
    """Liefert eine bereinigte Referenz oder ``None``."""
    if value is None:
        return None
    text = value.strip()
    return text or None
