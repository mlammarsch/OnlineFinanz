"""JSON-Import des eigenen Exportformats ohne Mapping (F-IMP-04).

Erkennt das eigene Format ueber den Kopf ``format == "finanzuebersicht"`` und
liest die Buchungen direkt in die Import-Datenstruktur. Ein Mapping-Schritt
entfaellt (F-IMP-04). Die Validierung nutzt die Modelle aus
:mod:`finue.services.export_schema` (F-EXP-04); Betraege werden ohne
Gleitkomma-Rundung aus dem Dezimal-String in Cent umgerechnet (N-DB-02).

Versionsregeln nach F-EXP-07: Version 1 ist die aktuelle Version. Abweichende
Versionen werden an :func:`finue.services.export_version.upgrade_to_current`
delegiert, sobald dieses Modul (T-052) vorhanden ist. Fehlt die
Versionskonvertierung, wird eine neuere Version mit klarer Meldung abgelehnt.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping

from pydantic import ValidationError

from finue.services.export_schema import (
    EXPORT_FORMAT_NAME,
    SUPPORTED_EXPORT_VERSION,
    ExportDocumentV1,
    ExportTransactionV1,
    UnsupportedExportVersionError,
)

__all__ = [
    "ImportFormatError",
    "TransactionData",
    "amount_string_to_cents",
    "detect_own_format",
    "parse_own_json",
]


class ImportFormatError(ValueError):
    """Die Datei ist kein gueltiges eigenes JSON-Importdokument."""


@dataclass(frozen=True, slots=True)
class TransactionData:
    """Eine validierte Buchung fuer den Import, unabhaengig von der Quelle.

    Betraege stehen als ganze Cent zur Verfuegung (N-DB-02). Datums- und
    Zeitwerte sind bereits geparst. Optionale Kennzeichen tragen Zeitpunkt und
    Herkunft nur dann, wenn sie gesetzt sind (F-ABS-01).
    """

    account_id: int
    status: str
    booking_date: date
    value_date: date | None
    amount_cents: int
    currency: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None
    note: str | None
    checked: bool
    checked_at: datetime | None
    checked_origin: str | None
    reconciled: bool
    reconciled_at: datetime | None
    reconciled_origin: str | None
    origin: str
    external_ref: str | None
    fingerprint: str
    occurrence_index: int
    merged: bool


def detect_own_format(data: object) -> bool:
    """Prueft, ob ``data`` der Kopf des eigenen JSON-Formats ist (F-IMP-04).

    Nur ein JSON-Objekt mit ``format == "finanzuebersicht"`` gilt als eigenes
    Format. Fremdes JSON, fehlende oder anderslautende ``format``-Felder
    liefern ``False`` statt einer Ausnahme.
    """
    return isinstance(data, Mapping) and data.get("format") == EXPORT_FORMAT_NAME


def parse_own_json(data: Mapping[str, object]) -> list[TransactionData]:
    """Liest die Buchungen des eigenen JSON-Formats ohne Mapping (F-IMP-04).

    Args:
        data: Geparstes JSON-Objekt (Wurzel des Exportdokuments).

    Returns:
        Die Buchungen in Dokumentreihenfolge als :class:`TransactionData`.

    Raises:
        ImportFormatError: Wurzel ist kein Objekt, das eigene Format fehlt,
            Pflichtfelder sind ungueltig oder die Version ist nicht lesbar.
        UnsupportedExportVersionError: Die Dateiversion ist neuer als die App
            und keine Versionskonvertierung (T-052) ist vorhanden (F-EXP-07).
    """
    if not isinstance(data, Mapping):
        raise ImportFormatError("JSON-Importdaten muessen ein JSON-Objekt sein.")
    if not detect_own_format(data):
        raise ImportFormatError("Kein eigenes JSON-Format: Feld 'format' fehlt oder ist nicht 'finanzuebersicht'.")

    document = _validate_document(data)
    return [_to_transaction_data(transaction) for transaction in document.transactions]


def amount_string_to_cents(value: str) -> int:
    """Rechnet einen Dezimal-String mit Punkt exakt in ganze Cent um (N-DB-02).

    Es wird bewusst :class:`decimal.Decimal` statt ``float`` verwendet, damit
    keine Binaer-Rundungsfehler entstehen. Mehr als zwei Nachkommastellen
    werden abgelehnt.
    """
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ImportFormatError(f"Betrag {value!r} ist kein Dezimal-String.") from exc
    if not amount.is_finite():
        raise ImportFormatError(f"Betrag {value!r} ist keine endliche Zahl.")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ImportFormatError(f"Betrag {value!r} hat mehr als zwei Nachkommastellen.")
    return int(cents)


def _validate_document(data: Mapping[str, object]) -> ExportDocumentV1:
    """Prueft die Version und validiert das Dokument gegen das v1-Modell."""
    version = _read_version(data)
    if version != SUPPORTED_EXPORT_VERSION:
        data = _upgrade_to_current(data, version)

    try:
        return ExportDocumentV1.model_validate(dict(data))
    except ValidationError as exc:
        raise ImportFormatError(f"Ungueltige JSON-Importdaten: {_describe_validation_error(exc)}") from exc


def _read_version(data: Mapping[str, object]) -> int:
    """Liest und prueft das Feld ``version`` vor jeder Schemavalidierung."""
    if "version" not in data:
        raise ImportFormatError("Feld 'version' fehlt im JSON-Importdokument.")
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ImportFormatError("Feld 'version' muss eine ganze Zahl sein (kein String).")
    if version < 1:
        raise ImportFormatError("Feld 'version' muss mindestens 1 sein.")
    return version


def _upgrade_to_current(data: Mapping[str, object], version: int) -> Mapping[str, object]:
    """Delegiert abweichende Versionen an die Versionskonvertierung (T-052).

    Das Modul wird erst zur Laufzeit importiert, damit T-051 auch ohne T-052
    lauffaehig bleibt. Fehlt es, gilt die Regel aus F-EXP-07: neuere Versionen
    werden mit klarer Meldung abgelehnt.
    """
    try:
        export_version = importlib.import_module("finue.services.export_version")
    except ModuleNotFoundError:
        export_version = None

    upgrader = getattr(export_version, "upgrade_to_current", None)
    if upgrader is None:
        if version > SUPPORTED_EXPORT_VERSION:
            raise UnsupportedExportVersionError(
                f"Exportformat-Version {version} ist neuer als die von dieser App unterstuetzte "
                f"Version {SUPPORTED_EXPORT_VERSION}; eine Versionskonvertierung ist nicht verfuegbar."
            )
        raise ImportFormatError(
            f"Exportformat-Version {version} kann nicht auf Version {SUPPORTED_EXPORT_VERSION} "
            "konvertiert werden, weil die Versionskonvertierung fehlt."
        )

    upgraded = upgrader(dict(data))
    if not isinstance(upgraded, Mapping):
        raise ImportFormatError("Versionskonvertierung lieferte kein JSON-Objekt.")
    return upgraded


def _to_transaction_data(transaction: ExportTransactionV1) -> TransactionData:
    """Uebertraegt eine validierte Exportbuchung in die Importstruktur."""
    return TransactionData(
        account_id=transaction.account_id,
        status=transaction.status,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        amount_cents=amount_string_to_cents(transaction.amount),
        currency=transaction.currency,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
        note=transaction.note,
        checked=transaction.checked,
        checked_at=transaction.checked_at,
        checked_origin=transaction.checked_origin,
        reconciled=transaction.reconciled,
        reconciled_at=transaction.reconciled_at,
        reconciled_origin=transaction.reconciled_origin,
        origin=transaction.origin,
        external_ref=transaction.external_ref,
        fingerprint=transaction.fingerprint,
        occurrence_index=transaction.occurrence_index,
        merged=transaction.merged,
    )


def _describe_validation_error(exc: ValidationError) -> str:
    """Fasst die ersten Pydantic-Fehler als eine klare deutsche Meldung zusammen."""
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "ungueltiger Wert"))
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)
