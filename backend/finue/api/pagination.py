"""Paginierung fuer Listenantworten (F-CON-02, F-MCP-11).

Listennetze nutzen den Umschlag ``{items, next_cursor}``. ``limit`` hat einen
Standard von 50 und hoechstens 200 (Werte in :mod:`finue.config`, SPEC
F-MCP-11); der Wert wird gegen die zentralen Grenzen geprueft und bei
Verletzung als deutscher 422-Fehler gemeldet.

Der Cursor ist ein undurchsichtiger, base64-kodierter Offset. Die
Schluesselwort-Paginierung der Buchungsliste folgt in T-093 und nutzt denselben
Umschlag.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import Request
from pydantic import BaseModel

from finue.api.errors import ValidationApiError
from finue.config import Settings

__all__ = [
    "Page",
    "PageParams",
    "decode_cursor",
    "encode_cursor",
    "resolve_page_params",
]

T = TypeVar("T")

_CURSOR_PREFIX = "o"

#: Obergrenze des Cursor-Offsets (portabel 32 Bit). Groessere Werte wuerden die
#: Datenbank ueberlaufen lassen und als 500 statt 422 erscheinen (F-CON-02).
_MAX_OFFSET = 2**31 - 1

#: Hoechstzahl an Ziffern des Offsets (10 fuer ``2147483647``). Eine laengere
#: Ziffernfolge wird vor :func:`int` abgelehnt, weil ``int`` ab ca. 4300 Ziffern
#: ``ValueError`` wirft und der Wert sonst als 500 statt 422 endet.
_MAX_OFFSET_DIGITS = len(str(_MAX_OFFSET))


class Page(BaseModel, Generic[T]):
    """Antwortumschlag einer Liste: Eintraege plus Cursor der naechsten Seite."""

    items: list[T]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class PageParams:
    """Aufgeloeste Listenparameter ``limit`` und ``cursor``."""

    limit: int
    cursor: str | None = None


def encode_cursor(offset: int) -> str:
    """Kodiert einen Offset als undurchsichtigen Cursor."""

    if offset < 0:
        raise ValueError("offset muss groesser oder gleich 0 sein.")
    raw = f"{_CURSOR_PREFIX}{offset}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    """Dekodiert einen Cursor; ungueltige Werte werden als 422 gemeldet.

    Ein Offset ueber :data:`_MAX_OFFSET`, eine ueberlange Ziffernfolge oder
    eine nicht-kanonische Form mit fuehrenden Nullen wird als ungueltiger
    Cursor abgelehnt, statt :func:`int` (und damit die Datenbank) in einen
    Fehler zu treiben.
    """

    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValidationApiError(
            "Der Cursor ist ungueltig.",
            details={"field": "cursor"},
        ) from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationApiError("Der Cursor ist ungueltig.", details={"field": "cursor"})
    digits = raw[1:]
    # Fuehrende Nullen und die Ziffernlaenge vor int() pruefen: int() wirft ab
    # ca. 4300 Ziffern ValueError (Python-Limit), sonst folgt ein 500 statt 422.
    if not digits or not digits.isdigit():
        raise ValidationApiError("Der Cursor ist ungueltig.", details={"field": "cursor"})
    if len(digits) > 1 and digits[0] == "0":
        raise ValidationApiError("Der Cursor ist ungueltig.", details={"field": "cursor"})
    if len(digits) > _MAX_OFFSET_DIGITS:
        raise ValidationApiError("Der Cursor ist ungueltig.", details={"field": "cursor"})
    offset = int(digits)
    if offset > _MAX_OFFSET:
        raise ValidationApiError("Der Cursor ist ungueltig.", details={"field": "cursor"})
    return offset


def resolve_page_params(request: Request, settings: Settings) -> PageParams:
    """Liest ``limit`` und ``cursor`` aus der Anfrage und prueft die Grenzen.

    Fehlt ``limit``, gilt ``settings.api_default_page_size``. Ein Wert unter 1
    oder ueber ``settings.api_max_page_size`` wird als deutscher 422-Fehler
    abgelehnt; ein nicht-numerischer Wert ebenso.
    """

    raw_limit = request.query_params.get("limit")
    if raw_limit is None or raw_limit == "":
        limit = settings.api_default_page_size
    else:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise ValidationApiError(
                "Der Parameter 'limit' muss eine ganze Zahl sein.",
                details={"field": "limit"},
            ) from exc
        if limit < 1 or limit > settings.api_max_page_size:
            raise ValidationApiError(
                f"Der Parameter 'limit' muss zwischen 1 und {settings.api_max_page_size} liegen.",
                details={"field": "limit", "min": 1, "max": settings.api_max_page_size},
            )

    cursor = request.query_params.get("cursor")
    if cursor is not None:
        decode_cursor(cursor)
    return PageParams(limit=limit, cursor=cursor)
