"""Integrationstests des vollstaendigen Sync-Flusses (T-081).

Der Test faehrt den echten :class:`~finue.services.sync_service.SyncService`
gegen eine temporaere SQLite-Datei und den skriptgesteuerten Fake-Client aus
``tests/fixtures/fints_fake.py`` (D8). Es gibt keinen Netzwerkzugriff, keine
echten Zugangsdaten und keine echten Wartezeiten.

Abgedeckte Anforderungen:

- F-SYN-01/F-SYN-02: Batch aller aktiven FinTS-Konten und Einzel-Sync genau
  der gewaehlten Konten.
- F-SYN-03: Saldo wird uebernommen; der Abrufzeitraum beginnt 14 Tage vor dem
  letzten erfolgreichen Sync.
- F-SYN-04: TAN per App-Freigabe in der Batch-Verarbeitung; ING-90-Tage-Text
  als Klartext-Challenge; Timeout nach fuenf Minuten.
- F-SYN-05/F-SYN-06: 9800 und 9010 werden mit frischem Dialog und injizierter
  Wartezeit wiederholt.
- F-SYN-07/F-SYN-10: Ein ImportRun je Konto, ``teilweise`` bei einem
  fehlerhaften Konto, Abbruch laesst abgeschlossene Konten bestehen.
- F-SYN-11: Zwei Threads starten gleichzeitig; der zweite Anstoss erhaelt den
  laufenden Job.
- F-AUF-03/F-AUF-05: Ausschliesslich lesende Operationen; ein nicht
  unterstuetzter Auftragsabruf bleibt ein erfolgreicher Sync.
- F-CON-05: Ein Sync aendert keine Nutzerfelder bestehender Buchungen.
- F-DUP-02: Ein zweiter identischer Sync legt keine Buchung neu an.
- F-DUP-04: Eine Vormerkung wird gebucht; Notiz und Pruef-Flag gehen auf die
  neue Buchung ueber.
- N-SEC-02: Antworten und Protokolle enthalten keine PIN, TAN oder Dialogdaten.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fints.exceptions import FinTSClientError, FinTSClientPINError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models import Account, BankAccess, ImportRun, Transaction
from finue.services import sync_persist as sync_persist_module
from finue.services.bank_access import create_access
from finue.services.bank_error_defaults import seed_defaults
from finue.services.standing_orders import ORDERS_UNAVAILABLE_MESSAGE, get_orders_state
from finue.services.sync_registry import SyncRegistry, SyncStatus
from finue.services.sync_service import SyncService, SyncTanConflictError

pytestmark = pytest.mark.integration

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_flow", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FakeBankError = fints_fake.FakeBankError
FakeCamtEntry = fints_fake.FakeCamtEntry
FakeTanChallenge = fints_fake.FakeTanChallenge
FakeTanFlow = fints_fake.FakeTanFlow
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_IBAN_GIRO = fints_fake.FAKE_IBAN_GIRO
FAKE_IBAN_CARD = fints_fake.FAKE_IBAN_CARD
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
fake_camt_xml = fints_fake.fake_camt_xml
fake_transaction = fints_fake.fake_transaction
script_happy_path = fints_fake.script_happy_path
script_ing_90_days = fints_fake.script_ing_90_days
script_tan_text = fints_fake.script_tan_text

UTC = timezone.utc
CHECKED_AT = datetime(2026, 11, 24, 9, 30, tzinfo=UTC)


@dataclass()
class SyncEnvironment:
    """Testumgebung aus Datenbank, Zugang und aktiven FinTS-Konten."""

    factory: sessionmaker[Session]
    access: BankAccess
    accounts: tuple[Account, ...]
    db_path: Path


class SleepSpy:
    """Injizierte Wartezeit ohne echten ``sleep`` (Laufzeit unter 60 s)."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class SharedClientFactory:
    """Liefert immer denselben Fake-Client, damit Skript und TAN-Zustand bleiben."""

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


class BlockingAccountsClient(FakeClient):
    """Blockiert den Kontenabruf bis zur Testfreigabe (paralleler Doppelstart)."""

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

    def get_sepa_accounts(self) -> Any:
        self._started.set()
        if not self._release.wait(5):
            raise RuntimeError("Testfreigabe fuer den blockierten Sync fehlt")
        return super().get_sepa_accounts()


class RecordingClient(FakeClient):
    """Fake-Client, der die Zeitraeume des camt-Abrufs mitschreibt."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.xml_ranges: list[tuple[date | None, date | None]] = []

    def get_transactions_xml(
        self,
        account: Any,
        start_date: date | None = None,
        end_date: date | None = None,
        supported_camt_messages: Any = None,
    ) -> Any:
        self.xml_ranges.append((start_date, end_date))
        return super().get_transactions_xml(
            account, start_date, end_date, supported_camt_messages
        )


class FakeClock:
    """Injizierbare Uhr fuer den deterministischen 5-Minuten-Timeout."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _settings() -> Settings:
    """Settings ohne .env, damit der Test isoliert bleibt."""

    return Settings(_env_file=None, fints_product_id=FAKE_PRODUCT_ID)


def _make_environment(tmp_path: Path, *, count: int = 2) -> SyncEnvironment:
    """Legt eine Datei-SQLite mit Zugang, Standardfehlern und FinTS-Konten an."""

    db_path = tmp_path / f"sync-flow-{count}.db"
    engine = create_db_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    specs = (
        ("Erfundenes Girokonto", FAKE_IBAN_GIRO),
        ("Erfundene Kreditkarte", FAKE_IBAN_CARD),
    )[:count]

    with factory() as session:
        access = create_access(
            session,
            blz=fints_fake.FAKE_BANK_IDENTIFIER,
            login_name=FAKE_USER_ID,
            store_pin=False,
            bank_name="Erfundene Testbank",
        )
        access.bank.fints_url = fints_fake.FAKE_SERVER
        accounts = [
            Account(
                name=name,
                iban=iban,
                currency="EUR",
                source="fints",
                bank_access_id=access.id,
                active=True,
            )
            for name, iban in specs
        ]
        session.add_all(accounts)
        # F-SYN-06: 9800 und 9010 sind nur mit den Standardeintraegen wiederholbar.
        seed_defaults(session)
        session.commit()
        access_id = access.id
        account_ids = tuple(account.id for account in accounts)

    with factory() as session:
        stored_access = session.get(BankAccess, access_id)
        stored_accounts = [session.get(Account, account_id) for account_id in account_ids]
        assert stored_access is not None
        assert all(account is not None for account in stored_accounts)

    return SyncEnvironment(
        factory=factory,
        access=stored_access,
        accounts=tuple(account for account in stored_accounts if account is not None),
        db_path=db_path,
    )


def _service(
    environment: SyncEnvironment,
    client_factory: Callable[..., Any],
    *,
    registry: SyncRegistry | None = None,
    sleep: Callable[[float], None] | None = None,
    wait_seconds: float = 0.0,
) -> SyncService:
    return SyncService(
        environment.factory,
        registry=registry,
        settings=_settings(),
        client_factory=client_factory,
        sleep=sleep if sleep is not None else SleepSpy(),
        wait_seconds=wait_seconds,
    )


def _runs(environment: SyncEnvironment) -> list[ImportRun]:
    """Liest alle Sync-Protokolle stabil sortiert."""

    with environment.factory() as session:
        return list(
            session.scalars(
                select(ImportRun)
                .where(ImportRun.kind == "sync")
                .order_by(ImportRun.id)
            ).all()
        )


def _account(environment: SyncEnvironment, account_id: int) -> Account:
    with environment.factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        return account


def _transactions(environment: SyncEnvironment, account_id: int) -> list[Transaction]:
    with environment.factory() as session:
        return list(
            session.scalars(
                select(Transaction)
                .where(Transaction.account_id == account_id)
                .order_by(Transaction.status, Transaction.id)
            ).all()
        )


def test_batch_app_approval_polls_to_success_f_syn_01_f_syn_04_f_syn_07(
    tmp_path: Path,
) -> None:
    """F-SYN-01/04/07: Batch mit zwei App-Freigaben und einem Protokoll je Konto."""

    environment = _make_environment(tmp_path)
    script = FakeScript(
        name="batch_app",
        accounts=fake_accounts(),
        balance=(
            FakeTanFlow(
                challenges=(
                    FakeTanChallenge("Bitte in der App bestaetigen (1/2)", decoupled=True),
                    FakeTanChallenge("Bitte in der App bestaetigen (2/2)", decoupled=True),
                ),
                result=fake_balance("100,00"),
            ),
            fake_balance("200,00"),
        ),
    )
    shared = SharedClientFactory(script)
    service = _service(environment, shared)

    paused = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert paused.status == "wartet_auf_tan"
    assert paused.challenge is not None
    assert paused.challenge.kind == "app"
    assert paused.job.status is SyncStatus.AWAITING_TAN
    assert paused.approval_polls == 0

    first_poll = service.poll_app_approval(paused.job_id)

    assert first_poll.status == "wartet_auf_tan"
    assert first_poll.approval_polls == 1
    assert first_poll.challenge is not None
    assert first_poll.challenge.challenge.endswith("(2/2)")

    completed = service.poll_app_approval(paused.job_id)

    assert completed.status == "erfolgreich"
    assert completed.approval_polls == 2
    assert completed.job.status is SyncStatus.COMPLETED
    assert [item.status for item in completed.account_results] == [
        "erfolgreich",
        "erfolgreich",
    ]

    first, second = environment.accounts
    assert _account(environment, first.id).balance_cents == 10_000
    assert _account(environment, second.id).balance_cents == 20_000

    runs = _runs(environment)
    assert len(runs) == 2
    assert {run.job_id for run in runs} == {completed.job_id}
    assert all(run.status == "erfolgreich" for run in runs)
    assert shared.client is not None
    assert "send_tan" in shared.client.method_log


def test_single_sync_writes_one_run_without_batch_job_id_f_syn_02(
    tmp_path: Path,
) -> None:
    """F-SYN-02: Einzel-Sync genau eines Kontos ohne Batch-job_id."""

    environment = _make_environment(tmp_path)
    shared = SharedClientFactory(
        FakeScript(
            name="single",
            accounts=fake_accounts(),
            balance=(fake_balance("50,00"),),
        )
    )
    service = _service(environment, shared)
    account, untouched = environment.accounts

    result = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert result.status == "erfolgreich"
    assert [item.account_id for item in result.account_results] == [account.id]

    runs = _runs(environment)
    assert len(runs) == 1
    assert runs[0].account_id == account.id
    assert runs[0].job_id is None
    assert _account(environment, account.id).balance_cents == 5_000
    assert _account(environment, untouched.id).balance_cents is None


def test_ing_90_day_challenge_is_plaintext_f_syn_05(tmp_path: Path) -> None:
    """F-SYN-05: ING-90-Tage-Hinweis erreicht den Aufrufer als Klartext."""

    environment = _make_environment(tmp_path, count=1)
    shared = SharedClientFactory(script_ing_90_days())
    service = _service(environment, shared)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert paused.status == "wartet_auf_tan"
    assert paused.challenge is not None
    assert paused.challenge.kind == "app"
    assert "ING" in paused.challenge.challenge
    assert "90 Tage" in paused.challenge.challenge

    completed = service.poll_app_approval(paused.job_id)

    assert completed.status == "erfolgreich"
    run = _runs(environment)[0]
    message = run.message or ""
    assert FAKE_PIN not in message
    assert paused.challenge.challenge not in message


def test_retryable_9800_and_9010_retry_with_fresh_dialog_f_syn_06(
    tmp_path: Path,
) -> None:
    """F-SYN-06: 9800 und 9010 werden zweimal mit frischem Dialog wiederholt."""

    environment = _make_environment(tmp_path, count=1)
    sleep = SleepSpy()
    script = FakeScript(
        name="retry_9800_9010",
        accounts=fake_accounts(),
        balance=(
            FakeBankError(
                FinTSClientPINError,
                "9800",
                "9800: Dialog abgebrochen (erfundener Testfall)",
            ),
            FakeBankError(
                FinTSClientError,
                "9010",
                "9010: Verarbeitung nicht moeglich (erfundener Testfall)",
            ),
            fake_balance("100,00"),
        ),
    )
    shared = SharedClientFactory(script)
    service = _service(environment, shared, sleep=sleep, wait_seconds=0.25)
    account = environment.accounts[0]

    result = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert result.status == "erfolgreich"
    assert result.account_results[0].attempts == 3
    assert sleep.calls == [0.25, 0.25]
    assert shared.created == 3  # Erstversuch plus je ein frischer Dialog
    assert _account(environment, account.id).balance_cents == 10_000

    runs = _runs(environment)
    assert len(runs) == 1
    assert runs[0].status == "erfolgreich"


def test_concurrent_double_start_returns_same_running_job_f_syn_11(
    tmp_path: Path,
) -> None:
    """F-SYN-11: Zwei Threads starten; der zweite erhaelt denselben Job."""

    environment = _make_environment(tmp_path)
    started = threading.Event()
    release = threading.Event()
    shared = SharedClientFactory(
        FakeScript(
            name="double_start",
            accounts=fake_accounts(),
            balance=(fake_balance("100,00"), fake_balance("150,00")),
        ),
        client_type=BlockingAccountsClient,
        client_kwargs={"started": started, "release": release},
    )
    service = _service(environment, shared)
    first_result: dict[str, Any] = {}

    def run_first() -> None:
        try:
            first_result["value"] = service.start_sync(
                environment.access.id, supplied_pin=FAKE_PIN
            )
        except BaseException as exc:  # pragma: no cover - Fehler im Thread sichtbar machen
            first_result["error"] = exc

    thread = threading.Thread(target=run_first)
    thread.start()
    second = None
    try:
        assert started.wait(5), "Der erste Sync hat den Dialog nicht geoeffnet."
        second = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)
        assert second.reused is True
        assert second.status == "laeuft"
        assert second.job.status is SyncStatus.RUNNING
        assert service.registry.is_active(environment.access.id) is True
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert "error" not in first_result
    first = first_result["value"]
    assert first.status == "erfolgreich"
    assert second is not None
    assert second.job is first.job
    assert shared.created == 1

    runs = _runs(environment)
    assert len(runs) == 2
    assert {run.job_id for run in runs} == {first.job_id}


def test_partial_failure_one_account_does_not_stop_other_f_syn_10(
    tmp_path: Path,
) -> None:
    """F-SYN-10: Ein fehlerhaftes Konto laesst das zweite Konto erfolgreich laufen."""

    environment = _make_environment(tmp_path)
    shared = SharedClientFactory(
        FakeScript(
            name="partial_failure",
            accounts=fake_accounts(),
            balance=(
                FakeBankError(
                    FinTSClientError,
                    "1234",
                    "1234: erfundener Bankfehler ohne Mapping",
                ),
                fake_balance("200,00"),
            ),
        )
    )
    service = _service(environment, shared)
    first, second = environment.accounts

    result = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert result.status == "teilweise"
    by_account = {item.account_id: item for item in result.account_results}
    assert by_account[first.id].status == "fehler"
    assert by_account[second.id].status == "erfolgreich"
    assert _account(environment, first.id).balance_cents is None
    assert _account(environment, second.id).balance_cents == 20_000

    runs = _runs(environment)
    assert len(runs) == 2
    assert {run.job_id for run in runs} == {result.job_id}
    failed = next(run for run in runs if run.account_id == first.id)
    assert failed.status == "fehler"
    message = failed.message or ""
    assert "1234: erfundener Bankfehler ohne Mapping" in message
    assert "Bitte den Code im Fehler-Mapping ergaenzen" in message


def test_cancel_paused_batch_keeps_completed_account_f_syn_10(tmp_path: Path) -> None:
    """F-SYN-10: Abbruch in der TAN-Pause erhaelt das fertige Konto."""

    environment = _make_environment(tmp_path)
    shared = SharedClientFactory(
        FakeScript(
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
    )
    service = _service(environment, shared)
    first, second = environment.accounts

    paused = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)
    assert paused.status == "wartet_auf_tan"
    states = {run.account_id: run.status for run in _runs(environment)}
    assert states == {first.id: "erfolgreich", second.id: "wartet_auf_tan"}

    cancelled = service.cancel(paused.job_id)

    assert cancelled.status == "abgebrochen"
    assert cancelled.job.status is SyncStatus.ABORTED
    by_account = {item.account_id: item.status for item in cancelled.account_results}
    assert by_account == {first.id: "erfolgreich", second.id: "abgebrochen"}
    assert service.registry.is_active(environment.access.id) is False

    states = {run.account_id: run.status for run in _runs(environment)}
    assert states == {first.id: "erfolgreich", second.id: "abgebrochen"}
    assert _account(environment, first.id).balance_cents == 10_000
    assert _account(environment, second.id).balance_cents is None


def test_timeout_after_five_minutes_records_zeitueberschreitung_f_syn_04_f_syn_10(
    tmp_path: Path,
) -> None:
    """F-SYN-04/10: Abgelaufene TAN-Frist verwirft den Dialog und protokolliert."""

    environment = _make_environment(tmp_path, count=1)
    start = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    registry = SyncRegistry(clock=clock)
    shared = SharedClientFactory(script_tan_text())
    service = _service(environment, shared, registry=registry)
    account = environment.accounts[0]

    paused = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert paused.status == "wartet_auf_tan"
    assert paused.job.deadline == start + timedelta(minutes=5)

    clock.advance(timedelta(minutes=5))
    timed_out = service.expire_tan_timeout(paused.job_id)

    assert timed_out is not None
    assert timed_out.status == "zeitueberschreitung"
    assert timed_out.job.status is SyncStatus.TIMED_OUT
    assert timed_out.account_results[0].status == "zeitueberschreitung"
    assert service.registry.is_active(environment.access.id) is False

    run = _runs(environment)[0]
    assert run.status == "zeitueberschreitung"
    assert "TAN-Freigabe ist nicht rechtzeitig erfolgt" in (run.message or "")

    with pytest.raises(SyncTanConflictError):
        service.submit_tan(paused.job_id, "4711")


def test_pending_becomes_booked_and_keeps_note_f_dup_04(tmp_path: Path) -> None:
    """F-DUP-04: Vormerkung wird gebucht; Notiz und Pruef-Flag gehen mit."""

    environment = _make_environment(tmp_path, count=1)
    pending = fake_transaction(
        counterparty="Erfundene Vermietung GmbH",
        counterparty_iban=FAKE_IBAN_CARD,
        purpose="Erfundene Miete Januar",
        amount="50,00",
        status="D",
        reference="FAKE-PENDING-1",
        pending=True,
    )
    booked = fake_transaction(
        counterparty="Erfundene Vermietung GmbH",
        counterparty_iban=FAKE_IBAN_CARD,
        purpose="Erfundene Miete Januar",
        amount="50,00",
        status="D",
        reference="FAKE-PENDING-1",
    )
    shared = SharedClientFactory(
        FakeScript(
            name="pending_then_booked",
            accounts=fake_accounts(),
            balance=(fake_balance("500,00"), fake_balance("450,00")),
            transactions=([pending], [booked]),
        )
    )
    service = _service(environment, shared)
    account = environment.accounts[0]

    first = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert first.status == "erfolgreich"

    rows = _transactions(environment, account.id)
    assert len(rows) == 1
    assert rows[0].status == "vorgemerkt"
    pending_id = rows[0].id

    with environment.factory() as session:
        stored_pending = session.get(Transaction, pending_id)
        assert stored_pending is not None
        stored_pending.note = "Eigene Notiz zur Vormerkung"
        stored_pending.checked = True
        stored_pending.checked_at = CHECKED_AT
        session.commit()

    second = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert second.status == "erfolgreich"

    rows = _transactions(environment, account.id)
    booked_rows = [row for row in rows if row.status == "gebucht"]
    pending_rows = [row for row in rows if row.status == "vorgemerkt"]
    assert len(booked_rows) == 1
    assert pending_rows == []
    assert booked_rows[0].note == "Eigene Notiz zur Vormerkung"
    assert booked_rows[0].checked is True
    assert booked_rows[0].checked_at == CHECKED_AT

    runs = _runs(environment)
    assert len(runs) == 2
    assert runs[-1].new_count == 1


def test_fetch_range_uses_14_day_overlap_and_persists_balance_f_syn_03(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-SYN-03: Abrufzeitraum ab letztem Erfolg minus 14 Tage; Saldo uebernommen."""

    environment = _make_environment(tmp_path, count=1)
    account = environment.accounts[0]
    fixed_now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sync_persist_module, "utc_now", lambda: fixed_now)

    with environment.factory() as session:
        session.add(
            ImportRun(
                account_id=account.id,
                job_id="alter-lauf",
                kind="sync",
                status="erfolgreich",
                started_at=datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
                finished_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
                message="Alter erfolgreicher Sync.",
            )
        )
        session.commit()

    script = FakeScript(
        name="range_balance",
        accounts=fake_accounts(),
        balance=(fake_balance("1234,56"),),
        camt_booked=(
            fake_camt_xml(
                (
                    FakeCamtEntry(
                        amount="10.00",
                        direction="DBIT",
                        booking_date=date(2026, 10, 10),
                        value_date=date(2026, 10, 10),
                        counterparty="Erfundene camt GmbH",
                        counterparty_iban=FAKE_IBAN_CARD,
                        purpose="Erfundene Buchung im Zeitraum",
                        reference="FAKE-RANGE-1",
                        end_to_end="FAKE-RANGE-1-E2E",
                    ),
                )
            ),
        ),
    )
    shared = SharedClientFactory(script, client_type=RecordingClient)
    service = _service(environment, shared)

    result = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert result.status == "erfolgreich"
    assert shared.client is not None
    assert shared.client.xml_ranges == [(date(2026, 10, 6), date(2026, 11, 26))]
    assert _account(environment, account.id).balance_cents == 123_456


def test_second_identical_sync_adds_nothing_and_keeps_user_fields_f_dup_02_f_con_05(
    tmp_path: Path,
) -> None:
    """F-DUP-02/F-CON-05: Zweiter Sync 0 neu und Nutzerfelder unveraendert."""

    environment = _make_environment(tmp_path, count=1)
    camt = fake_camt_xml(
        (
            FakeCamtEntry(
                amount="42.00",
                direction="DBIT",
                booking_date=date(2026, 11, 20),
                value_date=date(2026, 11, 20),
                counterparty="Erfundene Beispiel GmbH",
                counterparty_iban=FAKE_IBAN_CARD,
                purpose="Erfundene Rechnung 2026-001",
                reference="FAKE-IDEMPOTENT-1",
                end_to_end="FAKE-IDEMPOTENT-1-E2E",
            ),
        )
    )
    shared = SharedClientFactory(
        FakeScript(
            name="idempotent",
            accounts=fake_accounts(),
            balance=(fake_balance("1234,56"), fake_balance("1234,56")),
            camt_booked=(camt,),
        )
    )
    service = _service(environment, shared)
    account = environment.accounts[0]

    first = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert first.status == "erfolgreich"

    rows = _transactions(environment, account.id)
    assert len(rows) == 1
    stored_id = rows[0].id

    with environment.factory() as session:
        row = session.get(Transaction, stored_id)
        assert row is not None
        row.note = "Eigene Notiz"
        row.checked = True
        row.checked_at = CHECKED_AT
        row.reconciled = True
        row.reconciled_at = CHECKED_AT
        row.external_ref = "USER-REF"
        session.commit()
        version_before = row.version

    second = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )
    assert second.status == "erfolgreich"

    runs = _runs(environment)
    assert len(runs) == 2
    assert runs[-1].new_count == 0
    assert runs[-1].skipped_count == 1

    rows = _transactions(environment, account.id)
    assert len(rows) == 1
    unchanged = rows[0]
    assert unchanged.id == stored_id
    assert unchanged.note == "Eigene Notiz"
    assert unchanged.checked is True
    assert unchanged.checked_at == CHECKED_AT
    assert unchanged.reconciled is True
    assert unchanged.reconciled_at == CHECKED_AT
    assert unchanged.external_ref == "USER-REF"
    assert unchanged.version == version_before
    assert _account(environment, account.id).balance_cents == 123_456


def test_orders_unsupported_keeps_sync_successful_f_auf_05(tmp_path: Path) -> None:
    """F-AUF-05: Fehlender Auftragsabruf ist kein Sync-Fehler."""

    environment = _make_environment(tmp_path, count=1)
    shared = SharedClientFactory(
        FakeScript(
            name="orders_unsupported_e2e",
            accounts=fake_accounts(),
            balance=(fake_balance("100,00"),),
            orders_state="unsupported",
        )
    )
    service = _service(environment, shared)
    account = environment.accounts[0]

    result = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert result.status == "erfolgreich"
    assert result.account_results[0].status == "erfolgreich"

    with environment.factory() as session:
        state = get_orders_state(session, account.id)
    assert state is not None
    assert state.status == "unavailable"
    assert state.orders == ()
    assert state.message == ORDERS_UNAVAILABLE_MESSAGE

    run = _runs(environment)[0]
    assert run.status == "erfolgreich"
    assert (run.message or "").startswith("Sync abgeschlossen")


def test_sync_uses_only_read_operations_f_auf_03_d8(tmp_path: Path) -> None:
    """F-AUF-03/D8: Der Fake meldet ausschliesslich lesende Operationen."""

    environment = _make_environment(tmp_path, count=1)
    shared = SharedClientFactory(script_happy_path())
    service = _service(environment, shared)
    account = environment.accounts[0]

    result = service.start_sync(
        environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN
    )

    assert result.status == "erfolgreich"
    client = shared.client
    assert client is not None
    calls = set(client.method_log)
    allowed = fints_fake.FAKE_READ_OPERATIONS | fints_fake.DIALOG_OPERATIONS
    assert calls <= allowed
    assert calls.isdisjoint(fints_fake.WRITE_OPERATIONS)
    for name in fints_fake.WRITE_OPERATIONS:
        assert not hasattr(client, name)
