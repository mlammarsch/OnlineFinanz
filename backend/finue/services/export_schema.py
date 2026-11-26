"""Pydantic-v2-Modelle fuer das eigene JSON-Exportformat v1.

Setzt F-EXP-04 (Kopf mit Format, Version, Erstellungszeitpunkt, Filter und
Zeitraum; Konten ohne Zugangsdaten; vollstaendige Buchungen inklusive Pruef-
und Abstimmungskennzeichen mit Zeitpunkt und Herkunft; Betraege als
Dezimal-String mit Punkt; Datum ISO ``JJJJ-MM-TT``) und F-EXP-07
(Versionierung, neuere Versionen werden mit klarer Meldung abgelehnt) um.

Die Modelle bilden ``docs/schemas/export-v1.schema.json`` ab. Zugangsdaten,
PINs, TAN-Verfahren und Bankzugangsdaten sind bewusst nicht enthalten
(N-SEC-01, N-SEC-02).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal, Mapping, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

EXPORT_FORMAT_NAME = "finanzuebersicht"
SUPPORTED_EXPORT_VERSION = 1

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Id = Annotated[int, Field(strict=True, ge=1)]
StrictBool = Annotated[bool, Field(strict=True)]
Text = Annotated[str, StringConstraints(strict=True)]
NonEmptyText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
ShortText = Annotated[str, StringConstraints(strict=True, max_length=34)]
ExternalRef = Annotated[str, StringConstraints(strict=True, max_length=255)]
CurrencyCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{3}$")]
Iban = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$", max_length=34),
]
Fingerprint = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-fA-F]{64}$")]
AmountString = Annotated[str, StringConstraints(strict=True, pattern=r"^-?\d+\.\d{2}$")]

FlagOrigin = Literal["ui", "mcp", "import"]


def _parse_iso_date(value: object) -> date:
    """Akzeptiert nur ISO-Datumswerte als String im Format ``JJJJ-MM-TT``."""
    if not isinstance(value, str):
        raise ValueError("Datum muss ein String im Format JJJJ-MM-TT sein")
    if not _ISO_DATE_RE.fullmatch(value):
        raise ValueError("Datum muss exakt im Format JJJJ-MM-TT stehen")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Datum ist kein gueltiges Kalenderdatum") from exc


def _parse_iso_datetime(value: object) -> datetime:
    """Akzeptiert nur ISO-8601-Zeitstempel als String mit Zeitzone."""
    if not isinstance(value, str):
        raise ValueError("Zeitstempel muss ein ISO-8601-String mit Zeitzone sein")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Zeitstempel ist kein gueltiger ISO-8601-Wert") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Zeitstempel muss eine Zeitzone enthalten")
    return parsed


IsoDate = Annotated[date, BeforeValidator(_parse_iso_date)]
IsoDateTime = Annotated[datetime, BeforeValidator(_parse_iso_datetime)]


class ExportFormatError(ValueError):
    """Basisfehler fuer ungueltige Exportdaten."""


class UnsupportedExportVersionError(ExportFormatError):
    """Die Datei nutzt eine neuere Formatversion als diese App (F-EXP-07)."""


def ensure_supported_version(version: object) -> int:
    """Prueft die Formatversion vor der Modellwahl (F-EXP-07).

    Neuere Versionen werden mit klarer Meldung abgelehnt. Eine niedrigere
    Version ist zulaessig und wird vom Import schrittweise konvertiert.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        raise ExportFormatError("Feld 'version' muss eine ganze Zahl sein (kein String).")
    if version < 1:
        raise ExportFormatError("Feld 'version' muss mindestens 1 sein.")
    if version > SUPPORTED_EXPORT_VERSION:
        raise UnsupportedExportVersionError(
            f"Exportformat-Version {version} ist neuer als die von dieser App unterstuetzte "
            f"Version {SUPPORTED_EXPORT_VERSION}."
        )
    return version


class _StrictModel(BaseModel):
    """Basisklasse: keine unbekannten Felder, keine stillen Zusatzdaten."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExportFilterV1(_StrictModel):
    """Filterbeschreibung des Exports."""

    scope: Literal["all", "account", "group", "search", "selection"]
    account_ids: list[Id] | None = None
    group_ids: list[Id] | None = None
    transaction_ids: list[Id] | None = None
    search: Text | None = None
    checked: StrictBool | None = None
    reconciled: StrictBool | None = None
    with_note: StrictBool | None = None

    @model_validator(mode="after")
    def _validate_id_lists(self) -> Self:
        for name, values in (
            ("account_ids", self.account_ids),
            ("group_ids", self.group_ids),
            ("transaction_ids", self.transaction_ids),
        ):
            if values is not None and len(values) != len(set(values)):
                raise ValueError(f"'{name}' darf keine doppelten IDs enthalten")
        return self


class ExportPeriodV1(_StrictModel):
    """Inklusiver Zeitraum des Exports; ``null`` bedeutet offen."""

    start: IsoDate | None
    end: IsoDate | None

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("period.start darf nicht nach period.end liegen")
        return self


class ExportAccountV1(_StrictModel):
    """Kontostammdaten ohne Zugangsdaten (kein PIN, Login, TAN oder Bankzugang)."""

    id: Id
    name: NonEmptyText
    iban: Iban | None
    currency: CurrencyCode
    account_type: Text | None


class ExportTransactionV1(_StrictModel):
    """Eine Buchung mit allen exportrelevanten Feldern (F-EXP-04)."""

    id: Id
    account_id: Id
    status: Literal["gebucht", "vorgemerkt"]
    booking_date: IsoDate
    value_date: IsoDate | None
    counterparty: Text | None
    counterparty_iban: ShortText | None
    purpose: Text | None
    note: Text | None
    checked: StrictBool
    checked_at: IsoDateTime | None
    checked_origin: FlagOrigin | None
    reconciled: StrictBool
    reconciled_at: IsoDateTime | None
    reconciled_origin: FlagOrigin | None
    origin: Literal["fints", "csv", "json"]
    external_ref: ExternalRef | None
    fingerprint: Fingerprint
    occurrence_index: Id
    merged: StrictBool
    amount: AmountString
    currency: CurrencyCode

    @model_validator(mode="after")
    def _validate_flags(self) -> Self:
        _validate_flag(self.checked, self.checked_at, self.checked_origin, "checked")
        _validate_flag(self.reconciled, self.reconciled_at, self.reconciled_origin, "reconciled")
        return self


class ExportDocumentV1(_StrictModel):
    """Wurzel des JSON-Exports, Formatversion 1."""

    format: Literal["finanzuebersicht"]
    version: Literal[1]
    created_at: IsoDateTime
    filter: ExportFilterV1
    period: ExportPeriodV1
    accounts: list[ExportAccountV1]
    transactions: list[ExportTransactionV1]

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        account_ids = [account.id for account in self.accounts]
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("accounts enthaelt doppelte IDs")

        transaction_ids = [transaction.id for transaction in self.transactions]
        if len(set(transaction_ids)) != len(transaction_ids):
            raise ValueError("transactions enthaelt doppelte IDs")

        unknown_accounts = sorted({transaction.account_id for transaction in self.transactions} - set(account_ids))
        if unknown_accounts:
            raise ValueError(f"transactions verweisen auf unbekannte account_id(s): {unknown_accounts}")
        return self


def parse_export_document(payload: Mapping[str, object]) -> ExportDocumentV1:
    """Liest ein Exportdokument: erst Version pruefen, dann v1 validieren.

    Eine neuere Version wird vor der Pydantic-Validierung mit
    ``UnsupportedExportVersionError`` abgelehnt (F-EXP-07), damit die Meldung
    die Version nennt statt eines generischen Schemafehlers.
    """
    if not isinstance(payload, Mapping):
        raise ExportFormatError("Exportdaten muessen ein JSON-Objekt sein.")
    if "version" not in payload:
        raise ExportFormatError("Feld 'version' fehlt.")
    ensure_supported_version(payload["version"])
    return ExportDocumentV1.model_validate(dict(payload))


def _validate_flag(flag: bool, at: datetime | None, origin: str | None, label: str) -> None:
    """Erzwingt Zeitpunkt und Herkunft genau dann, wenn ein Kennzeichen gesetzt ist."""
    if flag:
        if at is None or origin is None:
            raise ValueError(
                f"'{label}' ist gesetzt, dann muessen '{label}_at' und '{label}_origin' gesetzt sein"
            )
    elif at is not None or origin is not None:
        raise ValueError(
            f"'{label}' ist nicht gesetzt, dann muessen '{label}_at' und '{label}_origin' null sein"
        )
