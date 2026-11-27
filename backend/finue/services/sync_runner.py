"""Hintergrund-Runner und Waechter fuer Sync-Anstoesse (T-112, D3).

Der Runner verbindet die REST-Schicht mit dem synchron blockierenden
:class:`~finue.services.sync_service.SyncService`:

- F-SYN-01/F-SYN-02, D3: Ein Anstoss beansprucht den Bankzugang sofort
  (:meth:`SyncService.claim_sync`) und fuehrt den Lauf in einem
  Hintergrund-Thread des einen Worker-Prozesses aus. Die Job-ID steht fest,
  bevor der Lauf startet; der HTTP-Aufruf kehrt sofort zurueck.
- F-SYN-11: Der zweite Anstoss desselben Zugangs erhaelt den laufenden Job
  zurueck, ohne einen zweiten Thread zu starten.
- F-SYN-04: Status, Kontoergebnisse sowie Challenge-Text und -Art einer
  offenen TAN-Abfrage liegen als Momentaufnahme im Arbeitsspeicher
  (:class:`SyncJobState`). TAN, PIN und Dialogdaten sind **nicht** Teil der
  Momentaufnahme (N-SEC-02/03); der Dialog bleibt ausschliesslich im
  :class:`~finue.services.sync_service.SyncService`.
- F-SYN-04, "5-Minuten-Timeout": Ein periodischer Waechter-Thread ruft
  :meth:`SyncService.expire_tan_timeout` und verwirft abgelaufene Dialoge.
  :meth:`SyncRunner.check_timeouts` ist die testbare Einzelpruefung.
- F-CON-04: Der Runner haengt als Listener an der Registry und uebersetzt die
  Statuswechsel in die Ereignisse ``sync.status``, ``sync.tan_required`` und
  ``sync.tan_answered``. Ein Ereignis traegt nur IDs und den Statuswert, nie
  den Challenge-Text, eine TAN oder eine PIN (N-SEC-02).

Der Runner kennt keine echten Bankzugriffe: Er nutzt denselben, injizierbaren
:class:`SyncService` wie die uebrigen Tests (D8).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from finue.services.events import EventBus, EventValue
from finue.services.sync_registry import SyncEvent, SyncJob, SyncStatus
from finue.services.sync_service import (
    AccountSyncResult,
    SyncOverallStatus,
    SyncResult,
    SyncService,
    SyncTanConflictError,
)

__all__ = ["SyncJobNotFound", "SyncJobState", "SyncRunner"]

_logger = logging.getLogger(__name__)

#: Ein Job gilt als beendet, sobald der Status kein Zwischenzustand mehr ist.
_ACTIVE_STATES: frozenset[str] = frozenset({"laeuft", "wartet_auf_tan"})

#: Fester Klartext fuer einen fehlgeschlagenen Anstoss (N-SEC-02).
_START_FAILED_MESSAGE = "Der Sync-Lauf konnte nicht gestartet werden."
_PIN_REQUIRED_MESSAGE = "PIN bitte eingeben."


class SyncJobNotFound(LookupError):
    """Die Job-ID gehoert zu keinem bekannten Sync-Job (mehr)."""


@dataclass(frozen=True, slots=True)
class SyncJobState:
    """Momentaufnahme eines Sync-Jobs fuer die REST-Schicht (F-SYN-04/07).

    ``challenge`` ist der anzuzeigende Text der offenen TAN-Abfrage,
    ``tan_kind`` ihre Art (``text`` oder ``app``). Beide sind ``None``, wenn
    keine Abfrage offen ist. Dialogdaten, TAN und PIN sind nie enthalten
    (N-SEC-02/03).
    """

    job_id: str
    bank_access_id: int
    status: SyncOverallStatus
    account_ids: tuple[int, ...]
    account_results: tuple[AccountSyncResult, ...]
    challenge: str | None
    tan_kind: str | None
    approval_polls: int
    started_at: datetime
    message: str | None = None

    @property
    def active(self) -> bool:
        """True, solange der Lauf laeuft oder auf eine TAN wartet."""

        return self.status in _ACTIVE_STATES


@dataclass(slots=True)
class _TrackedJob:
    """Laufender oder beendeter Job im Arbeitsspeicher des Runners."""

    job: SyncJob = field(repr=False)
    status: SyncOverallStatus = "laeuft"
    account_results: tuple[AccountSyncResult, ...] = ()
    challenge: str | None = None
    tan_kind: str | None = None
    approval_polls: int = 0
    message: str | None = None
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def job_id(self) -> str:
        return self.job.id


class SyncRunner:
    """Startet Sync-Laeufe im Hintergrund und haelt ihren Zustand (T-112, D3).

    Args:
        service: Der :class:`SyncService` als Singleton je App. Der Runner
            beansprucht den Zugang ueber :meth:`SyncService.claim_sync` und
            fuehrt den Lauf ueber :meth:`SyncService.start_claimed_sync`.
        event_bus: Ereignisbus fuer die Uebersetzung der Registry-Ereignisse
            (F-CON-04). Ohne Angabe werden keine Ereignisse veroeffentlicht.
        watchdog_interval: Abstand der Waechterpruefungen in Sekunden
            (Standard 5 s). :meth:`check_timeouts` prueft unabhaengig davon
            jederzeit einzeln.
        max_tracked_jobs: Hoechstzahl bekannter Jobs; aeltere beendete Jobs
            fallen heraus, laufende bleiben (Speicherbegrenzung).
    """

    def __init__(
        self,
        service: SyncService,
        *,
        event_bus: EventBus | None = None,
        watchdog_interval: float = 5.0,
        max_tracked_jobs: int = 200,
    ) -> None:
        if watchdog_interval <= 0:
            raise ValueError("watchdog_interval muss positiv sein.")
        if max_tracked_jobs < 1:
            raise ValueError("max_tracked_jobs muss mindestens 1 sein.")
        self._service = service
        self._event_bus = event_bus
        self._watchdog_interval = watchdog_interval
        self._max_tracked_jobs = max_tracked_jobs
        self._lock = threading.Lock()
        self._jobs: dict[str, _TrackedJob] = {}
        self._order: list[str] = []
        self._watchdog_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._unsubscribe = service.registry.add_listener(self._on_registry_event)

    @property
    def service(self) -> SyncService:
        """Der zugrunde liegende :class:`SyncService`."""

        return self._service

    # -- Start --------------------------------------------------------------

    def start(
        self,
        bank_access_id: int,
        *,
        account_ids: list[int] | None = None,
        pin: str | None = None,
    ) -> SyncJobState:
        """Stoesst einen Sync an und liefert sofort die Momentaufnahme.

        Laeuft fuer den Zugang bereits ein Job, wird dieser zurueckgegeben
        (F-SYN-11) und kein zweiter Thread gestartet. Die PIN bleibt nur im
        Arbeitsspeicher des Laufs (N-SEC-02); sie ist nicht Teil der
        Momentaufnahme.
        """

        job, created = self._service.claim_sync(bank_access_id, account_ids)
        if not created:
            return self._snapshot(self._existing(job))
        tracked = self._register(job)
        thread = threading.Thread(
            target=self._run_initial,
            args=(tracked, bank_access_id, account_ids, pin),
            name=f"sync-{job.id}",
            daemon=True,
        )
        tracked.thread = thread
        thread.start()
        return self._snapshot(tracked)

    # -- TAN, App-Freigabe, Abbruch -----------------------------------------

    def submit_tan(self, job_id: str, tan: str) -> SyncJobState:
        """Nimmt eine TAN an und setzt den Lauf fort (F-SYN-04).

        Die erste Antwort gewinnt; ein weiterer Aufruf loest
        :class:`SyncTanConflictError` aus (die Route antwortet 409).
        """

        tracked = self._require(job_id)
        result = self._service.submit_tan(job_id, tan)
        self._apply_result(tracked, result)
        return self._snapshot(tracked)

    def poll_app_approval(self, job_id: str) -> SyncJobState:
        """Fragt eine App-Freigabe erneut ab und meldet den Fortschritt (F-SYN-04)."""

        tracked = self._require(job_id)
        result = self._service.poll_app_approval(job_id)
        self._apply_result(tracked, result)
        return self._snapshot(tracked)

    def cancel(self, job_id: str) -> SyncJobState:
        """Bricht den Lauf ab; ein bereits beendeter Job bleibt unveraendert."""

        tracked = self._require(job_id)
        with self._lock:
            active = tracked.status in _ACTIVE_STATES
        if not active:
            return self._snapshot(tracked)
        result = self._service.cancel(job_id)
        self._apply_result(tracked, result)
        return self._snapshot(tracked)

    def get(self, job_id: str) -> SyncJobState:
        """Liefert die Momentaufnahme eines bekannten Jobs.

        Raises:
            SyncJobNotFound: Die Job-ID ist dem Runner nicht bekannt.
        """

        return self._snapshot(self._require(job_id))

    # -- Waechter -----------------------------------------------------------

    def start_watchdog(self) -> None:
        """Startet den periodischen Waechter-Thread (idempotent)."""

        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._stop.clear()
        thread = threading.Thread(target=self._watchdog_loop, name="sync-watchdog", daemon=True)
        self._watchdog_thread = thread
        thread.start()

    def stop_watchdog(self) -> None:
        """Beendet den Waechter-Thread und wartet kurz auf ihn."""

        self._stop.set()
        thread = self._watchdog_thread
        self._watchdog_thread = None
        if thread is not None:
            thread.join(timeout=max(1.0, self._watchdog_interval * 2))

    def check_timeouts(self) -> int:
        """Verwirft abgelaufene TAN-Dialoge und liefert ihre Anzahl.

        Die Pruefung nutzt die injizierbare Registry-Uhr; ohne erreichte Frist
        geschieht nichts. Sie ist zugleich die testbare Einzelpruefung des
        Waechters (F-SYN-04).
        """

        with self._lock:
            job_ids = tuple(self._jobs)
        expired = 0
        for job_id in job_ids:
            result = self._service.expire_tan_timeout(job_id)
            if result is None:
                continue
            tracked = self._jobs.get(job_id)
            if tracked is not None:
                self._apply_result(tracked, result)
                expired += 1
        return expired

    def close(self) -> None:
        """Beendet den Waechter und meldet den Registry-Listener ab."""

        self.stop_watchdog()
        self._unsubscribe()

    # -- Intern -------------------------------------------------------------

    def _run_initial(
        self,
        tracked: _TrackedJob,
        bank_access_id: int,
        account_ids: list[int] | None,
        pin: str | None,
    ) -> None:
        """Fuehrt den beanspruchten Job im Hintergrund-Thread aus (D3)."""

        try:
            result = self._service.start_claimed_sync(
                tracked.job,
                bank_access_id=bank_access_id,
                account_ids=account_ids,
                supplied_pin=pin,
            )
        except SyncTanConflictError:
            self._mark_failed(tracked, _START_FAILED_MESSAGE)
            return
        except Exception as exc:
            # Bekannte, geheimnisfreie Klartexte werden durchgereicht, alle
            # uebrigen Fehler bleiben hinter einem festen Text (N-SEC-02).
            self._mark_failed(tracked, _safe_failure_message(exc))
            return
        self._apply_result(tracked, result)

    def _mark_failed(self, tracked: _TrackedJob, message: str) -> None:
        with self._lock:
            tracked.status = "fehler"
            tracked.message = message
        _logger.info("Sync-Lauf %s ist fehlgeschlagen.", tracked.job_id)

    def _apply_result(self, tracked: _TrackedJob, result: SyncResult) -> None:
        challenge = result.challenge
        with self._lock:
            tracked.status = result.status
            tracked.account_results = result.account_results
            tracked.challenge = challenge.challenge if challenge is not None else None
            tracked.tan_kind = challenge.kind if challenge is not None else None
            tracked.approval_polls = result.approval_polls

    def _register(self, job: SyncJob) -> _TrackedJob:
        tracked = _TrackedJob(job=job)
        with self._lock:
            self._jobs[job.id] = tracked
            self._order.append(job.id)
            self._prune_locked()
        return tracked

    def _existing(self, job: SyncJob) -> _TrackedJob:
        """Liefert den bekannten Job oder spiegelt einen fremden laufenden Job."""

        with self._lock:
            tracked = self._jobs.get(job.id)
        if tracked is not None:
            return tracked
        return self._register(job)

    def _require(self, job_id: str) -> _TrackedJob:
        with self._lock:
            tracked = self._jobs.get(job_id)
        if tracked is None:
            raise SyncJobNotFound(f"Sync-Job {job_id} ist nicht bekannt.")
        return tracked

    def _prune_locked(self) -> None:
        """Entfernt aeltere beendete Jobs, bevor die Grenze ueberschritten wird."""

        if len(self._jobs) <= self._max_tracked_jobs:
            return
        for job_id in list(self._order):
            if len(self._jobs) <= self._max_tracked_jobs:
                break
            tracked = self._jobs.get(job_id)
            if tracked is None:
                self._order.remove(job_id)
                continue
            if tracked.status not in _ACTIVE_STATES:
                self._jobs.pop(job_id, None)
                self._order.remove(job_id)

    def _snapshot(self, tracked: _TrackedJob) -> SyncJobState:
        with self._lock:
            return SyncJobState(
                job_id=tracked.job.id,
                bank_access_id=tracked.job.bank_access_id,
                status=tracked.status,
                account_ids=tracked.job.account_ids,
                account_results=tracked.account_results,
                challenge=tracked.challenge,
                tan_kind=tracked.tan_kind,
                approval_polls=tracked.approval_polls,
                started_at=tracked.job.started_at,
                message=tracked.message,
            )

    # -- Ereignisse ---------------------------------------------------------

    def _on_registry_event(self, event: SyncEvent) -> None:
        """Uebersetzt einen Registry-Statuswechsel in ein Bus-Ereignis (F-CON-04)."""

        if event.status is SyncStatus.AWAITING_TAN:
            self._emit("sync.tan_required", job=event.job_id, bank_access=event.bank_access_id)
        elif event.previous_status is SyncStatus.AWAITING_TAN and event.status is SyncStatus.RUNNING:
            self._emit("sync.tan_answered", job=event.job_id, bank_access=event.bank_access_id)
        self._emit(
            "sync.status",
            job=event.job_id,
            bank_access=event.bank_access_id,
            status=event.status.value,
        )

    def _emit(self, event_type: str, **ids: EventValue) -> None:
        bus = self._event_bus
        if bus is None:
            return
        bus.emit(event_type, **ids)

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(self._watchdog_interval):
            try:
                self.check_timeouts()
            except Exception:
                _logger.exception("Sync-Waechter ist fehlgeschlagen.")


def _safe_failure_message(exc: Exception) -> str:
    """Liefert einen geheimnisfreien Klartext fuer einen Fehlstart (N-SEC-02)."""

    text = str(exc)
    if text == _PIN_REQUIRED_MESSAGE:
        return _PIN_REQUIRED_MESSAGE
    return _START_FAILED_MESSAGE
