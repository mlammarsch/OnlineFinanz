"""Wiederholungslogik fuer wiederholbare FinTS-Bankfehler (F-SYN-06).

Die Funktion :func:`run_with_retry` umrahmt eine einzelne lesende
Bankoperation und wiederholt sie nur dann, wenn alle Bedingungen zutreffen:

- Die Operation wirft einen :class:`~finue.services.fints_types.FinTSBankError`.
- Der Fehlercode ist nach dem Fehler-Mapping als wiederholbar markiert
  (``is_retryable(code)``, Standardcodes 9800 und 9010).
- Es sind noch Wiederholungen offen (hoechstens zwei, also insgesamt
  hoechstens drei Aufrufe).

Ein neuer Dialog je Versuch ist ein Vertrag des Aufrufers: Die Operation
erhaelt die Versuchsnummer (ab 1) und MUSS bei jedem Aufruf eine frische
Dialoginstanz erzeugen. Diese Funktion haelt keinen Dialog und keinen
Bankzustand; sie kennt weder Datenbank noch python-fints. Der Import von
:mod:`finue.services.fints_types` ist projektlokal und frei von
python-fints-Abhaengigkeiten.

Sofort beendet wird ohne Wiederholung bei falscher PIN, gesperrtem Zugang,
Abbruch und TAN-Timeout. Das gilt unabhaengig davon, was ``is_retryable``
fuer den Code meldet, weil diese Fehler nie durch einen neuen Versuch
behebbar sind (F-SYN-05, F-SYN-10, N-SEC-02).

Die Wartezeit ist ueber ``sleep`` injizierbar, damit Tests ohne echte
Wartezeit laufen. ``should_abort`` wird vor jedem Versuch und vor sowie nach
dem Warten geprueft: Kommt das Abbruchsignal waehrend der Wartezeit, folgt
kein weiterer Versuch.

:class:`RetryOutcome` liefert Ergebnis oder die zuletzt aufgetretene
Exception zusammen mit der Versuchszahl fuer das Sync-Protokoll (F-SYN-07).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Generic, TypeVar

from finue.services.fints_types import (
    AccessLockedError,
    FinTSBankError,
    PinWrongError,
    SyncCancelledError,
    TanTimeoutError,
)

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_WAIT_SECONDS",
    "RetryOutcome",
    "run_with_retry",
]

T = TypeVar("T")

#: F-SYN-06: hoechstens zwei Wiederholungen (insgesamt drei Aufrufe).
DEFAULT_MAX_RETRIES: Final[int] = 2

#: Wartezeit zwischen zwei Versuchen. Ueber ``run_with_retry`` aenderbar.
DEFAULT_WAIT_SECONDS: Final[float] = 2.0

#: Bankfehler, die nie wiederholt werden, auch wenn ihr Code als
#: wiederholbar markiert waere. PIN und Sperre sind nicht durch einen neuen
#: Dialog behebbar (F-SYN-05, N-SEC-02).
_HARD_STOP_BANK_ERRORS: Final[tuple[type[FinTSBankError], ...]] = (
    PinWrongError,
    AccessLockedError,
)


@dataclass(frozen=True, slots=True)
class RetryOutcome(Generic[T]):
    """Ergebnis eines Wiederholungslaufs fuer das Sync-Protokoll.

    Genau einer der beiden Werte ist gesetzt: ``result`` bei Erfolg,
    ``error`` bei einem endgueltigen Fehler. ``attempts`` zaehlt die
    tatsaechlich ausgefuehrten Aufrufe der Operation (0 nur, wenn das
    Abbruchsignal bereits vor dem ersten Versuch gesetzt war). ``aborted``
    markiert einen Abbruch waehrend der Wartezeit; ``error`` traegt dann die
    zuletzt aufgetretene Bank-Exception.
    """

    result: T | None
    error: Exception | None
    attempts: int
    aborted: bool = False

    @property
    def succeeded(self) -> bool:
        """``True``, wenn die Operation ohne Fehler abgeschlossen wurde."""

        return self.error is None


def run_with_retry(
    operation: Callable[[int], T],
    is_retryable: Callable[[str], bool],
    max_retries: int = DEFAULT_MAX_RETRIES,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    should_abort: Callable[[], bool] | None = None,
) -> RetryOutcome[T]:
    """Fuehrt ``operation`` mit hoechstens ``max_retries`` Wiederholungen aus.

    ``operation`` erhaelt die Versuchsnummer ab 1 und muss je Aufruf einen
    neuen Dialog erzeugen (Aufrufervertrag). ``is_retryable`` entscheidet
    anhand des Bankcodes ueber eine Wiederholung. ``sleep`` und
    ``should_abort`` sind injizierbar; es gibt keine echten Wartezeiten in
    Tests. Andere Exceptions als ``FinTSBankError``, ``SyncCancelledError``
    und ``TanTimeoutError`` werden nicht abgefangen, damit Programmierfehler
    sichtbar bleiben.
    """

    if max_retries < 0:
        raise ValueError("max_retries darf nicht negativ sein.")
    if wait_seconds < 0:
        raise ValueError("wait_seconds darf nicht negativ sein.")

    max_attempts = max_retries + 1
    attempts = 0
    last_error: FinTSBankError | None = None

    for attempt in range(1, max_attempts + 1):
        if should_abort is not None and should_abort():
            error: Exception = last_error if last_error is not None else SyncCancelledError()
            return RetryOutcome(result=None, error=error, attempts=attempts, aborted=True)

        attempts = attempt
        try:
            result = operation(attempt)
        except (SyncCancelledError, TanTimeoutError) as exc:
            # Abbruch und Timeout brechen sofort ab (F-SYN-04, F-SYN-10).
            return RetryOutcome(result=None, error=exc, attempts=attempt)
        except FinTSBankError as exc:
            last_error = exc
            if isinstance(exc, _HARD_STOP_BANK_ERRORS) or not is_retryable(exc.code):
                return RetryOutcome(result=None, error=exc, attempts=attempt)
            if attempt >= max_attempts:
                return RetryOutcome(result=None, error=exc, attempts=attempt)
            if not _wait_interruptibly(wait_seconds, sleep, should_abort):
                return RetryOutcome(result=None, error=exc, attempts=attempt, aborted=True)
        else:
            return RetryOutcome(result=result, error=None, attempts=attempt)

    # Nicht erreichbar, weil max_attempts >= 1 und jeder Pfad zurueckkehrt.
    # Der Zweig beruhigt die Typpruefung.
    fallback: Exception = last_error if last_error is not None else RuntimeError("Wiederholung ohne Versuch")
    return RetryOutcome(result=None, error=fallback, attempts=attempts)


def _wait_interruptibly(
    wait_seconds: float,
    sleep: Callable[[float], None],
    should_abort: Callable[[], bool] | None,
) -> bool:
    """Wartet und meldet ``False``, wenn das Abbruchsignal gesetzt ist.

    Ein Abbruch kann waehrend des Wartens gesetzt werden. Er wird deshalb
    nach dem ``sleep`` erneut geprueft; der laufende ``sleep`` wird nicht
    unterbrochen.
    """

    if should_abort is not None and should_abort():
        return False
    sleep(wait_seconds)
    return should_abort is None or not should_abort()
