"""Kurzlebiger Zwischenspeicher fuer hochgeladene Import-Dateien (F-IMP-01).

Der Import-Assistent laeuft in mehreren Schritten: erst die Formaterkennung
(:func:`finue.services.import_detect.detect_import`), dann Vorschau und Lauf.
Damit die Datei nicht bei jedem Schritt erneut uebertragen werden muss, legt
``POST /api/import/detect`` die rohen Bytes hier ab und gibt einen
undurchsichtigen Token zurueck. Vorschau und Lauf holen die Bytes ueber diesen
Token wieder ab.

Der Speicher liegt **nur im Arbeitsspeicher** (kein Volume, kein Backup) und
raeumt Eintraege nach einer festen Lebensdauer (TTL) sowie bei Ueberschreiten
der Hoechstzahl von selbst auf. Der Server laeuft mit genau einem
Worker-Prozess (D3), daher genuegt ein prozesslokaler Speicher. Ein Zugriff mit
unbekanntem oder abgelaufenem Token wirft :class:`UploadExpired`; der Aufrufer
meldet dann Klartext und bittet um einen erneuten Upload.

N-SEC-02: Der Speicher haelt ausschliesslich die hochgeladenen Rohbytes und
den Zeitpunkt; er protokolliert nichts und gibt keine Geheimnisse aus.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_UPLOAD_TTL_SECONDS",
    "UploadExpired",
    "UploadStore",
]

#: Standard-Lebensdauer eines Uploads in Sekunden (Entscheidung in ``decisions.md``).
DEFAULT_UPLOAD_TTL_SECONDS: Final = 300

#: Hoechstzahl gleichzeitig gehaltener Uploads; aeltere fallen zuerst heraus.
DEFAULT_MAX_ENTRIES: Final = 32

#: Laenge des Zufallstokens in Bytes (base64-urlsafe kodiert rund 32 Zeichen).
_TOKEN_BYTES: Final = 24


class UploadExpired(LookupError):
    """Der Upload-Token ist unbekannt oder abgelaufen (erneuter Upload noetig)."""


@dataclass(frozen=True, slots=True)
class _Entry:
    """Ein zwischengespeicherter Upload samt Ablaufzeitpunkt."""

    data: bytes
    expires_at: datetime


class UploadStore:
    """Threadsicherer In-Memory-Speicher fuer Import-Uploads mit TTL (D3).

    Args:
        clock: Injizierbare Uhr (aware ``datetime``), damit Tests die
            Lebensdauer ohne Systemzeit-Stub pruefen koennen.
        ttl_seconds: Lebensdauer eines Uploads in Sekunden.
        max_entries: Hoechstzahl gleichzeitig gehaltener Uploads.

    Raises:
        ValueError: ``ttl_seconds`` oder ``max_entries`` sind nicht positiv.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        ttl_seconds: int = DEFAULT_UPLOAD_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds muss mindestens 1 sein.")
        if max_entries < 1:
            raise ValueError("max_entries muss mindestens 1 sein.")
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def put(self, data: bytes) -> str:
        """Legt ``data`` ab und liefert einen neuen, undurchsichtigen Token."""

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = self._clock()
        with self._lock:
            self._purge(now)
            self._entries[token] = _Entry(data=data, expires_at=now + self._ttl)
            while len(self._entries) > self._max_entries:
                # dict bewahrt die Einfuegereihenfolge: das aelteste zuerst.
                oldest = next(iter(self._entries))
                del self._entries[oldest]
        return token

    def get(self, token: str) -> bytes:
        """Liefert die Bytes zum Token.

        Raises:
            UploadExpired: Der Token ist unbekannt oder abgelaufen.
        """

        now = self._clock()
        with self._lock:
            self._purge(now)
            entry = self._entries.get(token)
        if entry is None:
            raise UploadExpired("Der Upload ist abgelaufen oder unbekannt.")
        return entry.data

    def discard(self, token: str) -> None:
        """Entfernt einen Upload vorzeitig (idempotent)."""

        with self._lock:
            self._entries.pop(token, None)

    def _purge(self, now: datetime) -> None:
        """Entfernt alle abgelaufenen Eintraege (Aufruf unter dem Lock)."""

        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            del self._entries[token]
