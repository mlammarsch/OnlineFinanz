"""Tests fuer die Wiederholungslogik wiederholbarer Bankfehler (F-SYN-06).

Abgedeckt: Erfolg im zweiten Versuch, endgueltiges Ende nach drei Aufrufen,
sofortiges Ende bei nicht wiederholbarem Code sowie bei falscher PIN, Sperre,
Abbruch und Timeout, Abbruchsignal waehrend der Wartezeit, injizierbarer
``sleep`` ohne echte Wartezeit und der Aufrufervertrag "neuer Dialog je
Versuch" ueber die Versuchsnummer. Zusaetzlich wird geprueft, dass das Modul
weder Datenbank noch python-fints importiert (F-SYN-05, F-SYN-10, N-SEC-02).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from finue.services import sync_retry
from finue.services.fints_types import (
    AccessLockedError,
    FinTSBankError,
    PinWrongError,
    SyncCancelledError,
    TanTimeoutError,
)
from finue.services.sync_retry import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_WAIT_SECONDS,
    RetryOutcome,
    run_with_retry,
)


class FakeSleep:
    """Injizierbarer Ersatz fuer ``time.sleep`` ohne echte Wartezeit."""

    def __init__(self, on_sleep: Callable[[], None] | None = None) -> None:
        self.calls: list[float] = []
        self._on_sleep = on_sleep

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._on_sleep is not None:
            self._on_sleep()


class ScriptedOperation:
    """Operation mit festem Drehbuch je Versuch und Protokoll der Aufrufe."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.attempts: list[int] = []
        self.dialogs: list[object] = []

    def __call__(self, attempt: int) -> Any:
        self.attempts.append(attempt)
        # Jeder Versuch erhaelt einen neuen Dialog (Aufrufervertrag).
        self.dialogs.append(object())
        outcome = self.outcomes[attempt - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_defaults_match_spec_f_syn_06() -> None:
    assert DEFAULT_MAX_RETRIES == 2
    assert DEFAULT_WAIT_SECONDS > 0


def test_success_in_second_attempt_f_syn_06() -> None:
    first_error = FinTSBankError("9800", "Bank ist beschaeftigt")
    operation = ScriptedOperation([first_error, "saldo"])
    sleep = FakeSleep()

    outcome = run_with_retry(
        operation,
        lambda code: code == "9800",
        wait_seconds=1.5,
        sleep=sleep,
    )

    assert outcome == RetryOutcome(result="saldo", error=None, attempts=2)
    assert outcome.succeeded is True
    assert operation.attempts == [1, 2]
    assert len(operation.dialogs) == 2
    assert operation.dialogs[0] is not operation.dialogs[1]
    assert sleep.calls == [1.5]
    assert outcome.aborted is False


def test_retryable_error_exhausts_after_three_attempts_f_syn_06() -> None:
    errors = [FinTSBankError("9800", f"Versuch {number}") for number in (1, 2, 3)]
    operation = ScriptedOperation(list(errors))
    sleep = FakeSleep()
    checked_codes: list[str] = []

    def is_retryable(code: str) -> bool:
        checked_codes.append(code)
        return True

    outcome = run_with_retry(operation, is_retryable, wait_seconds=0.4, sleep=sleep)

    assert operation.attempts == [1, 2, 3]
    assert outcome.attempts == 3
    assert outcome.result is None
    assert outcome.error is errors[-1]
    assert outcome.aborted is False
    assert checked_codes == ["9800", "9800", "9800"]
    # Nur zwischen den Versuchen warten, nicht nach dem letzten Fehlschlag.
    assert sleep.calls == [0.4, 0.4]


def test_non_retryable_code_ends_immediately_f_syn_06() -> None:
    error = FinTSBankError("1234", "Unbekannter Fehler")
    operation = ScriptedOperation([error])
    sleep = FakeSleep()
    checked_codes: list[str] = []

    def is_retryable(code: str) -> bool:
        checked_codes.append(code)
        return False

    outcome = run_with_retry(operation, is_retryable, sleep=sleep)

    assert operation.attempts == [1]
    assert outcome.attempts == 1
    assert outcome.error is error
    assert checked_codes == ["1234"]
    assert sleep.calls == []


@pytest.mark.parametrize(
    "error",
    [
        PinWrongError("9340", "PIN falsch"),
        AccessLockedError("3938", "Zugang gesperrt"),
    ],
)
def test_pin_and_locked_access_are_never_retried_f_syn_06(error: FinTSBankError) -> None:
    operation = ScriptedOperation([error])
    sleep = FakeSleep()
    consulted: list[str] = []

    def is_retryable(code: str) -> bool:
        consulted.append(code)
        return True

    outcome = run_with_retry(operation, is_retryable, wait_seconds=1.0, sleep=sleep)

    assert operation.attempts == [1]
    assert outcome.attempts == 1
    assert outcome.error is error
    assert sleep.calls == []
    # Das Mapping wird fuer harte Stopps gar nicht erst befragt.
    assert consulted == []


@pytest.mark.parametrize("error", [SyncCancelledError(), TanTimeoutError()])
def test_cancel_and_timeout_stop_immediately_f_syn_10(error: Exception) -> None:
    operation = ScriptedOperation([error])
    sleep = FakeSleep()

    outcome = run_with_retry(operation, lambda code: True, wait_seconds=1.0, sleep=sleep)

    assert operation.attempts == [1]
    assert outcome.attempts == 1
    assert outcome.error is error
    assert sleep.calls == []


def test_abort_during_wait_prevents_next_attempt_f_syn_10() -> None:
    state = {"abort": False}
    operation = ScriptedOperation([FinTSBankError("9010", "Netzwerkfehler")])
    sleep = FakeSleep(on_sleep=lambda: state.update(abort=True))

    outcome = run_with_retry(
        operation,
        lambda code: True,
        wait_seconds=1.0,
        sleep=sleep,
        should_abort=lambda: state["abort"],
    )

    assert operation.attempts == [1]
    assert sleep.calls == [1.0]
    assert outcome.attempts == 1
    assert outcome.aborted is True
    assert isinstance(outcome.error, FinTSBankError)
    assert outcome.succeeded is False


def test_abort_before_first_attempt_skips_operation_f_syn_10() -> None:
    operation = ScriptedOperation([])

    outcome = run_with_retry(
        operation,
        lambda code: True,
        should_abort=lambda: True,
    )

    assert operation.attempts == []
    assert outcome.attempts == 0
    assert outcome.aborted is True
    assert isinstance(outcome.error, SyncCancelledError)


def test_zero_retries_calls_operation_once_f_syn_06() -> None:
    error = FinTSBankError("9800", "Bank ist beschaeftigt")
    operation = ScriptedOperation([error])
    sleep = FakeSleep()

    outcome = run_with_retry(operation, lambda code: True, max_retries=0, sleep=sleep)

    assert operation.attempts == [1]
    assert outcome.attempts == 1
    assert outcome.error is error
    assert sleep.calls == []


def test_none_result_is_a_success() -> None:
    operation = ScriptedOperation([None])

    outcome = run_with_retry(operation, lambda code: True, sleep=FakeSleep())

    assert outcome.succeeded is True
    assert outcome.result is None
    assert outcome.error is None
    assert outcome.attempts == 1


def test_non_bank_exception_is_not_swallowed() -> None:
    operation = ScriptedOperation([RuntimeError("Programmierfehler")])

    with pytest.raises(RuntimeError, match="Programmierfehler"):
        run_with_retry(operation, lambda code: True, sleep=FakeSleep())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_retries": -1}, "max_retries"),
        ({"wait_seconds": -1.0}, "wait_seconds"),
    ],
)
def test_invalid_arguments_rejected(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run_with_retry(
            ScriptedOperation([]),
            lambda code: True,
            sleep=FakeSleep(),
            **kwargs,
        )


def test_module_has_no_db_or_fints_dependency_f_syn_06() -> None:
    source = inspect.getsource(sync_retry)
    for forbidden in (
        "import sqlalchemy",
        "from sqlalchemy",
        "import alembic",
        "from alembic",
        "import fints",
        "from fints",
    ):
        assert forbidden not in source
