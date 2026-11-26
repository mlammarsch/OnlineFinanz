"""Tests fuer die In-Memory-Sync-Registry.

Referenziert F-SYN-11 (hoechstens ein Sync je Bankzugang, andere Zugaenge
parallel), F-SYN-04 (TAN-Dialog, fuenf Minuten Timeout, erste Antwort gilt),
F-SYN-10 (Abbruchmarke und Freigabe) sowie N-SEC-03 (Dialogdaten nur im
Arbeitsspeicher) und N-SEC-02 (keine Dialogdaten in ``repr`` oder Ereignissen).
"""

from __future__ import annotations

import builtins
import inspect
import io
import json
import os
import pickle
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy

from finue.services import sync_registry
from finue.services.sync_registry import (
    DEFAULT_TAN_TIMEOUT,
    SyncAlreadyRunningError,
    SyncEvent,
    SyncJob,
    SyncRegistry,
    SyncStatus,
)


class FakeClock:
    """Injizierbare Uhr fuer deterministische Timeout-Tests."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _start_time() -> datetime:
    return datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def test_same_access_gets_running_job_f_syn_11() -> None:
    clock = FakeClock(_start_time())
    registry = SyncRegistry(clock=clock)

    first = registry.start_or_get(17, (101, 102))
    assert first.bank_access_id == 17
    assert first.account_ids == (101, 102)
    assert first.started_at == _start_time()
    assert first.status is SyncStatus.RUNNING
    assert first.deadline is None
    assert registry.get(first.id) is first
    assert registry.is_active(17) is True

    second = registry.start_or_get(17, (999,))
    assert second is first
    assert second.account_ids == (101, 102)
    assert len({second.id, first.id}) == 1


def test_different_accesses_run_in_parallel_f_syn_11() -> None:
    registry = SyncRegistry()
    events: list[SyncEvent] = []
    registry.add_listener(events.append)

    first = registry.start_or_get(1, (10,))
    second = registry.start_or_get(2, (20,))

    assert first is not second
    assert first.id != second.id
    assert registry.get(first.id) is first
    assert registry.get(second.id) is second
    assert registry.is_active(1) is True
    assert registry.is_active(2) is True
    assert [event.job_id for event in events] == [first.id, second.id]


def test_concurrent_threads_create_single_job_per_access_f_syn_11() -> None:
    registry = SyncRegistry()
    events: list[SyncEvent] = []
    registry.add_listener(events.append)
    barrier = threading.Barrier(8)
    results: list[SyncJob] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=2)
            results.append(registry.start_or_get(42, (7,)))
        except BaseException as exc:  # Test sammelt jeden Thread-Fehler ein.
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert len(results) == 8
    assert len({job.id for job in results}) == 1
    assert all(job is results[0] for job in results)
    assert [event.status for event in events] == [SyncStatus.RUNNING]


def test_run_releases_lock_on_exception_f_syn_11() -> None:
    registry = SyncRegistry()
    events: list[SyncEvent] = []
    registry.add_listener(events.append)

    with pytest.raises(RuntimeError, match="abgebrochen"):
        with registry.run(23, (1,)) as job:
            registry.mark_awaiting_tan(job.id, {"challenge": "geheim"})
            raise RuntimeError("abgebrochen")

    assert job.status is SyncStatus.FAILED
    assert job.dialog_state is None
    assert registry.get(job.id) is None
    assert registry.is_active(23) is False
    assert [event.status for event in events] == [
        SyncStatus.RUNNING,
        SyncStatus.AWAITING_TAN,
        SyncStatus.FAILED,
    ]

    new_job = registry.start_or_get(23)
    assert new_job is not job


def test_run_completes_and_keeps_second_owner_out_f_syn_11() -> None:
    registry = SyncRegistry()

    with registry.run(5) as job:
        assert job.status is SyncStatus.RUNNING
        assert registry.is_active(5) is True

    assert job.status is SyncStatus.COMPLETED
    assert registry.is_active(5) is False

    running = registry.start_or_get(5)
    with pytest.raises(SyncAlreadyRunningError) as caught:
        with registry.run(5):
            pass
    assert caught.value.job_id == running.id
    assert running.status is SyncStatus.RUNNING
    assert registry.get(running.id) is running


def test_tan_timeout_uses_injected_clock_f_syn_04() -> None:
    assert DEFAULT_TAN_TIMEOUT == timedelta(minutes=5)
    clock = FakeClock(_start_time())
    registry = SyncRegistry(clock=clock)
    events: list[SyncEvent] = []
    registry.add_listener(events.append)

    job = registry.start_or_get(9)
    assert job.deadline is None
    registry.mark_awaiting_tan(job.id, {"challenge": "Bitte TAN"})
    assert job.status is SyncStatus.AWAITING_TAN
    assert job.deadline == _start_time() + timedelta(minutes=5)

    clock.advance(timedelta(minutes=4, seconds=59))
    assert registry.expire_if_timed_out(job.id) is False
    assert job.status is SyncStatus.AWAITING_TAN

    clock.advance(timedelta(seconds=1))
    assert registry.expire_if_timed_out(job.id) is True
    assert job.status is SyncStatus.TIMED_OUT
    assert job.dialog_state is None
    assert registry.get(job.id) is None
    assert events[-1].previous_status is SyncStatus.AWAITING_TAN
    assert events[-1].status is SyncStatus.TIMED_OUT

    # Nach dem Timeout startet derselbe Zugang einen neuen Job.
    assert registry.start_or_get(9) is not job


def test_answer_after_deadline_times_out_f_syn_04() -> None:
    clock = FakeClock(_start_time())
    registry = SyncRegistry(clock=clock)

    job = registry.start_or_get(10)
    registry.mark_awaiting_tan(job.id, {"challenge": "Bitte TAN"})
    clock.advance(timedelta(minutes=5))

    assert registry.answer_tan(job.id) is False
    assert job.status is SyncStatus.TIMED_OUT
    assert job.dialog_state is None


def test_first_tan_answer_wins_f_syn_04() -> None:
    registry = SyncRegistry()
    events: list[SyncEvent] = []
    registry.add_listener(events.append)

    job = registry.start_or_get(11)
    registry.mark_awaiting_tan(job.id, {"challenge": "Bitte TAN"})

    assert registry.answer_tan(job.id) is True
    assert job.status is SyncStatus.RUNNING
    assert job.deadline is None
    assert registry.answer_tan(job.id) is False
    assert job.status is SyncStatus.RUNNING

    resumed = [
        event
        for event in events
        if event.previous_status is SyncStatus.AWAITING_TAN and event.status is SyncStatus.RUNNING
    ]
    assert len(resumed) == 1

    registry.clear_dialog_state(job.id)
    assert job.dialog_state is None


def test_abort_marker_and_abort_status_f_syn_10() -> None:
    registry = SyncRegistry()

    job = registry.start_or_get(12)
    assert registry.request_abort(job.id) is True
    assert job.abort_requested is True
    assert registry.request_abort(job.id) is False

    assert registry.abort(job.id) is True
    assert job.status is SyncStatus.ABORTED
    assert job.dialog_state is None
    assert registry.is_active(12) is False
    assert registry.start_or_get(12) is not job


def test_expire_finishes_job_as_timed_out_and_is_idempotent_f_syn_10() -> None:
    """F-SYN-10: ``expire`` gibt die Sperre frei und wiederholt sich folgenlos."""

    registry = SyncRegistry()

    job = registry.start_or_get(13)
    assert job.status is SyncStatus.RUNNING
    assert registry.expire(job.id) is True
    assert job.status is SyncStatus.TIMED_OUT
    assert job.dialog_state is None
    assert registry.is_active(13) is False
    assert registry.expire(job.id) is False


def test_terminal_transitions_clear_dialog_state_n_sec_03() -> None:
    registry = SyncRegistry()

    for access_id, finish in (
        (31, registry.complete),
        (32, registry.fail),
        (33, registry.abort),
    ):
        job = registry.start_or_get(access_id)
        registry.mark_awaiting_tan(job.id, {"secret": "dialog-daten"})
        assert finish(job.id) is True
        assert job.dialog_state is None


def test_listeners_are_synchronous_and_unsubscribable() -> None:
    registry = SyncRegistry()
    seen: list[SyncEvent] = []

    def listener(event: SyncEvent) -> None:
        seen.append(event)

    unsubscribe = registry.add_listener(listener)
    job = registry.start_or_get(13)
    assert len(seen) == 1
    assert seen[0].status is SyncStatus.RUNNING
    assert seen[0].previous_status is None

    registry.complete(job.id)
    assert seen[-1].status is SyncStatus.COMPLETED
    assert seen[-1].previous_status is SyncStatus.RUNNING

    unsubscribe()
    second = registry.start_or_get(13)
    registry.complete(second.id)
    assert len(seen) == 2


def test_broken_listener_does_not_block_release() -> None:
    registry = SyncRegistry()
    seen: list[SyncEvent] = []

    def broken(_event: SyncEvent) -> None:
        raise RuntimeError("Listener kaputt")

    registry.add_listener(broken)
    registry.add_listener(seen.append)

    job = registry.start_or_get(14)
    assert job.status is SyncStatus.RUNNING
    assert len(seen) == 1
    assert registry.complete(job.id) is True
    assert registry.is_active(14) is False


def test_dialog_state_never_reaches_disk_db_or_serializer_n_sec_03(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "TAN-4711-PIN-0815"
    calls: list[str] = []

    def blocker(name: str) -> Any:
        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"unerlaubter Persistenzzugriff: {name}")

        return blocked

    registry = SyncRegistry()
    events: list[SyncEvent] = []
    registry.add_listener(events.append)
    job_repr = ""
    job_str = ""
    kept_dialog = False

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", blocker("builtins.open"))
        patch.setattr(io, "open", blocker("io.open"))
        patch.setattr(os, "open", blocker("os.open"))
        patch.setattr(sqlite3, "connect", blocker("sqlite3.connect"))
        patch.setattr(sqlalchemy, "create_engine", blocker("sqlalchemy.create_engine"))
        patch.setattr(pickle, "dump", blocker("pickle.dump"))
        patch.setattr(pickle, "dumps", blocker("pickle.dumps"))
        patch.setattr(json, "dump", blocker("json.dump"))
        patch.setattr(json, "dumps", blocker("json.dumps"))

        job = registry.start_or_get(15, (1, 2))
        registry.mark_awaiting_tan(job.id, {"challenge": secret, "pin": secret, "tan": secret})
        assert registry.answer_tan(job.id) is True
        assert registry.request_abort(job.id) is True
        kept_dialog = job.dialog_state is not None
        job_repr = repr(job)
        job_str = str(job)
        registry.abort(job.id)

    assert calls == []
    assert kept_dialog is True
    assert secret not in job_repr
    assert secret not in job_str
    assert secret not in repr(events)
    assert "dialog_state" not in job_repr

    # Auch ohne aktiven Dialog verweigert der Job die Serialisierung.
    with pytest.raises(TypeError, match="serialisiert"):
        pickle.dumps(job)


def test_module_has_no_persistence_imports_n_sec_03() -> None:
    source = inspect.getsource(sync_registry)
    for forbidden in (
        "import json",
        "from json",
        "import pickle",
        "from pickle",
        "import sqlite3",
        "import sqlalchemy",
        "from sqlalchemy",
        "open(",
    ):
        assert forbidden not in source
