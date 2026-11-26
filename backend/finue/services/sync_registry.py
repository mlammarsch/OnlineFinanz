"""Threadsichere In-Memory-Registry fuer laufende FinTS-Sync-Jobs.

Die Registry setzt F-SYN-11 um: je Bankzugang laeuft hoechstens ein Sync.
Ein zweiter Anstoss erhaelt den laufenden Job zurueck, waehrend andere
Bankzugaenge parallel laufen. Ein pausierter TAN-Dialog haelt die Sperre des
Zugangs, bis der Job abgeschlossen, abgebrochen oder nach fuenf Minuten
abgelaufen ist (F-SYN-04, F-SYN-10, D3).

Dialogdaten liegen ausschliesslich als opake Struktur im Arbeitsspeicher
(N-SEC-03). Das Modul kennt keine Datenbank, keine Datei- und keine
Serialisierungswege; ``SyncJob`` verweigert zusaetzlich das Pickling. Die
``repr``-Darstellung enthaelt keine Dialogdaten (N-SEC-02).

Statuswechsel werden synchron an registrierte Listener gemeldet. Die
Listener laufen ohne Netzwerkzugriff und ausserhalb des Registry-Locks,
damit M4 daraus SSE-Ereignisse bauen kann. Die erste TAN-Antwort gewinnt:
``answer_tan`` wechselt den Job genau einmal von ``awaiting_tan`` nach
``running``; weitere Aufrufe liefern ``False`` (F-SYN-04).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Final, SupportsIndex

__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_TAN_TIMEOUT",
    "SyncAlreadyRunningError",
    "SyncEvent",
    "SyncEventListener",
    "SyncJob",
    "SyncJobNotFoundError",
    "SyncRegistry",
    "SyncRegistryError",
    "SyncStatus",
]

_logger = logging.getLogger(__name__)

#: TAN-Dialoge laufen nach fuenf Minuten ab (F-SYN-04, D3).
DEFAULT_TAN_TIMEOUT: Final[timedelta] = timedelta(minutes=5)


class SyncStatus(StrEnum):
    """Lebenszyklus eines Sync-Jobs."""

    RUNNING = "running"
    AWAITING_TAN = "awaiting_tan"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    TIMED_OUT = "timed_out"


ACTIVE_STATUSES: Final[frozenset[SyncStatus]] = frozenset({SyncStatus.RUNNING, SyncStatus.AWAITING_TAN})


class SyncRegistryError(RuntimeError):
    """Basisfehler der Sync-Registry."""


class SyncJobNotFoundError(LookupError):
    """Die Job-ID gehoert zu keinem aktiven Job."""


class SyncAlreadyRunningError(SyncRegistryError):
    """Fuer den Bankzugang laeuft bereits ein anderer Sync-Job."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Fuer diesen Bankzugang laeuft bereits Sync-Job {job_id}.")


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """Statuswechsel eines Sync-Jobs fuer synchrone Listener.

    ``challenge`` und ``tan_kind`` tragen bei ``AWAITING_TAN`` den
    anzuzeigenden Challenge-Text und die Art der Freigabe. Damit erreicht die
    TAN-Abfrage alle angemeldeten Sitzungen ueber denselben Registry-Hook
    (F-SYN-04). Der Text ist kein Geheimnis; TAN und Dialogdaten bleiben
    ausschliesslich im Arbeitsspeicher (N-SEC-02/03).
    """

    job_id: str
    bank_access_id: int
    previous_status: SyncStatus | None
    status: SyncStatus
    occurred_at: datetime
    challenge: str | None = None
    tan_kind: str | None = None


SyncEventListener = Callable[[SyncEvent], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True, eq=False, repr=False)
class SyncJob:
    """Zustand eines Sync-Jobs, ausschliesslich im Arbeitsspeicher.

    ``dialog_state`` ist eine opake Struktur (zum Beispiel der dekonstruierte
    python-fints-Dialog). Sie wird bewusst nicht in ``repr`` ausgegeben und
    nicht verglichen. Der Job ist nicht serialisierbar (N-SEC-03).
    """

    id: str
    bank_access_id: int
    account_ids: tuple[int, ...]
    started_at: datetime
    status: SyncStatus = SyncStatus.RUNNING
    deadline: datetime | None = None
    challenge: str | None = field(default=None, repr=False, compare=False)
    tan_kind: str | None = field(default=None, repr=False, compare=False)
    dialog_state: object | None = field(default=None, repr=False, compare=False)
    _abort_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False, compare=False)

    @property
    def abort_requested(self) -> bool:
        """Abbruchmarke, die der Sync-Lauf regelmaessig prueft (F-SYN-10)."""

        return self._abort_event.is_set()

    def wait_for_abort(self, timeout: float) -> bool:
        """Wartet auf die Abbruchmarke und meldet, ob sie gesetzt wurde.

        ``Event.wait`` statt eines blockierenden ``sleep``: Ein Abbruch
        unterbricht die Wartezeit zwischen zwei Wiederholungen sofort
        (F-SYN-10, T-070-Hinweis).
        """

        return self._abort_event.wait(timeout)

    def __repr__(self) -> str:
        deadline = None if self.deadline is None else self.deadline.isoformat()
        return (
            f"SyncJob(id={self.id!r}, bank_access_id={self.bank_access_id!r}, "
            f"status={self.status.value!r}, account_ids={self.account_ids!r}, "
            f"started_at={self.started_at.isoformat()!r}, deadline={deadline!r}, "
            f"abort_requested={self.abort_requested!r})"
        )

    __str__ = __repr__

    def __reduce_ex__(self, _protocol: SupportsIndex) -> str | tuple[Any, ...]:
        raise TypeError("SyncJob enthaelt Dialogdaten und darf nicht serialisiert werden.")


class SyncRegistry:
    """Threadsichere Verwaltung aktiver Sync-Jobs je Bankzugang."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        tan_timeout: timedelta = DEFAULT_TAN_TIMEOUT,
    ) -> None:
        if tan_timeout <= timedelta(0):
            raise ValueError("tan_timeout muss positiv sein.")
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._tan_timeout = tan_timeout
        self._lock = threading.Lock()
        self._active: dict[int, SyncJob] = {}
        self._listeners: list[SyncEventListener] = []

    def add_listener(self, listener: SyncEventListener) -> Callable[[], None]:
        """Registriert einen synchronen Listener und liefert die Abmeldung."""

        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            self.remove_listener(listener)

        return unsubscribe

    def remove_listener(self, listener: SyncEventListener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def start_or_get(self, bank_access_id: int, account_ids: Iterable[int] = ()) -> SyncJob:
        """Liefert den laufenden Job des Zugangs oder startet einen neuen.

        Die Pruefung und das Einfuegen laufen unter demselben Lock. Damit
        erhalten zwei gleichzeitige Anstosse fuer denselben Zugang denselben
        Job (F-SYN-11); unterschiedliche Zugaenge blockieren sich nicht.
        """

        job, _created = self.try_start(bank_access_id, account_ids)
        return job

    def try_start(self, bank_access_id: int, account_ids: Iterable[int] = ()) -> tuple[SyncJob, bool]:
        """Wie :meth:`start_or_get`, meldet aber zusaetzlich ``created``."""

        accounts = tuple(dict.fromkeys(account_ids))
        now = self._clock()
        events: list[SyncEvent] = []
        with self._lock:
            existing = self._active.get(bank_access_id)
            if (
                existing is not None
                and existing.status is SyncStatus.AWAITING_TAN
                and self._deadline_reached(existing, now)
            ):
                events.append(self._finish_locked(existing, SyncStatus.TIMED_OUT, now))
                existing = None
            if existing is not None and existing.status in ACTIVE_STATUSES:
                job = existing
                created = False
            else:
                job = SyncJob(
                    id=uuid.uuid4().hex,
                    bank_access_id=bank_access_id,
                    account_ids=accounts,
                    started_at=now,
                )
                self._active[bank_access_id] = job
                events.append(
                    SyncEvent(
                        job_id=job.id,
                        bank_access_id=job.bank_access_id,
                        previous_status=None,
                        status=job.status,
                        occurred_at=now,
                    )
                )
                created = True
        for event in events:
            self._emit(event)
        return job, created

    def get(self, job_id: str) -> SyncJob | None:
        """Liefert den aktiven Job zur ID oder ``None``."""

        with self._lock:
            return self._find_active_locked(job_id)

    def is_active(self, bank_access_id: int) -> bool:
        with self._lock:
            job = self._active.get(bank_access_id)
            return job is not None and job.status in ACTIVE_STATUSES

    def mark_awaiting_tan(
        self,
        job_id: str,
        dialog_state: object,
        *,
        challenge: str | None = None,
        tan_kind: str | None = None,
        timeout: timedelta | None = None,
    ) -> bool:
        """Pausiert den Job fuer eine TAN-Abfrage (F-SYN-04).

        ``dialog_state`` bleibt nur im Arbeitsspeicher. ``challenge`` und
        ``tan_kind`` machen die Abfrage fuer alle Sitzungen lesbar; der Text
        ist kein Geheimnis (N-SEC-02). Die Frist beginnt jetzt; ohne eigenen
        Wert gelten fuenf Minuten. Die Zugangssperre bleibt bis zum
        Abschluss, Abbruch oder Timeout bestehen.
        """

        wait = self._tan_timeout if timeout is None else timeout
        if wait <= timedelta(0):
            raise ValueError("timeout muss positiv sein.")
        now = self._clock()
        with self._lock:
            job = self._require_active_locked(job_id)
            if job.status is not SyncStatus.RUNNING:
                raise SyncRegistryError(f"Job {job_id} wartet nicht auf einen TAN-Dialog.")
            previous = job.status
            job.status = SyncStatus.AWAITING_TAN
            job.dialog_state = dialog_state
            job.challenge = challenge
            job.tan_kind = tan_kind
            job.deadline = now + wait
            event = self._event(job, previous, now)
        self._emit(event)
        return True

    def answer_tan(self, job_id: str) -> bool:
        """Nimmt die erste TAN-Antwort an; weitere Antworten verlieren.

        Der Dialogzustand bleibt fuer die Wiederaufnahme erhalten, bis der
        Aufrufer ihn mit :meth:`clear_dialog_state` entfernt. Ist die Frist
        bereits abgelaufen, wird der Job auf ``timed_out`` gesetzt und die
        Antwort abgelehnt (F-SYN-04, F-SYN-10).
        """

        now = self._clock()
        accepted = False
        event: SyncEvent | None = None
        with self._lock:
            job = self._require_active_locked(job_id)
            if job.status is SyncStatus.AWAITING_TAN:
                if self._deadline_reached(job, now):
                    event = self._finish_locked(job, SyncStatus.TIMED_OUT, now)
                else:
                    previous = job.status
                    job.status = SyncStatus.RUNNING
                    job.deadline = None
                    job.challenge = None
                    job.tan_kind = None
                    event = self._event(job, previous, now)
                    accepted = True
        if event is not None:
            self._emit(event)
        return accepted

    def expire_if_timed_out(self, job_id: str) -> bool:
        """Beendet einen abgelaufenen TAN-Dialog und liefert ``True``."""

        now = self._clock()
        with self._lock:
            job = self._require_active_locked(job_id)
            if job.status is not SyncStatus.AWAITING_TAN:
                return False
            if not self._deadline_reached(job, now):
                return False
            event = self._finish_locked(job, SyncStatus.TIMED_OUT, now)
        self._emit(event)
        return True

    def clear_dialog_state(self, job_id: str) -> None:
        """Verwirft die Dialogdaten im Arbeitsspeicher (N-SEC-03)."""

        with self._lock:
            job = self._require_active_locked(job_id)
            job.dialog_state = None

    def request_abort(self, job_id: str) -> bool:
        """Setzt die Abbruchmarke; der laufende Sync pollt sie (F-SYN-10)."""

        with self._lock:
            job = self._require_active_locked(job_id)
            if job.abort_requested:
                return False
            job._abort_event.set()
            return True

    def complete(self, job_id: str) -> bool:
        return self._finish(job_id, SyncStatus.COMPLETED)

    def fail(self, job_id: str) -> bool:
        return self._finish(job_id, SyncStatus.FAILED)

    def abort(self, job_id: str) -> bool:
        return self._finish(job_id, SyncStatus.ABORTED)

    def expire(self, job_id: str) -> bool:
        """Beendet den Job als zeitueberschritten und gibt die Sperre frei.

        Idempotent: Ist der Job bereits in einem Endzustand, bleibt dieser
        erhalten. Gedacht fuer den Abschlusspfad eines Timeouts, der nicht
        ueber :meth:`expire_if_timed_out` gelaufen ist (F-SYN-10).
        """

        return self._finish(job_id, SyncStatus.TIMED_OUT)

    def release(self, job_id: str) -> bool:
        """Gibt die Zugangssperre frei, ohne den Job als erfolgreich zu werten."""

        return self._finish(job_id, SyncStatus.ABORTED)

    @contextmanager
    def run(self, bank_access_id: int, account_ids: Iterable[int] = ()) -> Iterator[SyncJob]:
        """Rahmt einen eigenen Sync-Lauf und gibt die Sperre immer frei.

        Der Kontext ist nur fuer den Erststarter gedacht. Laeuft bereits ein
        Job, wird :class:`SyncAlreadyRunningError` geworfen; der Aufrufer
        nutzt dann :meth:`start_or_get`, um den bestehenden Job zu erhalten.
        Bei einer Exception wird der Job ``failed``, sonst ``completed``.
        Das ``finally`` gibt die Sperre auch dann frei, wenn die
        Statusmeldung selbst fehlschlaegt (F-SYN-11).
        """

        job, created = self.try_start(bank_access_id, account_ids)
        if not created:
            raise SyncAlreadyRunningError(job.id)
        try:
            yield job
        except BaseException:
            self.fail(job.id)
            raise
        else:
            self.complete(job.id)
        finally:
            self.release(job.id)

    def _finish(self, job_id: str, status: SyncStatus) -> bool:
        now = self._clock()
        with self._lock:
            job = self._find_active_locked(job_id)
            if job is None or job.status not in ACTIVE_STATUSES:
                return False
            event = self._finish_locked(job, status, now)
        self._emit(event)
        return True

    def _finish_locked(self, job: SyncJob, status: SyncStatus, now: datetime) -> SyncEvent:
        previous = job.status
        job.status = status
        job.dialog_state = None
        job.deadline = None
        job.challenge = None
        job.tan_kind = None
        if self._active.get(job.bank_access_id) is job:
            del self._active[job.bank_access_id]
        return self._event(job, previous, now)

    def _event(self, job: SyncJob, previous: SyncStatus | None, now: datetime) -> SyncEvent:
        return SyncEvent(
            job_id=job.id,
            bank_access_id=job.bank_access_id,
            previous_status=previous,
            status=job.status,
            occurred_at=now,
            challenge=job.challenge,
            tan_kind=job.tan_kind,
        )

    def _deadline_reached(self, job: SyncJob, now: datetime) -> bool:
        return job.deadline is not None and now >= job.deadline

    def _find_active_locked(self, job_id: str) -> SyncJob | None:
        for job in self._active.values():
            if job.id == job_id:
                return job
        return None

    def _require_active_locked(self, job_id: str) -> SyncJob:
        job = self._find_active_locked(job_id)
        if job is None:
            raise SyncJobNotFoundError(f"Sync-Job {job_id} ist nicht aktiv.")
        return job

    def _emit(self, event: SyncEvent) -> None:
        # Snapshot unter Lock, Aufruf ausserhalb: Listener duerfen die
        # Registry erneut betreten, ohne zu verklemmen. Ein fehlerhafter
        # Listener darf den Sync-Lauf nicht stoppen (F-SYN-04).
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                _logger.exception("Synchroner Sync-Listener ist fehlgeschlagen.")
