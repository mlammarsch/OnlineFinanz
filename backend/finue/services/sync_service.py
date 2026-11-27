"""Orchestrierung des lesenden FinTS-Syncs fuer einen Bankzugang.

Der Service verbindet die Bausteine aus M3 zu einem Batch- oder Einzel-Sync:

- F-SYN-01: Ein Batch startet fuer alle aktiven FinTS-Konten eines Zugangs und
  nutzt dafuer genau einen Dialog. Passen nicht alle Konten in einen Lauf,
  bricht nur das betroffene Konto ab.
- F-SYN-02: Ein Einzel-Sync uebergibt genau die gewaehlten Konten.
- F-SYN-11: Pro Zugang laeuft hoechstens ein Job. Ein zweiter Anstoss erhaelt
  den laufenden :class:`~finue.services.sync_registry.SyncJob` zurueck, statt
  einen zweiten Lauf zu starten. Die Sperre haelt bis zum Ende des Laufs.
- F-SYN-03: Der Abrufzeitraum je Konto stammt aus
  :func:`finue.services.sync_persist.compute_fetch_range`; der erste Sync
  uebergibt ``None`` und laesst die Bank so weit liefern, wie sie kann.
- F-SYN-04, D3: Verlangt die Bank eine TAN oder App-Freigabe, friert der
  Adapter den Dialog ein (:class:`~finue.services.fints_client.TanHandoff`).
  Der Zustand liegt ausschliesslich im Arbeitsspeicher der Registry
  (N-SEC-03). Der Challenge-Text erreicht ueber den Registry-Hook alle
  Sitzungen; die erste Antwort gewinnt, weitere erhalten einen definierten
  Konflikt. :meth:`SyncService.submit_tan` setzt per
  ``from_data``/``resume_dialog``/``send_tan`` fort,
  :meth:`SyncService.poll_app_approval` fragt eine App-Freigabe erneut ab.
  Nach fuenf Minuten ohne Antwort wird der Dialog verworfen und der Lauf als
  ``zeitueberschreitung`` protokolliert; die Uhr ist injizierbar.
- F-SYN-05/F-SYN-06: Bankfehler werden ueber
  :func:`finue.services.bank_errors.resolve_error` auf Wiederholbarkeit
  geprueft. Wiederholbare Codes laufen mit hoechstens zwei weiteren Versuchen
  und einem frischen Dialog je Versuch. PIN-Fehler, Sperre, Abbruch und
  Timeout werden nie wiederholt.
- F-SYN-07: Je Konto schreibt :mod:`finue.services.sync_persist` genau einen
  ImportRun mit gemeinsamem ``job_id`` im Batch. Waehrend einer TAN-Abfrage
  steht die Zeile des betroffenen Kontos auf ``wartet_auf_tan``; nach der
  Freigabe wird dieselbe Zeile fortgeschrieben.
- F-SYN-10: Jedes Konto wird in einer eigenen Transaktion uebernommen. Ein
  Fehler eines Kontos stoppt die uebrigen Konten nicht. Bei Abbruch bleiben
  vollstaendig uebernommene Konten erhalten; vom laufenden Konto werden keine
  Kontodaten gespeichert, nur der Abbruch protokolliert.
- F-AUF-04/F-AUF-05: Der Auftragsabruf wird nach jedem erfolgreichen Abruf
  in derselben Konto-Transaktion ueber
  :func:`finue.services.standing_orders.replace_standing_orders` gespeichert.
  ``empty`` loescht den Bestand, ``unsupported`` leert ihn ebenfalls und
  liefert spaeter ``unavailable``. Beide Zustaende sind kein Sync-Fehler; der
  Kontostatus bleibt ``erfolgreich``.
- N-SEC-02/N-SEC-03: PIN, TAN und Dialogdaten bleiben ausschliesslich im
  Arbeitsspeicher. Der Service loggt weder PIN noch TAN noch Banktexte mit
  Geheimnissen. Fehler und Protokollzeilen enthalten nur feste Klartexte.
- N-DB-04: An der TAN-Pause (``AWAITING_TAN``, Text- und App-Freigabe)
  schliesst der Lauf die DB-Session; der Verbindungspool haelt dann keine
  Verbindung und parallele Schreibvorgaenge laufen ohne Sperrwartezeit
  (SQLite WAL mit ``busy_timeout``). Nach der Freigabe oeffnet der Lauf eine
  frische Session und laedt die benoetigten Objekte ueber ihre IDs. Zwischen
  den Schritten traegt der Laufzustand nur IDs und Werte, kein ORM-Objekt.
  Der pausierte Dialog bleibt nur im Arbeitsspeicher (N-SEC-03), die
  Fuenf-Minuten-Frist bleibt unveraendert; Abbruch und Timeout geben Session
  und Dialog frei.

Der Service kennt keine echten Bankzugriffe: Der
:class:`~finue.services.fints_client.FinTSGateway` erhaelt eine injizierbare
Client-Fabrik. In Tests kommt ausschliesslich der Fake-Client aus
``tests/fixtures/fints_fake.py`` zum Einsatz (D8).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal, TypeVar, cast

from fints.models import SEPAAccount  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.config import Settings
from finue.models.account import Account
from finue.models.bank import BankAccess
from finue.models.import_run import ImportRun
from finue.services.duplicate import TransactionData
from finue.services.bank_access import resolve_pin
from finue.services.bank_errors import resolve_error
from finue.services.fints_client import (
    FinTSClientProtocol,
    FinTSGateway,
    TanHandoff,
    TanRequired,
    TransactionFetch,
)
from finue.services.fints_types import (
    BalanceResult,
    FetchedTransaction,
    FinTSBankError,
    OrdersResult,
    PinWrongError,
    SyncAccountData,
    SyncCancelledError,
    TanChallenge,
    TanTimeoutError,
    TransactionStatus,
)
from finue.services.pin_crypto import PinCipher
from finue.services.standing_orders import replace_standing_orders
from finue.services.sync_persist import (
    compute_fetch_range,
    persist_account_sync,
    persist_sync_failure,
    persist_waiting_for_tan,
)
from finue.services.sync_registry import SyncJob, SyncRegistry, SyncStatus
from finue.services.sync_retry import DEFAULT_MAX_RETRIES, DEFAULT_WAIT_SECONDS
from finue.timeutil import utc_now

__all__ = [
    "AccountRunStatus",
    "AccountSyncResult",
    "SyncAccountNotInBankError",
    "SyncConfigurationError",
    "SyncOverallStatus",
    "SyncResult",
    "SyncService",
    "SyncTanConflictError",
]

#: Session-Fabrik. Ein neuer Sync erhaelt eine eigene Session, damit ein
#: fehlgeschlagenes Konto nach dem Rollback die anderen nicht blockiert.
SessionFactory = Callable[[], Session]

#: Ergebnis eines einzelnen Kontos nach F-SYN-07.
AccountRunStatus = Literal["erfolgreich", "fehler", "abgebrochen", "zeitueberschreitung"]
#: Gesamtstatus eines Laufs. ``laeuft`` entsteht beim zweiten Anstoss,
#: ``wartet_auf_tan`` bei einer offenen TAN-Abfrage (F-SYN-04).
SyncOverallStatus = Literal[
    "laeuft",
    "wartet_auf_tan",
    "erfolgreich",
    "teilweise",
    "fehler",
    "abgebrochen",
    "zeitueberschreitung",
]

_ACCOUNT_STATUSES: Final[frozenset[str]] = frozenset({"erfolgreich", "fehler", "abgebrochen", "zeitueberschreitung"})

_ResultT = TypeVar("_ResultT")


class SyncConfigurationError(RuntimeError):
    """Der Sync kann mit den vorhandenen Daten nicht gestartet werden."""


class SyncTanConflictError(RuntimeError):
    """Die TAN-Antwort kann nicht angenommen werden (F-SYN-04).

    Der Fehler enthaelt bewusst nur einen festen Klartext, nie die
    abgelehnte TAN (N-SEC-02).
    """

    _safe_message = "Die TAN-Antwort kann nicht angenommen werden."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self._safe_message)

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self._safe_message


class SyncAccountNotInBankError(FinTSBankError):
    """Die Bank hat ein aktives Konto des Zugangs nicht geliefert.

    Der Fehler ist ein :class:`~finue.services.fints_types.FinTSBankError`,
    damit die Protokollierung in
    :func:`finue.services.sync_persist.persist_sync_failure` denselben Pfad
    wie ein echter Bankfehler nutzt (F-SYN-05, F-SYN-07).
    """

    def __init__(self, account_id: int, iban: str | None) -> None:
        label = iban if iban else "ohne IBAN"
        super().__init__(
            "KONTO",
            f"Konto {account_id} ({label}) wurde von der Bank nicht geliefert.",
        )


@dataclass(frozen=True, slots=True)
class AccountSyncResult:
    """Ergebnis eines Kontos innerhalb eines Sync-Laufs (F-SYN-07).

    ``import_run_id`` verweist auf die persistierte Protokollzeile. ``message``
    ist der Klartext aus dem Fehler-Mapping bzw. die Erfolgsmeldung, nie ein
    Geheimnis (N-SEC-02).
    """

    account_id: int
    status: AccountRunStatus
    import_run_id: int
    attempts: int
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Ergebnis von :meth:`SyncService.start_sync` (F-SYN-01, F-SYN-04/07).

    ``reused`` ist ``True``, wenn ein bereits laufender Job zurueckgegeben
    wurde (F-SYN-11). ``challenge`` traegt bei ``wartet_auf_tan`` den
    anzuzeigenden Text. ``approval_polls`` zaehlt die App-Freigabe-Abfragen
    dieses Laufs (F-SYN-04).
    """

    job: SyncJob
    reused: bool
    status: SyncOverallStatus
    account_results: tuple[AccountSyncResult, ...] = ()
    challenge: TanChallenge | None = None
    approval_polls: int = 0

    @property
    def job_id(self) -> str:
        """Kennung des zugrunde liegenden Registry-Jobs."""
        return self.job.id


@dataclass(slots=True)
class _RunContext:
    """Laufzustand waehrend einer TAN-Pause, nur im Arbeitsspeicher.

    Der :class:`~finue.services.fints_client.TanHandoff` enthaelt Dialog- und
    Clientzustand und wird nie serialisiert (N-SEC-03). ``session`` ist nur
    waehrend eines laufenden Schritts gesetzt und an der TAN-Pause ``None``
    (N-DB-04): Vor dem Pausieren schliesst der Lauf die Session, nach der
    Freigabe oeffnet er eine frische. Der Kontext traegt darueber hinaus nur
    IDs und Werte, kein ORM-Objekt.
    """

    job: SyncJob
    bank_access_id: int
    requested_ids: tuple[int, ...] | None
    job_group_id: str | None
    generator: Generator[TanHandoff[Any], str | None, None] | None = None
    session: Session | None = None
    results: list[AccountSyncResult] = field(default_factory=list)
    account_ids: tuple[int, ...] = ()
    index: int = 0
    current_account_id: int | None = None
    waiting_run_id: int | None = None
    blz: str | None = None
    handoff: TanHandoff[Any] | None = None
    challenge: TanChallenge | None = None
    busy: bool = False
    approval_polls: int = 0


class _GatewayHolder:
    """Haelt den Dialog eines Laufs und oeffnet nach einer TAN einen neuen.

    Ein pausierter TAN-Dialog schliesst den Client im Adapter. Nach der
    Wiederaufnahme liefert die Bibliothek nur noch das Ergebnis der
    ausloesenden Operation; weitere Abrufe brauchen deshalb einen frischen
    Dialog.

    Der Halter traegt nur den Bankzugang als ID und die PIN im
    Arbeitsspeicher, kein ORM-Objekt (N-DB-04). Jeder Dialog wird ueber die
    jeweils offene Session des Laufs aufgebaut; an der TAN-Pause wird der
    Dialog verworfen, damit waehrend der Pause keine Verbindung gebunden ist.
    """

    def __init__(self, service: "SyncService", bank_access_id: int, pin: str) -> None:
        self._service = service
        self._bank_access_id = bank_access_id
        self._pin = pin
        self.gateway: FinTSGateway | None = None
        self.stale = False

    def ensure_open(self, session: Session) -> FinTSGateway:
        if self.gateway is None or self.stale:
            self.close()
            gateway = self._build(session)
            gateway.__enter__()
            self.gateway = gateway
            self.stale = False
        return self.gateway

    def resume_gateway(self, session: Session) -> FinTSGateway:
        """Baut einen Dialoghalter ohne offenes ``with`` fuer ``resume_tan``.

        Die Wiederaufnahme baut ihren Client selbst aus den Handoff-Bytes; ein
        ``__enter__`` waere falsch. Der zurueckgegebene Gateway wird nur fuer
        diesen einen Aufruf gehalten und danach verworfen.
        """

        self.close()
        return self._build(session)

    def restart(self) -> None:
        """Verwirft den aktuellen Dialog; der naechste Schritt oeffnet neu."""

        self.close()
        self.stale = True

    def close(self) -> None:
        gateway = self.gateway
        self.gateway = None
        if gateway is not None:
            gateway.__exit__(None, None, None)

    def _build(self, session: Session) -> FinTSGateway:
        access = session.get(BankAccess, self._bank_access_id)
        if access is None:
            raise LookupError(f"Bankzugang {self._bank_access_id} nicht gefunden.")
        return self._service._open_gateway(access, self._pin)


class SyncService:
    """Startet Batch- und Einzel-Syncs je Bankzugang (F-SYN-01, F-SYN-02).

    Die Abhaengigkeiten sind injizierbar, damit Tests ohne Netz, ohne echte
    PIN und ohne Wartezeit laufen (D8):
    Args:
        session_factory: Liefert je Sync und nach jeder TAN-Pause eine
            SQLAlchemy-Session. Der Service schliesst sie am Ende des Laufs
            und an jeder TAN-Pause (N-DB-04); die Persistenz committet je
            Konto.
        registry: In-Memory-Registry fuer Sperre und TAN-Dialogzustand
            (F-SYN-04, F-SYN-11). Ohne Angabe wird eine eigene Registry genutzt.
        settings: Anwendungskonfiguration mit ``FINTS_PRODUCT_ID``. Ohne
            Angabe wird die gecachte Konfiguration von
            :func:`finue.config.get_settings` genutzt.
        client_factory: Fabrik fuer den python-fints-Client. In Tests wird
            hier der Fake-Client eingesetzt.
        cipher: :class:`~finue.services.pin_crypto.PinCipher` fuer gespeicherte
            PINs. Bei ``store_pin = False`` wird sie nicht benoetigt.
        sleep: Wartezeit zwischen Wiederholungen. Ohne Angabe wartet der
            Service ueber ``Event.wait`` der Abbruchmarke (F-SYN-10); Tests
            injizieren einen Spion ohne echte Wartezeit.
        max_retries: Hoechstzahl zusaetzlicher Versuche (F-SYN-06).
        wait_seconds: Wartezeit zwischen zwei Versuchen.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        registry: SyncRegistry | None = None,
        settings: Settings | None = None,
        client_factory: Callable[..., FinTSClientProtocol] | None = None,
        cipher: PinCipher | None = None,
        sleep: Callable[[float], None] | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries darf nicht negativ sein.")
        if wait_seconds < 0:
            raise ValueError("wait_seconds darf nicht negativ sein.")
        self._session_factory = session_factory
        self._registry = registry if registry is not None else SyncRegistry()
        self._settings = settings
        self._client_factory = client_factory
        self._cipher = cipher
        self._sleep = sleep
        self._max_retries = max_retries
        self._wait_seconds = wait_seconds
        self._lock = threading.RLock()
        self._runs: dict[str, _RunContext] = {}

    @property
    def registry(self) -> SyncRegistry:
        """Registry mit den aktiven Jobs und TAN-Abfragen."""
        return self._registry

    # -- Start --------------------------------------------------------------

    def claim_sync(
        self,
        bank_access_id: int,
        account_ids: Iterable[int] | None = None,
    ) -> tuple[SyncJob, bool]:
        """Beansprucht den Bankzugang atomar und liefert Job und ``created``.

        Der Aufruf prueft die IDs, legt bei freiem Zugang einen neuen Job an
        und gibt ``created = False`` samt laufendem Job zurueck, wenn bereits
        ein Sync laeuft (F-SYN-11). Der Hintergrund-Runner (T-112) nennt den
        Zugang sofort und kennt die Job-ID, bevor der Lauf startet.
        """
        checked_access_id = _checked_id(bank_access_id, "bank_access_id")
        requested_ids = _checked_account_ids(account_ids)
        return self._registry.try_start(
            checked_access_id,
            requested_ids if requested_ids is not None else (),
        )

    def start_sync(
        self,
        bank_access_id: int,
        account_ids: Iterable[int] | None = None,
        supplied_pin: str | None = None,
    ) -> SyncResult:
        """Startet einen Batch- oder Einzel-Sync und liefert das Ergebnis.

        ``account_ids = None`` startet den Batch aller aktiven FinTS-Konten
        des Zugangs in genau einem Dialog (F-SYN-01). Eine Liste startet den
        Einzel-Sync genau dieser Konten (F-SYN-02). Laeuft fuer den Zugang
        bereits ein Job, wird dieser unveraendert zurueckgegeben (F-SYN-11).
        Verlangt die Bank eine TAN, endet der Aufruf mit
        ``status='wartet_auf_tan'`` und der Challenge (F-SYN-04).

        Args:
            bank_access_id: Bankzugang, dessen Konten synchronisiert werden.
            account_ids: Konten fuer einen Einzel-Sync; ``None`` fuer Batch.
            supplied_pin: PIN im Arbeitsspeicher fuer Zugaenge mit
                ``store_pin = False`` (F-SYN-08). Bei gespeicherter PIN wird
                der Wert ignoriert.

        Returns:
            :class:`SyncResult` mit Job, Gesamtstatus und Kontoergebnissen.

        Raises:
            LookupError: Der Zugang oder ein ausdruecklich genanntes Konto
                existiert nicht bzw. gehoert nicht zum Zugang.
            SyncConfigurationError: Der Zugang hat keine aktiven FinTS-Konten.
            PinRequiredError: Eine PIN wird benoetigt, ist aber nicht
                verfuegbar (F-SYN-08).
        """
        job, created = self.claim_sync(bank_access_id, account_ids)
        if not created:
            # F-SYN-11: Der zweite Anstoss fuehrt nichts aus und liefert den
            # laufenden Job. Die Sperre bleibt beim Erststarter.
            return SyncResult(job=job, reused=True, status="laeuft")
        return self.start_claimed_sync(
            job,
            bank_access_id=bank_access_id,
            account_ids=account_ids,
            supplied_pin=supplied_pin,
        )

    def start_claimed_sync(
        self,
        job: SyncJob,
        *,
        bank_access_id: int,
        account_ids: Iterable[int] | None = None,
        supplied_pin: str | None = None,
    ) -> SyncResult:
        """Fuehrt einen bereits beanspruchten Job aus (:meth:`claim_sync`).

        Der Hintergrund-Runner (T-112, D3) ruft diese Methode in einem eigenen
        Thread auf: Die Job-ID ist bereits vergeben, der HTTP-Anstoss kehrt
        sofort zurueck. Die Methode kehrt an der ersten TAN-Pause
        (``wartet_auf_tan``) oder nach dem Laufende zurueck und verhaelt sich
        ansonsten wie :meth:`start_sync`.
        """
        checked_access_id = _checked_id(bank_access_id, "bank_access_id")
        requested_ids = _checked_account_ids(account_ids)
        job_group_id = job.id if requested_ids is None else None
        context = _RunContext(
            job=job,
            bank_access_id=checked_access_id,
            requested_ids=requested_ids,
            job_group_id=job_group_id,
        )
        generator = self._run(context, supplied_pin)
        context.generator = generator
        with self._lock:
            self._runs[job.id] = context
        try:
            handoff = next(generator)
        except StopIteration:
            return self._finish_completed(context)
        except SyncCancelledError:
            # F-SYN-10: Ein Abbruch vor der ersten TAN-Pause (auch waehrend
            # eines laufenden Netzaufrufs) beendet den Lauf als
            # ``abgebrochen``, nicht als generischen ``fehler`` - analog zu
            # :meth:`_advance`.
            return self._finish_interrupted_result(context, "abgebrochen")
        except TanTimeoutError:
            # F-SYN-04: Dasselbe gilt fuer einen bereits abgelaufenen Dialog.
            return self._finish_interrupted_result(context, "zeitueberschreitung")
        except BaseException:
            self._forget(context)
            self._release_registry(context, "fehler")
            raise
        if context.job.abort_requested:
            # Der Abbruch kam waehrend des laufenden Netzaufrufs an, bevor die
            # erste TAN-Pause erreicht war. Statt eine Pause zu oeffnen, wird
            # der Lauf regulaer beendet und die Sperre freigegeben (F-SYN-10) -
            # analog zu :meth:`_advance`.
            return self._interrupt(context, SyncCancelledError(), "abgebrochen")
        return self._pause_result(context, handoff)

    # -- TAN und Abbruch ----------------------------------------------------

    def submit_tan(self, job_id: str, tan: str) -> SyncResult:
        """Nimmt eine TAN an und setzt den pausierten Dialog fort (F-SYN-04).

        Die erste Antwort gewinnt. Ein weiterer Aufruf fuer dieselbe Abfrage
        oder ein Aufruf fuer eine App-Freigabe liefert
        :class:`SyncTanConflictError`. Die TAN wird weder gespeichert noch
        protokolliert (N-SEC-02).
        """

        if not isinstance(tan, str) or not tan.strip():
            raise ValueError("Fuer die TAN-Eingabe fehlt die TAN.")
        context, timed_out = self._claim_answer(job_id, expected_kind="text")
        if timed_out:
            try:
                return self._interrupt(context, TanTimeoutError(), "zeitueberschreitung")
            finally:
                context.busy = False
        try:
            return self._advance(context, tan)
        finally:
            context.busy = False

    def poll_app_approval(self, job_id: str) -> SyncResult:
        """Fragt eine App-Freigabe erneut ab und meldet den Fortschritt.

        Jeder Aufruf zaehlt als Abfrage (``approval_polls``). Liefert die Bank
        eine weitere Freigabe, bleibt der Lauf ``wartet_auf_tan``; die erste
        Antwort in einer anderen Sitzung gewinnt (F-SYN-04).
        """

        context, timed_out = self._claim_answer(job_id, expected_kind="app")
        if timed_out:
            try:
                return self._interrupt(context, TanTimeoutError(), "zeitueberschreitung")
            finally:
                context.busy = False
        context.approval_polls += 1
        try:
            return self._advance(context, None)
        finally:
            context.busy = False

    def cancel(self, job_id: str) -> SyncResult:
        """Bricht den Lauf ab und beendet den Dialog (F-SYN-10).

        Vollstaendig uebernommene Konten bleiben erhalten. Vom laufenden Konto
        werden keine Kontodaten gespeichert; die Protokollzeile wechselt auf
        ``abgebrochen``.
        """

        with self._lock:
            context = self._runs.get(job_id)
            if context is None:
                raise SyncTanConflictError("Der Sync-Job ist nicht aktiv.")
            if context.handoff is None or context.busy:
                self._registry.request_abort(job_id)
                return SyncResult(
                    job=context.job,
                    reused=False,
                    status="laeuft",
                    account_results=tuple(context.results),
                    approval_polls=context.approval_polls,
                )
            self._registry.abort(job_id)
            self._runs.pop(job_id, None)
            context.busy = True
        try:
            return self._interrupt(context, SyncCancelledError(), "abgebrochen")
        finally:
            context.busy = False

    def expire_tan_timeout(self, job_id: str) -> SyncResult | None:
        """Prueft die injizierbare 5-Minuten-Frist und verwirft den Dialog.

        Liefert ``None``, wenn der Job nicht wartet oder die Frist nicht
        erreicht ist. Nach Ablauf wird der Lauf als ``zeitueberschreitung``
        protokolliert (F-SYN-04/07/10).
        """

        with self._lock:
            context = self._runs.get(job_id)
            if context is None or context.handoff is None or context.busy:
                return None
            if not self._registry.expire_if_timed_out(job_id):
                return None
            self._runs.pop(job_id, None)
            context.busy = True
        try:
            return self._interrupt(context, TanTimeoutError(), "zeitueberschreitung")
        finally:
            context.busy = False

    def get_challenge(self, job_id: str) -> TanChallenge | None:
        """Liefert die offene TAN-Abfrage fuer eine beliebige Sitzung."""

        with self._lock:
            context = self._runs.get(job_id)
            if context is None:
                return None
            return context.challenge

    # -- Ausfuehrung --------------------------------------------------------

    def _run(
        self,
        context: _RunContext,
        supplied_pin: str | None,
    ) -> Generator[TanHandoff[Any], str | None, None]:
        """Fuehrt den Lauf aus und liefert bei TAN die Handoffs.

        Die Funktion ist ein Generator: Jede TAN-Abfrage wird als
        :class:`~finue.services.fints_client.TanHandoff` geliefert; der
        Aufrufer sendet die TAN (oder ``None`` fuer eine App-Abfrage) zurueck.
        An der Pause schliesst der Lauf die DB-Session und traegt danach nur
        noch IDs und Werte im Laufzustand (N-DB-04); der eingefrorene Dialog
        bleibt nur im Arbeitsspeicher (N-SEC-03). Nach der Freigabe oeffnet
        der Lauf eine frische Session und setzt fort.
        """

        job = context.job
        session = self._open_session(context)
        holder: _GatewayHolder | None = None
        try:
            access = session.get(BankAccess, context.bank_access_id)
            if access is None:
                raise LookupError(f"Bankzugang {context.bank_access_id} nicht gefunden.")

            accounts = _select_accounts(session, context.bank_access_id, context.requested_ids)
            if not accounts:
                raise SyncConfigurationError("Der Bankzugang hat keine aktiven FinTS-Konten fuer den Sync.")
            context.account_ids = tuple(account.id for account in accounts)
            context.blz = access.bank.blz
            job.account_ids = context.account_ids

            pin = resolve_pin(access, supplied_pin, cipher=self._cipher)
            holder = _GatewayHolder(self, context.bank_access_id, pin)
            # Kein ORM-Objekt und keine Session ueber die erste TAN-Pause tragen.
            del access, accounts, session

            try:
                sepa_accounts = yield from self._step(context, holder, None, lambda gateway: gateway.get_accounts())
            except (SyncCancelledError, TanTimeoutError):
                raise
            except Exception as exc:
                self._fail_all(context, context.account_ids, exc)
                return

            by_iban: dict[str, SEPAAccount] = {}
            for sepa_account in sepa_accounts:
                iban = _normalized_iban(sepa_account.iban)
                if iban is not None:
                    by_iban[iban] = sepa_account

            for index in range(context.index, len(context.account_ids)):
                context.index = index
                account_id = context.account_ids[index]
                if job.abort_requested:
                    raise SyncCancelledError()
                context.current_account_id = account_id
                exists, account_iban = self._account_iban(context, account_id)
                if not exists:
                    context.current_account_id = None
                    continue
                sepa_account = by_iban.get(_normalized_iban(account_iban) or "")
                if sepa_account is None:
                    self._fail_current(
                        context,
                        account_id,
                        SyncAccountNotInBankError(account_id, account_iban),
                        attempts=1,
                    )
                    context.current_account_id = None
                    continue

                fetch_range = compute_fetch_range(self._require_session(context), account_id)
                attempts = 1
                try:
                    data, attempts = yield from self._fetch_with_retry(
                        context,
                        holder,
                        account_id,
                        sepa_account,
                        account_iban,
                        fetch_range,
                    )
                    if job.abort_requested:
                        raise SyncCancelledError()
                    run = self._persist_success(context, account_id, data)
                    context.results.append(
                        AccountSyncResult(
                            account_id=account_id,
                            status=_account_status(run.status),
                            import_run_id=run.id,
                            attempts=attempts,
                            message=run.message,
                        )
                    )
                    del run
                    context.current_account_id = None
                    context.waiting_run_id = None
                except (SyncCancelledError, TanTimeoutError):
                    raise
                except Exception as exc:
                    self._fail_current(context, account_id, exc, attempts=attempts)
                    context.current_account_id = None
                    continue
        except (SyncCancelledError, TanTimeoutError) as exc:
            # An der Pause ist die Session geschlossen; fuer die
            # Abschlussprotokolle oeffnet der Lauf eine frische (N-DB-04).
            self._open_session(context)
            try:
                self._finalize_interrupted(context, exc)
            finally:
                self._close_session(context)
            raise
        finally:
            if holder is not None:
                holder.close()
            self._close_session(context)

    def _fetch_with_retry(
        self,
        context: _RunContext,
        holder: _GatewayHolder,
        account_id: int,
        sepa_account: SEPAAccount,
        account_iban: str | None,
        fetch_range: tuple[date, date] | None,
    ) -> Generator[TanHandoff[Any], str | None, tuple[SyncAccountData, int]]:
        """Ruft ein Konto ab und wiederholt nur wiederholbare Bankfehler.

        Der erste Versuch nutzt den offenen Dialog; jeder weitere oeffnet
        einen frischen Dialog, weil die Bank ihn nach einem wiederholbaren
        Fehler verlangt (F-SYN-06). TAN-Abfragen innerhalb eines Versuchs
        laufen ueber :meth:`_step` und unterbrechen die Wiederholung nicht.
        """

        attempts = 0
        while True:
            attempts += 1
            if attempts > 1:
                holder.restart()
            try:
                return (
                    yield from self._fetch_account(
                        context,
                        holder,
                        account_id,
                        sepa_account,
                        account_iban,
                        fetch_range,
                    )
                ), attempts
            except (SyncCancelledError, TanTimeoutError):
                raise
            except PinWrongError as exc:
                if not self._resolve_retryable(context, exc.code, exc.original_text):
                    raise
                error: FinTSBankError = FinTSBankError(exc.code, exc.original_text)
            except FinTSBankError as exc:
                error = exc

            if attempts > self._max_retries or not self._resolve_retryable(context, error.code, ""):
                raise error
            _retry_sleep(context.job, self._sleep)(self._wait_seconds)
            if context.job.abort_requested:
                raise SyncCancelledError()

    def _resolve_retryable(self, context: _RunContext, code: str, original_text: str) -> bool:
        """Prueft einen Bankcode ueber das Fehler-Mapping (F-SYN-05/06).

        Die Session wird erst hier, nach einem etwaigen Resume, ueber den
        Laufzustand geladen; sie ist in diesem Pfad immer offen.
        """

        return resolve_error(
            self._require_session(context),
            code,
            context.blz,
            original_text,
        ).retryable

    def _fetch_account(
        self,
        context: _RunContext,
        holder: _GatewayHolder,
        account_id: int,
        sepa_account: SEPAAccount,
        account_iban: str | None,
        fetch_range: tuple[date, date] | None,
    ) -> Generator[TanHandoff[Any], str | None, SyncAccountData]:
        """Liest Saldo, Umsaetze und Auftraege eines Kontos (F-SYN-03)."""

        balance: BalanceResult = yield from self._step(
            context,
            holder,
            account_id,
            lambda gateway: gateway.get_balance(sepa_account),
        )
        if fetch_range is None:
            fetch: TransactionFetch = yield from self._step(
                context,
                holder,
                account_id,
                lambda gateway: gateway.get_transactions(sepa_account),
            )
        else:
            start_date, end_date = fetch_range
            fetch = yield from self._step(
                context,
                holder,
                account_id,
                lambda gateway: gateway.get_transactions(
                    sepa_account,
                    start_date=start_date,
                    end_date=end_date,
                ),
            )
        # F-AUF-04/F-AUF-05: Ein nicht unterstuetzter oder leerer Abruf ist
        # kein Sync-Fehler. Der Zustand wird mit persistiert.
        orders: OrdersResult = yield from self._step(
            context,
            holder,
            account_id,
            lambda gateway: gateway.fetch_standing_orders(sepa_account),
        )
        return SyncAccountData(
            account_ref=_account_reference(sepa_account, account_id, account_iban),
            balance=balance,
            transactions=_to_fetched_transactions(fetch),
            orders=orders,
        )

    def _step(
        self,
        context: _RunContext,
        holder: _GatewayHolder,
        account_id: int | None,
        operation: Callable[[FinTSGateway], _ResultT],
    ) -> Generator[TanHandoff[Any], str | None, _ResultT]:
        """Fuehrt einen lesenden Schritt aus und pausiert bei einer TAN.

        Der Generator liefert den Handoff an den Service; die Antwort wird als
        ``str`` (Text-TAN) oder ``None`` (App-Freigabe) zurueckgesendet. Vor
        dem Pausieren schliesst der Schritt die DB-Session und verwirft den
        Dialog (N-DB-04); nach der Freigabe oeffnet er eine frische Session
        und setzt den eingefrorenen Dialog mit einem neuen Dialoghalter fort.
        """

        if context.job.abort_requested:
            raise SyncCancelledError()
        gateway = holder.ensure_open(self._require_session(context))
        try:
            return operation(gateway)
        except TanRequired as exc:
            handoff = exc.handoff
            holder.close()
            del gateway
            while True:
                self._prepare_waiting(context, account_id)
                # Keine offene Verbindung waehrend der Pause (N-DB-04).
                self._close_session(context)
                tan = yield handoff
                resumed = holder.resume_gateway(self._open_session(context))
                try:
                    return cast(_ResultT, resumed.resume_tan(handoff, tan))
                except TanRequired as further:
                    handoff = further.handoff
                    holder.close()
                    del resumed

    def _prepare_waiting(
        self,
        context: _RunContext,
        account_id: int | None,
    ) -> None:
        """Legt die Protokollzeile ``wartet_auf_tan`` vor dem Handoff an."""

        if account_id is None or context.waiting_run_id is not None:
            return
        run = persist_waiting_for_tan(self._require_session(context), account_id, context.job_group_id)
        context.waiting_run_id = run.id

    # -- Session-Lebenszyklus ----------------------------------------------

    def _open_session(self, context: _RunContext) -> Session:
        """Oeffnet die Session des Laufs, falls keine offen ist (N-DB-04)."""

        if context.session is None:
            context.session = self._session_factory()
        return context.session

    def _close_session(self, context: _RunContext) -> None:
        """Schliesst die Session des Laufs und gibt die Verbindung frei (N-DB-04)."""

        session = context.session
        context.session = None
        if session is not None:
            session.close()

    def _require_session(self, context: _RunContext) -> Session:
        """Liefert die offene Session; wartet der Lauf, wirft der Aufruf."""

        session = context.session
        if session is None:
            raise RuntimeError("Kein Laufzustand fuer die TAN-Pause vorhanden.")
        return session

    def _account_iban(self, context: _RunContext, account_id: int) -> tuple[bool, str | None]:
        """Liest die IBAN eines Kontos frisch aus der offenen Session.

        Zwischen den Schritten wird ausschliesslich dieser Wert gehalten, kein
        ORM-Objekt (N-DB-04).
        """

        account = self._require_session(context).get(Account, account_id)
        if account is None:
            return False, None
        return True, account.iban

    # -- Ergebnis und Persistenz -------------------------------------------

    def _persist_success(
        self,
        context: _RunContext,
        account_id: int,
        data: SyncAccountData,
    ) -> ImportRun:
        session = self._require_session(context)
        waiting = session.get(ImportRun, context.waiting_run_id) if context.waiting_run_id is not None else None
        if data.orders is not None:
            # F-AUF-04/F-AUF-05: Der Auftragsbestand wird vor dem Commit von
            # persist_account_sync ersetzt und damit in derselben
            # Konto-Transaktion gespeichert. Bei Abbruch oder Fehler wird
            # dieser Zweig nie erreicht bzw. die Transaktion zurueckgerollt;
            # vom laufenden Konto bleibt kein Auftragsstand zurueck.
            try:
                replace_standing_orders(
                    session,
                    account_id,
                    data.orders.orders,
                    utc_now(),
                )
            except Exception:
                # Bereits geloeschte Zeilen sofort zuruecknehmen, damit die
                # anschliessende Fehlerprotokollierung auf einer nutzbaren
                # Session laeuft.
                session.rollback()
                raise
        return persist_account_sync(
            session,
            account_id,
            data,
            context.job_group_id,
            waiting_run=waiting,
        )

    def _fail_current(
        self,
        context: _RunContext,
        account_id: int,
        error: Exception,
        *,
        attempts: int,
    ) -> None:
        session = self._require_session(context)
        waiting = session.get(ImportRun, context.waiting_run_id) if context.waiting_run_id is not None else None
        run = persist_sync_failure(
            session,
            account_id,
            context.job_group_id,
            error,
            blz=context.blz,
            waiting_run=waiting,
        )
        context.results.append(
            AccountSyncResult(
                account_id=account_id,
                status=_account_status(run.status),
                import_run_id=run.id,
                attempts=attempts,
                message=run.message,
            )
        )
        context.waiting_run_id = None

    def _fail_all(
        self,
        context: _RunContext,
        account_ids: Sequence[int],
        error: Exception,
    ) -> None:
        for account_id in account_ids:
            self._fail_current(context, account_id, error, attempts=1)

    def _finalize_interrupted(
        self,
        context: _RunContext,
        error: Exception,
    ) -> None:
        """Protokolliert Abbruch/Timeout je Konto (F-SYN-10).

        Das laufende Konto wird nicht uebernommen; vollstaendig uebernommene
        Konten bleiben erhalten. Fuer das laufende und die noch nicht
        gestarteten Konten entsteht je eine Protokollzeile. Die Protokolle
        laufen auf der vor dem Aufruf frisch geoeffneten Session (N-DB-04).
        """

        session = self._require_session(context)
        if context.current_account_id is not None:
            account_id = context.current_account_id
            if session.get(Account, account_id) is not None:
                self._fail_current(context, account_id, error, attempts=1)
            context.current_account_id = None
            start = context.index + 1
        else:
            start = context.index

        for account_id in context.account_ids[start:]:
            if session.get(Account, account_id) is None:
                continue
            self._fail_current(context, account_id, error, attempts=1)

    # -- Antworten und Status ----------------------------------------------

    def _claim_answer(
        self,
        job_id: str,
        *,
        expected_kind: str,
    ) -> tuple[_RunContext, bool]:
        """Nimmt die erste Antwort unter dem Service-Lock an (F-SYN-04)."""

        with self._lock:
            context = self._runs.get(job_id)
            if context is None:
                raise SyncTanConflictError("Der Sync-Job wartet nicht auf eine TAN.")
            if context.busy or context.handoff is None:
                raise SyncTanConflictError("Die TAN-Antwort wurde bereits angenommen.")
            if context.handoff.challenge.kind != expected_kind:
                raise SyncTanConflictError("Es ist keine passende TAN-Abfrage offen.")
            if not self._registry.answer_tan(job_id):
                if context.job.status is SyncStatus.TIMED_OUT:
                    self._runs.pop(job_id, None)
                    context.busy = True
                    return context, True
                raise SyncTanConflictError("Die TAN-Antwort wurde bereits angenommen.")
            context.busy = True
            context.handoff = None
            context.challenge = None
            return context, False

    def _advance(
        self,
        context: _RunContext,
        answer: str | None,
    ) -> SyncResult:
        generator = context.generator
        if generator is None:
            raise RuntimeError("Der Sync-Lauf hat keinen Generator.")
        try:
            handoff = generator.send(answer)
        except StopIteration:
            return self._finish_completed(context)
        except SyncCancelledError:
            return self._finish_interrupted_result(context, "abgebrochen")
        except TanTimeoutError:
            return self._finish_interrupted_result(context, "zeitueberschreitung")
        except BaseException:
            self._forget(context)
            self._release_registry(context, "fehler")
            raise
        if context.job.abort_requested:
            # Der Abbruch kam waehrend des laufenden Netzaufrufs an. Statt eine
            # weitere TAN-Pause zu oeffnen, wird der Generator regulaer beendet
            # und die Sperre freigegeben (F-SYN-10).
            return self._interrupt(context, SyncCancelledError(), "abgebrochen")
        return self._pause_result(context, handoff)

    def _interrupt(
        self,
        context: _RunContext,
        error: Exception,
        status: SyncOverallStatus,
    ) -> SyncResult:
        """Wirft Abbruch/Timeout in den pausierten Generator."""

        generator = context.generator
        if generator is not None:
            try:
                generator.throw(error)
            except (StopIteration, SyncCancelledError, TanTimeoutError):
                pass
        return self._finish_interrupted_result(context, status)

    def _pause_result(
        self,
        context: _RunContext,
        handoff: TanHandoff[Any],
    ) -> SyncResult:
        self._registry.mark_awaiting_tan(
            context.job.id,
            handoff,
            challenge=handoff.challenge.challenge,
            tan_kind=handoff.challenge.kind,
        )
        context.handoff = handoff
        context.challenge = handoff.challenge
        return SyncResult(
            job=context.job,
            reused=False,
            status="wartet_auf_tan",
            account_results=tuple(context.results),
            challenge=handoff.challenge,
            approval_polls=context.approval_polls,
        )

    def _finish_completed(self, context: _RunContext) -> SyncResult:
        overall = _overall_status(context.results)
        if overall == "fehler":
            self._registry.fail(context.job.id)
        else:
            self._registry.complete(context.job.id)
        self._forget(context)
        return SyncResult(
            job=context.job,
            reused=False,
            status=overall,
            account_results=tuple(context.results),
            approval_polls=context.approval_polls,
        )

    def _finish_interrupted_result(
        self,
        context: _RunContext,
        status: SyncOverallStatus,
    ) -> SyncResult:
        self._forget(context)
        self._release_registry(context, status)
        return SyncResult(
            job=context.job,
            reused=False,
            status=status,
            account_results=tuple(context.results),
            approval_polls=context.approval_polls,
        )

    def _release_registry(
        self,
        context: _RunContext,
        status: SyncOverallStatus,
    ) -> None:
        """Gibt die Zugangssperre nach einem Abschluss idempotent frei.

        Ein Abbruch waehrend eines laufenden Netzaufrufs setzt nur die
        Abbruchmarke; der Netzaufruf selbst wird nicht unterbrochen
        (F-SYN-10). Sobald er zurueckkehrt, muss der Lauf die Sperre trotzdem
        freigeben. Alle Registry-Endzustaende sind idempotent, ein bereits
        gesetzter Endzustand bleibt unveraendert (F-SYN-11).
        """

        job_id = context.job.id
        if status == "abgebrochen":
            self._registry.abort(job_id)
        elif status == "zeitueberschreitung":
            self._registry.expire(job_id)
        else:
            self._registry.fail(job_id)

    def _forget(self, context: _RunContext) -> None:
        with self._lock:
            if self._runs.get(context.job.id) is context:
                del self._runs[context.job.id]

    def _open_gateway(self, access: BankAccess, pin: str) -> FinTSGateway:
        """Oeffnet einen lesenden Gateway fuer den Zugang (N-SEC-05, D2)."""
        return FinTSGateway(
            access.bank,
            login_name=access.login_name,
            pin=pin,
            settings=self._settings,
            tan_medium=access.tan_method,
            client_factory=self._client_factory,
        )


def _retry_sleep(job: SyncJob, injected: Callable[[float], None] | None) -> Callable[[float], None]:
    """Baut die Wartefunktion zwischen zwei Wiederholungen.

    Ohne injizierten Spion wartet der Service auf ``Event.wait`` der
    Abbruchmarke: Ein Abbruch unterbricht die Wartezeit sofort und ein
    Timeout beendet sie nach der vorgegebenen Dauer (F-SYN-10).
    """

    if injected is not None:
        return injected

    def wait(seconds: float) -> None:
        job.wait_for_abort(seconds)

    return wait


def _select_accounts(
    session: Session,
    bank_access_id: int,
    account_ids: tuple[int, ...] | None,
) -> list[Account]:
    """Waehlt die aktiven FinTS-Konten des Zugangs (F-SYN-01, F-SYN-02)."""
    statement = select(Account).where(
        Account.bank_access_id == bank_access_id,
        Account.source == "fints",
        Account.active.is_(True),
    )
    if account_ids is not None:
        statement = statement.where(Account.id.in_(account_ids))
    accounts = list(session.scalars(statement.order_by(Account.id)))

    if account_ids is not None:
        found = {account.id for account in accounts}
        missing = [account_id for account_id in account_ids if account_id not in found]
        if missing:
            raise LookupError(
                "Konto/Konten nicht gefunden oder nicht aktiv: "
                + ", ".join(str(account_id) for account_id in missing)
                + "."
            )
    return accounts


def _overall_status(results: Sequence[AccountSyncResult]) -> SyncOverallStatus:
    """Leitet den Batch-Gesamtstatus aus den Kontoergebnissen ab (F-SYN-07)."""
    if not results:
        return "fehler"
    successful = sum(1 for result in results if result.status == "erfolgreich")
    if successful == len(results):
        return "erfolgreich"
    if successful == 0:
        return "fehler"
    return "teilweise"


def _to_fetched_transactions(fetch: TransactionFetch) -> tuple[FetchedTransaction, ...]:
    """Ueberfuehrt die Adapterbuchungen in das Persistenzformat.

    Der Adapter liefert :class:`~finue.services.duplicate.TransactionData`;
    :class:`~finue.services.fints_types.SyncAccountData` erwartet
    :class:`~finue.services.fints_types.FetchedTransaction`. Die Ueberfuehrung
    erhaelt Betrag, Datum, Status und die externe Referenz.
    """
    return tuple(_to_fetched_transaction(row) for row in fetch.all)


def _to_fetched_transaction(row: TransactionData) -> FetchedTransaction:
    """Ueberfuehrt eine einzelne Adapterbuchung."""
    if row.status not in {"gebucht", "vorgemerkt"}:
        raise ValueError("Unbekannter Buchungsstatus aus dem FinTS-Adapter.")
    return FetchedTransaction(
        booking_date=row.booking_date,
        amount_cents=row.amount_cents,
        currency=row.currency,
        status=cast(TransactionStatus, row.status),
        value_date=row.value_date,
        counterparty=row.counterparty,
        counterparty_iban=row.counterparty_iban,
        purpose=row.purpose,
        raw_reference=row.external_ref,
    )


def _account_reference(sepa_account: SEPAAccount, account_id: int, account_iban: str | None) -> str:
    """Liefert die von der Bank gelieferte Kontoreferenz.

    Zwischen den Schritten stehen nur die Konto-ID und die IBAN zur Verfuegung,
    kein ORM-Objekt (N-DB-04).
    """
    iban = sepa_account.iban
    if isinstance(iban, str) and iban.strip():
        return iban.strip()
    if account_iban:
        return account_iban
    return f"Konto {account_id}"


def _normalized_iban(value: str | None) -> str | None:
    """Normalisiert eine IBAN fuer den Abgleich; leere Werte werden ``None``."""
    if not value:
        return None
    text = value.strip()
    return text or None


def _account_status(value: str) -> AccountRunStatus:
    """Prueft den Status einer ImportRun-Zeile aus der Persistenz."""
    if value not in _ACCOUNT_STATUSES:
        raise ValueError(f"Unbekannter Sync-Status: {value!r}.")
    return cast(AccountRunStatus, value)


def _checked_id(value: object, field: str) -> int:
    """Prueft eine positive Ganzzahl-ID."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} muss eine positive Ganzzahl sein.")
    return value


def _checked_account_ids(value: Iterable[int] | None) -> tuple[int, ...] | None:
    """Prueft optionale Konten-IDs und entfernt Duplikate."""
    if value is None:
        return None
    return tuple(dict.fromkeys(_checked_id(item, "account_id") for item in value))
