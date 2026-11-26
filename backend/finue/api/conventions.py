"""Gemeinsame API-Konventionen: Version, Batch, Herkunft und Grenzen.

Setzt die Grundbausteine fuer F-CON-02 (Versionsfeld im Body), F-CON-03
(Batch-Ergebnisumschlag) und F-ABS-01/02 (Herkunft der Aenderung) um. Die
Grenzen (Batch-Groesse, Upload-Groesse) stammen aus :mod:`finue.config`.

Alle Betraege gehen als ``amount_cents`` (ganzzahlig, mit Vorzeichen) plus
``currency`` (ISO-4217) ueber die Leitung; Datumswerte sind ISO ``JJJJ-MM-TT``
(siehe ``docs/api.md``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, Field

from finue.api.errors import PayloadTooLargeError, ValidationApiError
from finue.config import Settings

__all__ = [
    "REST_ORIGIN",
    "BatchItemResult",
    "BatchOutcome",
    "BatchResponse",
    "VersionedRequest",
    "check_batch_size",
    "check_upload_size",
]

#: Herkunft von Aenderungen ueber die REST-API (F-ABS-01/02). MCP nutzt
#: ``"mcp"``, der Datei-Import ``"import"`` (siehe ``export_schema.FlagOrigin``).
REST_ORIGIN: Final = "ui"


class VersionedRequest(BaseModel):
    """Basis fuer aendernde Anfragen mit optimistischem Sperren (F-CON-02).

    Die Anfrage sendet die bekannte Version mit; passt sie nicht mehr, liefert
    der Server 409 mit dem aktuellen Stand (:class:`finue.api.errors.ConflictError`).
    """

    version: int = Field(ge=1, description="Bekannte Versionsnummer des Datensatzes")


class BatchOutcome(StrEnum):
    """Ergebnis je Eintrag eines Batch-Aufrufs (F-CON-03)."""

    GEAENDERT = "geaendert"
    UNVERAENDERT = "unveraendert"
    KONFLIKT = "konflikt"
    FEHLER = "fehler"


class BatchItemResult(BaseModel):
    """Ergebnis einer einzelnen Batch-Aktion.

    ``current`` traegt bei ``konflikt`` den aktuellen Stand als DTO;
    ``message`` ist deutscher Klartext bei ``konflikt`` und ``fehler``.
    """

    id: int
    outcome: BatchOutcome
    current: dict[str, Any] | None = None
    message: str | None = None


class BatchResponse(BaseModel):
    """Umschlag einer Batch-Aktion; Konflikte brechen den Rest nicht ab."""

    results: list[BatchItemResult]


def check_batch_size(count: int, settings: Settings) -> None:
    """Lehnt einen Batch ab, der die zentrale Obergrenze ueberschreitet.

    Raises:
        ValidationApiError: ``count`` liegt ueber ``settings.api_max_batch_size``.
    """

    if count > settings.api_max_batch_size:
        raise ValidationApiError(
            f"Der Batch darf hoechstens {settings.api_max_batch_size} Eintraege enthalten.",
            details={"max": settings.api_max_batch_size, "actual": count},
        )


def check_upload_size(size_bytes: int, settings: Settings) -> None:
    """Lehnt eine Datei ab, die die zentrale Obergrenze ueberschreitet.

    Raises:
        PayloadTooLargeError: ``size_bytes`` liegt ueber ``settings.api_max_upload_bytes``.
    """

    if size_bytes > settings.api_max_upload_bytes:
        raise PayloadTooLargeError(
            f"Die Datei darf hoechstens {settings.api_max_upload_bytes} Bytes gross sein.",
            details={"max": settings.api_max_upload_bytes, "actual": size_bytes},
        )
