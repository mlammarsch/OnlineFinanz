"""Tests fuer TAN-Pause, Wiederaufnahme, Abbruch und Timeout (T-078).

Referenzierte Anforderungen:

- F-SYN-04: Challenge-Text, Text-TAN und App-Freigabe mit Fortschritt,
  erste Antwort gewinnt, weitere Sitzungen erhalten einen definierten
  Konflikt, Timeout nach fuenf Minuten.
- F-SYN-07: ImportRun je Konto, waehrend der Pause ``wartet_auf_tan``,
  danach ``erfolgreich``, ``abgebrochen`` oder ``zeitueberschreitung``.
- F-SYN-10: Abbruch beendet den Dialog; vollstaendig uebernommene Konten
  bleiben, vom laufenden Konto wird nichts gespeichert.
- N-SEC-02/N-SEC-03: TAN und Dialogdaten erscheinen nicht in Logs,
  Protokollzeilen, Fehlern, ``repr`` oder auf Platte.

Alle Konten, Betraege und TANs sind frei erfunden (D8). Der Fake-Client
liefert die Antworten; es findet kein Bankzugriff statt.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fints.exceptions import FinTSClientPINError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import BankAccess
from finue.models.import_run import ImportRun
from finue.services.bank_access import create_access
from finue.services.bank_error_defaults import seed_defaults
from finue.services.sync_registry import (
    SyncEvent,
    SyncJobNotFoundError,
    SyncRegistry,
    SyncStatus,
)
from finue.services.sync_service import SyncService, SyncTanConflictError

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_tan", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FakeTanChallenge = fints_fake.FakeTanChallenge
FakeTanFlow = fints_fake.FakeTanFlow
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_IBAN_GIRO = fints_fake.FAKE_IBAN_GIRO
FAKE_IBAN_CARD = fints_fake.FAKE_IBAN_CARD
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
script_tan_app = fints_fake.script_tan_app
script_tan_text = fints_fake.script_tan_text

UTC = timezone.utc
TAN_TEXT = "TAN-GEHEIM-4711"


class FakeClock:
    """Injizierbare Uhr fuer den deterministischen 5-Minuten-Timeout."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass()
class TanEnvironment:
    """Testumgebung aus Datenbank, Zugang und Konten."""

    factory: sessionmaker[Session]
    access: BankAccess
    accounts: tuple[Account, ...]
    db_path: Path


class SharedClientFactory:
    """Liefert immer denselben Fake-Client fuer alle Dialoge eines Tests."""

    def __init__(
        self,
        script: FakeScript,
        *,
        client_type: type[Any] = FakeClient,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._script = script
        self._client_type = client_type
        self._client_kwargs = client_kwargs or {}
        self.client: Any | None = None
        self.created = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.created += 1
        if self.client is None:
            self.client = self._client_type(
                script=self._script, **self._client_kwargs, **kwargs
            )
        return self.client


class RecordingTanClient(FakeClient):
    """Fake-Client, der TANs mitschreibt und eine falsche TAN ablehnen kann."""

    def __init__(
        self,
        *args: Any,
        wrong_tan: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._wrong_tan = wrong_tan
        self.tan_values: list[str] = []

    def send_tan(self, challenge: Any, tan: str | None = None) -> Any:
        if tan:
            self.tan_values.append(tan)
        if tan is not None and self._wrong_tan is not None and tan == self._wrong_tan:
            error = FinTSClientPINError("9942: erfundene falsche TAN")
            setattr(error, "bank_code", "9942")
            setattr(error, "original_text", "9942: erfundene falsche TAN")
            raise error
        return super().send_tan(challenge, tan)


class BlockingTanClient(FakeClient):
    """Fake-Client, dessen ``send_tan`` bis zur Testfreigabe blockiert.

    Damit laesst sich ein laufender Netzaufruf nachstellen, waehrend der ein
    Abbruch nur die Abbruchmarke setzen kann (F-SYN-10).
    """

    def __init__(
        self,
        *args: Any,
        started: threading.Event,
        release: threading.Event,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._started = started
        self._release = release

    def send_tan(self, challenge: Any, tan: str | None = None) -> Any:
        self._started.set()
        if not self._release.wait(10):
            raise RuntimeError("Testfreigabe fuer den blockierten send_tan fehlt")
        return super().send_tan(challenge, tan)


def _settings() -> Settings:
    return Settings(_env_file=None, fints_product_id=FAKE_PRODUCT_ID)


def _make_environment(tmp_path: Path) -> TanEnvironment:
    """Legt eine Datei-SQLite mit Zugang und zwei aktiven FinTS-Konten an."""

    db_path = tmp_path / "tan.db"
    engine = create_db_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        access = create_access(
            session,
            blz=fints_fake.FAKE_BANK_IDENTIFIER,
            login_name=FAKE_USER_ID,
            store_pin=False,
            bank_name="Erfundene Testbank",
        )
        access.bank.fints_url = fints_fake.FAKE_SERVER
        first = Account(
            name="Erfundenes Girokonto",
            iban=FAKE_IBAN_GIRO,
            currency="EUR",
            source="fints",
            bank_access_id=access.id,
            active=True,
        )
        second = Account(
            name="Erfundene Kreditkarte",
            iban=FAKE_IBAN_CARD,
            currency="EUR",
            source="fints",
            bank_access_id=access.id,
            active=True,
        )
        session.add_all([first, second])
        seed_defaults(session)
        session.commit()
        access_id = access.id
        account_ids = (first.id, second.id)

    with factory() as session:
        access = session.get(BankAccess, access_id)
        assert access is not None
        accounts = tuple(session.get(Account, account_id) for account_id in account_ids)
        assert all(account is not None for account in accounts)

    return TanEnvironment(
        factory=factory,
        access=access,
        accounts=accounts,  # type: ignore[arg-type]
        db_path=db_path,
    )


def _service(
    environment: TanEnvironment,
    client_factory: Callable[..., Any],
    *,
    registry: SyncRegistry | None = None,
    sleep: Callable[[float], None] | None = None,
) -> SyncService:
    return SyncService(
        environment.factory,
        registry=registry if registry is not None else SyncRegistry(),
        settings=_settings(),
        client_factory=client_factory,
        sleep=sleep if sleep is not None else (lambda _seconds: None),
        wait_seconds=0.0,
    )


def _runs(factory: sessionmaker[Session]) -> list[ImportRun]:
    with factory() as session:
        return list(
            session.scalars(
                select(ImportRun).where(ImportRun.kind == "sync").order_by(ImportRun.id)
            ).all()
        )


def _two_tan_script() -> FakeScript:
    """Zwei Konten, jedes verlangt eine eigene Text-TAN."""

    return FakeScript(
        name="batch_two_tan",
        accounts=fake_accounts(),
        balance=(
            FakeTanFlow(
                challenges=(FakeTanChallenge("Bitte TAN fuer Konto 1 eingeben"),),
                result=fake_balance("100,00"),
            ),
            FakeTanFlow(
                challenges=(FakeTanChallenge("Bitte TAN fuer Konto 2 eingeben"),),
                result=fake_balance("200,00"),
            ),
        ),
    )


def test_tan_success_pauses_and_resumes_with_waiting_import_run_f_syn_04_f_syn_07(
    tmp_path: Path,
) -> None:
    """F-SYN-04/07: Pause, Challenge-Hook, wartet_auf_tan und Wiederaufnahme."""

    environment = _make_environment(tmp_path)
    registry = SyncRegistry()
    events: list[SyncEvent] = []
    registry.add_listener(events.append)
    factory = SharedClientFactory(script_tan_text(), client_type=RecordingTanClient)
    service = _service(environment, factory, registry=registry)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert paused.status == "wartet_auf_tan"
    assert paused.challenge is not None
    assert paused.challenge.kind == "text"
    assert paused.challenge.challenge == "Bitte TAN eingeben: 123456"
    assert paused.job.status is SyncStatus.AWAITING_TAN
    assert paused.job.deadline is not None
    assert paused.job.challenge == "Bitte TAN eingeben: 123456"
    assert paused.job.tan_kind == "text"
    assert service.get_challenge(paused.job_id) == paused.challenge

    awaiting = [event for event in events if event.status is SyncStatus.AWAITING_TAN]
    assert len(awaiting) == 1
    assert awaiting[0].challenge == "Bitte TAN eingeben: 123456"
    assert awaiting[0].tan_kind == "text"

    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "wartet_auf_tan"
    assert runs[0].job_id is None  # Einzel-Sync
    waiting_run_id = runs[0].id

    completed = service.submit_tan(paused.job_id, TAN_TEXT)

    assert completed.status == "erfolgreich"
    assert completed.account_results[0].status == "erfolgreich"
    assert completed.job.status is SyncStatus.COMPLETED
    assert completed.job.challenge is None
    assert completed.job.dialog_state is None
    assert service.get_challenge(paused.job_id) is None
    assert registry.get(paused.job_id) is None
    assert registry.is_active(environment.access.id) is False

    assert factory.client is not None
    assert factory.client.resume_count == 1
    assert factory.client.send_tan_count == 1
    assert factory.client.tan_values == [TAN_TEXT]

    runs = _runs(environment.factory)
    assert len(runs) == 1  # dieselbe Zeile wurde fortgeschrieben
    assert runs[0].id == waiting_run_id
    assert runs[0].status == "erfolgreich"


def test_wrong_tan_fails_with_plaintext_without_secret_f_syn_04_n_sec_02(
    tmp_path: Path,
) -> None:
    """F-SYN-04, N-SEC-02: falsche TAN als Klartextfehler ohne TAN-Wert."""

    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(
        script_tan_text(),
        client_type=RecordingTanClient,
        client_kwargs={"wrong_tan": "000000"},
    )
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    result = service.submit_tan(paused.job_id, "000000")

    assert result.status == "fehler"
    assert result.account_results[0].status == "fehler"
    assert result.job.status is SyncStatus.FAILED
    assert registry_inactive(service, environment.access.id)

    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "fehler"
    message = runs[0].message or ""
    assert "PIN nicht akzeptiert" in message
    assert "000000" not in message
    assert TAN_TEXT not in message


def test_app_approval_polls_progress_until_success_f_syn_04(tmp_path: Path) -> None:
    """F-SYN-04: App-Freigabe braucht mehrere Polls und meldet Fortschritt."""

    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(script_tan_app())
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert paused.status == "wartet_auf_tan"
    assert paused.challenge is not None
    assert paused.challenge.kind == "app"
    assert paused.approval_polls == 0

    first_poll = service.poll_app_approval(paused.job_id)
    assert first_poll.status == "wartet_auf_tan"
    assert first_poll.challenge is not None
    assert first_poll.challenge.kind == "app"
    assert first_poll.challenge.challenge.endswith("(2/2)")
    assert first_poll.approval_polls == 1
    assert first_poll.job.status is SyncStatus.AWAITING_TAN

    second_poll = service.poll_app_approval(paused.job_id)
    assert second_poll.status == "erfolgreich"
    assert second_poll.approval_polls == 2
    assert second_poll.account_results[0].status == "erfolgreich"
    assert second_poll.job.status is SyncStatus.COMPLETED
    assert registry_inactive(service, environment.access.id)

    assert factory.client is not None
    assert factory.client.resume_count == 2
    assert factory.client.send_tan_count == 2


def test_cancel_mid_batch_keeps_completed_and_saves_nothing_for_running_account_f_syn_10(
    tmp_path: Path,
) -> None:
    """F-SYN-10: Abbruch mitten im Batch; laufendes Konto bleibt ungespeichert."""

    environment = _make_environment(tmp_path)
    script = FakeScript(
        name="cancel_batch",
        accounts=fake_accounts(),
        balance=(
            fake_balance("100,00"),
            FakeTanFlow(
                challenges=(FakeTanChallenge("Bitte TAN fuer Konto 2 eingeben"),),
                result=fake_balance("200,00"),
            ),
        ),
    )
    factory = SharedClientFactory(script)
    service = _service(environment, factory)

    paused = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)
    assert paused.status == "wartet_auf_tan"
    first, second = environment.accounts

    runs = _runs(environment.factory)
    states = {run.account_id: run.status for run in runs}
    assert states[first.id] == "erfolgreich"
    assert states[second.id] == "wartet_auf_tan"

    cancelled = service.cancel(paused.job_id)

    assert cancelled.status == "abgebrochen"
    assert cancelled.job.status is SyncStatus.ABORTED
    assert cancelled.job.dialog_state is None
    by_account = {item.account_id: item.status for item in cancelled.account_results}
    assert by_account[first.id] == "erfolgreich"
    assert by_account[second.id] == "abgebrochen"
    assert registry_inactive(service, environment.access.id)

    runs = _runs(environment.factory)
    states = {run.account_id: run.status for run in runs}
    assert states == {first.id: "erfolgreich", second.id: "abgebrochen"}
    assert len([run for run in runs if run.account_id == second.id]) == 1

    with environment.factory() as session:
        stored_second = session.get(Account, second.id)
        assert stored_second is not None
        assert stored_second.balance_cents is None


def test_timeout_after_five_minutes_discards_dialog_and_marks_zeitueberschreitung_f_syn_04_f_syn_10(
    tmp_path: Path,
) -> None:
    """F-SYN-04/10: injizierbare Frist, Dialog verworfen, Status zeitueberschreitung."""

    environment = _make_environment(tmp_path)
    start = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    registry = SyncRegistry(clock=clock)
    factory = SharedClientFactory(script_tan_text())
    service = _service(environment, factory, registry=registry)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert paused.job.deadline == start + timedelta(minutes=5)
    assert paused.job.dialog_state is not None

    clock.advance(timedelta(minutes=5))

    timed_out = service.expire_tan_timeout(paused.job_id)

    assert timed_out is not None
    assert timed_out.status == "zeitueberschreitung"
    assert timed_out.account_results[0].status == "zeitueberschreitung"
    assert timed_out.job.status is SyncStatus.TIMED_OUT
    assert timed_out.job.dialog_state is None
    assert timed_out.job.challenge is None
    assert registry_inactive(service, environment.access.id)

    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "zeitueberschreitung"
    assert runs[0].message is not None
    assert "TAN-Freigabe ist nicht rechtzeitig erfolgt" in runs[0].message

    with pytest.raises(SyncTanConflictError):
        service.submit_tan(paused.job_id, TAN_TEXT)


def test_parallel_tan_answers_only_first_wins_f_syn_04(tmp_path: Path) -> None:
    """F-SYN-04: zwei Sitzungen antworten gleichzeitig; genau eine TAN gilt."""

    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(
        script_tan_text(),
        client_type=RecordingTanClient,
    )
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    barrier = threading.Barrier(2)
    results: list[Any] = []
    conflicts: list[SyncTanConflictError] = []

    def answer(tan: str) -> None:
        barrier.wait(timeout=5)
        try:
            results.append(service.submit_tan(paused.job_id, tan))
        except SyncTanConflictError as exc:
            conflicts.append(exc)

    threads = [
        threading.Thread(target=answer, args=("111111",)),
        threading.Thread(target=answer, args=("222222",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 1
    assert len(conflicts) == 1
    assert results[0].status == "erfolgreich"
    assert factory.client is not None
    assert factory.client.send_tan_count == 1
    assert factory.client.tan_values in (["111111"], ["222222"])
    assert "111111" not in str(conflicts[0])
    assert "222222" not in str(conflicts[0])


def test_batch_with_two_tan_requests_is_served_sequentially_f_syn_04_f_syn_07(
    tmp_path: Path,
) -> None:
    """F-SYN-04/07: mehrere TAN-Abfragen im Batch werden nacheinander bedient."""

    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(_two_tan_script())
    service = _service(environment, factory)

    paused_first = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)
    assert paused_first.status == "wartet_auf_tan"
    assert paused_first.challenge is not None
    assert paused_first.challenge.challenge == "Bitte TAN fuer Konto 1 eingeben"
    assert len(paused_first.account_results) == 0

    paused_second = service.submit_tan(paused_first.job_id, "111111")
    assert paused_second.status == "wartet_auf_tan"
    assert paused_second.challenge is not None
    assert paused_second.challenge.challenge == "Bitte TAN fuer Konto 2 eingeben"
    assert len(paused_second.account_results) == 1
    assert paused_second.account_results[0].status == "erfolgreich"

    completed = service.submit_tan(paused_second.job_id, "222222")
    assert completed.status == "erfolgreich"
    assert [item.status for item in completed.account_results] == [
        "erfolgreich",
        "erfolgreich",
    ]
    assert registry_inactive(service, environment.access.id)

    assert factory.client is not None
    assert factory.client.send_tan_count == 2

    runs = _runs(environment.factory)
    assert len(runs) == 2
    assert all(run.status == "erfolgreich" for run in runs)
    assert {run.job_id for run in runs} == {completed.job_id}


def test_tan_and_dialog_state_never_reach_logs_db_errors_or_repr_n_sec_02_n_sec_03(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N-SEC-02/03: TAN und Dialogdaten bleiben im Arbeitsspeicher."""

    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(
        script_tan_text(),
        client_type=RecordingTanClient,
    )
    service = _service(environment, factory)
    account = environment.accounts[0]
    secret = "TAN-CAPTURE-987654"

    with caplog.at_level(logging.DEBUG):
        paused = service.start_sync(
            environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
        )
        assert paused.job.dialog_state is not None
        assert "dialog_state" not in repr(paused.job)
        completed = service.submit_tan(paused.job_id, secret)

    assert completed.status == "erfolgreich"
    assert secret not in caplog.text
    assert secret not in repr(completed.job)
    assert secret not in str(completed)

    with environment.factory() as session:
        runs = list(session.scalars(select(ImportRun)))
        errors = [error for run in runs for error in run.errors]
        assert all(secret not in (run.message or "") for run in runs)
        assert all(secret not in error.message for error in errors)

    raw = environment.db_path.read_bytes()
    assert secret.encode("utf-8") not in raw
    assert b"fake-dialog-state" not in raw


def test_finished_registry_releases_lock_and_discards_dialog_after_terminal_outcomes(
    tmp_path: Path,
) -> None:
    """F-SYN-04/11: nach Erfolg, Abbruch und Timeout ist die Sperre frei."""

    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(script_tan_text())
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    service.submit_tan(paused.job_id, TAN_TEXT)

    with pytest.raises(SyncJobNotFoundError):
        service.registry.clear_dialog_state(paused.job_id)
    assert service.registry.is_active(environment.access.id) is False

    with pytest.raises(SyncTanConflictError):
        service.cancel(paused.job_id)


def test_cancel_during_running_send_tan_releases_registry_and_allows_restart_f_syn_10_f_syn_11(
    tmp_path: Path,
) -> None:
    """F-SYN-10/11: Abbruch im laufenden send_tan gibt die Zugangssperre frei."""

    environment = _make_environment(tmp_path)
    started = threading.Event()
    release = threading.Event()
    factory = SharedClientFactory(
        script_tan_text(),
        client_type=BlockingTanClient,
        client_kwargs={"started": started, "release": release},
    )
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert paused.status == "wartet_auf_tan"

    results: list[Any] = []

    def answer() -> None:
        results.append(service.submit_tan(paused.job_id, TAN_TEXT))

    thread = threading.Thread(target=answer)
    thread.start()
    try:
        assert started.wait(5)
        # cancel trifft den laufenden Netzaufruf: nur Abbruchmarke, kein
        # behaupteter Netzabbruch.
        cancelled = service.cancel(paused.job_id)
        assert cancelled.status == "laeuft"
        assert registry_inactive(service, environment.access.id) is False
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].status == "abgebrochen"
    assert service.registry.is_active(environment.access.id) is False
    assert service.registry.get(paused.job_id) is None

    restarted = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert restarted.reused is False
    assert restarted.job_id != paused.job_id


def test_cancel_during_running_app_poll_releases_registry_lock_f_syn_04_f_syn_10(
    tmp_path: Path,
) -> None:
    """F-SYN-04/10: Abbruch im laufenden App-Poll gibt die Zugangssperre frei."""

    environment = _make_environment(tmp_path)
    started = threading.Event()
    release = threading.Event()
    factory = SharedClientFactory(
        script_tan_app(),
        client_type=BlockingTanClient,
        client_kwargs={"started": started, "release": release},
    )
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert paused.status == "wartet_auf_tan"
    assert paused.challenge is not None
    assert paused.challenge.kind == "app"

    results: list[Any] = []

    def approve() -> None:
        results.append(service.poll_app_approval(paused.job_id))

    thread = threading.Thread(target=approve)
    thread.start()
    try:
        assert started.wait(5)
        cancelled = service.cancel(paused.job_id)
        assert cancelled.status == "laeuft"
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].status == "abgebrochen"
    assert service.registry.is_active(environment.access.id) is False
    assert service.registry.get(paused.job_id) is None


def registry_inactive(service: SyncService, bank_access_id: int) -> bool:
    """Hilfsfunktion fuer die Sperrpruefung nach einem Endzustand."""

    return service.registry.is_active(bank_access_id) is False
