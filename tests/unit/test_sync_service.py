"""Tests fuer die Sync-Orchestrierung Batch/Einzel mit Fehler-Mapping (T-076).

Abgedeckt:

- F-SYN-01: Batch aller aktiven FinTS-Konten eines Zugangs in genau einem
  Dialog.
- F-SYN-02: Einzel-Sync genau eines Kontos.
- F-SYN-11: Ein zweiter Anstoss liefert den laufenden Job zurueck.
- F-SYN-03: Der Zeitraum aus
  :func:`finue.services.sync_persist.compute_fetch_range` wird an den
  Umsatzabruf weitergereicht.
- F-SYN-05: Falsche PIN und unbekannte Codes liefern Klartext und
  Handlungsempfehlung ohne Geheimnisse.
- F-SYN-06: Code 9800 wird mit frischem Dialog und injizierter Wartezeit
  wiederholt.
- F-SYN-07: Je Konto entsteht ein ImportRun; im Batch teilen alle Laeufe die
  ``job_id``. Der Gesamtstatus ist ``erfolgreich``, ``teilweise`` oder
  ``fehler``.
- F-SYN-10: Ein fehlerhaftes Konto stoppt die uebrigen Konten nicht.
- F-AUF-05: Der Auftragsabruf ist ``unsupported`` und macht den Sync nicht
  fehlerhaft.
- D8, N-SEC-02: Ausschliesslich erfundene Testdaten und der Fake-Client; PIN
  und Dialogdaten bleiben im Arbeitsspeicher.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fints.exceptions import FinTSClientError, FinTSClientPINError
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
from finue.services.sync_registry import SyncStatus
from finue.services.sync_service import SyncService

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_service", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FakeBankError = fints_fake.FakeBankError
FakeCamtEntry = fints_fake.FakeCamtEntry
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_IBAN_GIRO = fints_fake.FAKE_IBAN_GIRO
FAKE_IBAN_CARD = fints_fake.FAKE_IBAN_CARD
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
fake_camt_xml = fints_fake.fake_camt_xml
script_wrong_pin = fints_fake.script_wrong_pin

UTC = timezone.utc


@dataclass()
class SyncEnvironment:
    """Testumgebung aus Datenbank, Zugang und Konten."""

    factory: sessionmaker[Session]
    access: BankAccess
    accounts: tuple[Account, ...]


class SleepSpy:
    """Injizierte Wartezeit ohne echten ``sleep``."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class SharedClientFactory:
    """Liefert immer denselben Fake-Client und zaehlt Dialogaufbauten.

    Der erste Aufruf erzeugt den Client mit dem Skript. Weitere Aufrufe
    (Wiederholung mit frischem Dialog) liefern dieselbe Instanz, damit der
    Skript-Fortschritt erhalten bleibt.
    """

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
            self.client = self._client_type(script=self._script, **self._client_kwargs, **kwargs)
        return self.client


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
        return super().get_transactions_xml(account, start_date, end_date, supported_camt_messages)


class BlockingClient(FakeClient):
    """Fake-Client, der den Kontenabruf bis zur Testfreigabe blockiert."""

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


def _settings() -> Settings:
    """Settings ohne .env, damit der Test isoliert bleibt."""
    return Settings(_env_file=None, fints_product_id=FAKE_PRODUCT_ID)


def _make_environment(tmp_path: Path) -> SyncEnvironment:
    """Legt eine Datei-SQLite mit Zugang und zwei aktiven FinTS-Konten an."""
    db_path = (tmp_path / "sync.db").as_posix()
    engine = create_db_engine(f"sqlite:///{db_path}")
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

    return SyncEnvironment(factory=factory, access=access, accounts=accounts)  # type: ignore[arg-type]


def _two_account_script(*outcomes: Any) -> FakeScript:
    """Skript mit zwei Saldoantworten in der Reihenfolge der Konten."""
    return FakeScript(
        name="batch",
        accounts=fake_accounts(),
        balance=outcomes,
    )


def _service(
    environment: SyncEnvironment,
    client_factory: Callable[..., Any],
    *,
    sleep: Callable[[float], None] | None = None,
    wait_seconds: float = 0.25,
) -> SyncService:
    return SyncService(
        environment.factory,
        settings=_settings(),
        client_factory=client_factory,
        sleep=sleep if sleep is not None else SleepSpy(),
        wait_seconds=wait_seconds,
    )


def _runs(factory: sessionmaker[Session]) -> list[ImportRun]:
    """Liest alle Sync-Protokolle stabil sortiert."""
    with factory() as session:
        return list(session.scalars(select(ImportRun).where(ImportRun.kind == "sync").order_by(ImportRun.id)).all())


def test_batch_uses_one_dialog_and_shared_job_id_f_syn_01_f_syn_07(
    tmp_path: Path,
) -> None:
    """F-SYN-01, F-SYN-07: Batch in genau einem Dialog mit gemeinsamer job_id."""
    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(_two_account_script(fake_balance("100,00"), fake_balance("200,00")))
    service = _service(environment, factory)

    result = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert result.reused is False
    assert result.status == "erfolgreich"
    assert len(result.account_results) == 2
    assert all(item.status == "erfolgreich" for item in result.account_results)
    assert factory.created == 1  # genau ein Dialog fuer den ganzen Batch

    runs = _runs(environment.factory)
    assert len(runs) == 2
    assert {run.job_id for run in runs} == {result.job.id}
    assert all(run.status == "erfolgreich" for run in runs)
    assert result.job.status is SyncStatus.COMPLETED


def test_single_sync_has_no_batch_job_id_f_syn_02(tmp_path: Path) -> None:
    """F-SYN-02: Einzel-Sync genau eines Kontos ohne Batch-job_id."""
    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(_two_account_script(fake_balance()))
    service = _service(environment, factory)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)

    assert result.reused is False
    assert result.status == "erfolgreich"
    assert [item.account_id for item in result.account_results] == [account.id]
    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].job_id is None
    assert factory.created == 1


def test_faulty_account_does_not_stop_others_f_syn_10_f_syn_07(
    tmp_path: Path,
) -> None:
    """F-SYN-10, F-SYN-07: Fehler des ersten Kontos stoppt das zweite nicht."""
    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(
        _two_account_script(
            FakeBankError(
                FinTSClientError,
                "1234",
                "1234: erfundener Bankfehler ohne Mapping",
            ),
            fake_balance("200,00"),
        )
    )
    service = _service(environment, factory)

    result = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert result.status == "teilweise"
    by_account = {item.account_id: item for item in result.account_results}
    first, second = environment.accounts
    assert by_account[first.id].status == "fehler"
    assert by_account[second.id].status == "erfolgreich"

    runs = _runs(environment.factory)
    assert len(runs) == 2
    assert {run.job_id for run in runs} == {result.job.id}
    failed = next(run for run in runs if run.account_id == first.id)
    assert failed.status == "fehler"
    assert "1234: erfundener Bankfehler ohne Mapping" in (failed.message or "")
    assert "Bitte den Code im Fehler-Mapping ergaenzen" in (failed.message or "")


def test_retryable_9800_is_retried_with_fresh_dialog_f_syn_06(
    tmp_path: Path,
) -> None:
    """F-SYN-06: Code 9800 wird mit frischem Dialog und Wartezeit wiederholt."""
    environment = _make_environment(tmp_path)
    sleep = SleepSpy()
    factory = SharedClientFactory(
        _two_account_script(
            FakeBankError(FinTSClientPINError, "9800", "9800: Dialog abgebrochen"),
            fake_balance("100,00"),
        )
    )
    service = _service(environment, factory, sleep=sleep, wait_seconds=0.25)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)

    assert result.status == "erfolgreich"
    assert result.account_results[0].attempts == 2
    assert factory.created == 2  # Erstversuch plus frischer Dialog je Wiederholung
    assert sleep.calls == [0.25]
    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "erfolgreich"


def test_wrong_pin_fails_with_plaintext_without_secret_f_syn_05(
    tmp_path: Path,
) -> None:
    """F-SYN-05, N-SEC-02: falsche PIN sofort als Fehler mit Klartext."""
    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(script_wrong_pin())
    service = _service(environment, factory)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)

    assert result.status == "fehler"
    assert result.account_results[0].attempts == 1
    assert factory.created == 1  # PIN-Fehler wird nie wiederholt

    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "fehler"
    message = runs[0].message or ""
    assert "PIN nicht akzeptiert" in message
    assert "PIN pruefen" in message
    assert FAKE_PIN not in message


def test_double_start_returns_running_job_f_syn_11(tmp_path: Path) -> None:
    """F-SYN-11: der zweite Anstoss erhaelt denselben laufenden Job."""
    environment = _make_environment(tmp_path)
    started = threading.Event()
    release = threading.Event()
    factory = SharedClientFactory(
        _two_account_script(fake_balance("100,00"), fake_balance("150,00")),
        client_type=BlockingClient,
        client_kwargs={"started": started, "release": release},
    )
    service = _service(environment, factory)
    first_result: dict[str, Any] = {}

    def run_first() -> None:
        try:
            first_result["value"] = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)
        except BaseException as exc:  # pragma: no cover - Fehler im Thread sichtbar machen
            first_result["error"] = exc

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert started.wait(5), "Der erste Sync hat den Dialog nicht geoeffnet."

        second = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

        assert second.reused is True
        assert second.status == "laeuft"
        assert second.job.status is SyncStatus.RUNNING
        assert service.registry.is_active(environment.access.id) is True
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert "error" not in first_result
    first = first_result["value"]
    assert first.status == "erfolgreich"
    assert second.job is first.job
    assert factory.created == 1
    assert len(_runs(environment.factory)) == 2


def test_fetch_range_from_last_successful_sync_is_passed_f_syn_03(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-SYN-03: compute_fetch_range liefert den Zeitraum an den Abruf."""
    environment = _make_environment(tmp_path)
    account = environment.accounts[0]
    fixed_now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

    with environment.factory() as session:
        session.add(
            ImportRun(
                account_id=account.id,
                job_id="alter-lauf",
                kind="sync",
                status="erfolgreich",
                started_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                finished_at=datetime(2026, 10, 1, 9, 5, tzinfo=UTC),
                message="Alter erfolgreicher Sync.",
            )
        )
        session.commit()

    from finue.services import sync_persist as sync_persist_module

    monkeypatch.setattr(sync_persist_module, "utc_now", lambda: fixed_now)

    script = FakeScript(
        name="range",
        accounts=fake_accounts(),
        balance=(fake_balance("100,00"),),
        camt_booked=(
            fake_camt_xml(
                (
                    FakeCamtEntry(
                        amount="10.00",
                        direction="DBIT",
                        booking_date=date(2026, 10, 2),
                        value_date=date(2026, 10, 2),
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
    factory = SharedClientFactory(script, client_type=RecordingClient)
    service = _service(environment, factory)

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)

    assert result.status == "erfolgreich"
    assert factory.client is not None
    assert factory.client.xml_ranges[0] == (date(2026, 9, 17), date(2026, 11, 26))


def test_orders_unsupported_keeps_sync_successful_f_auf_05(tmp_path: Path) -> None:
    """F-AUF-05: fehlender Auftragsabruf ist kein Sync-Fehler."""
    environment = _make_environment(tmp_path)
    script = FakeScript(
        name="orders_unsupported",
        accounts=fake_accounts(),
        balance=(fake_balance("100,00"),),
        orders_state="unsupported",
    )
    factory = SharedClientFactory(script)
    service = _service(environment, factory)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)

    assert result.status == "erfolgreich"
    assert result.account_results[0].status == "erfolgreich"
    runs = _runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "erfolgreich"
    assert (runs[0].message or "").startswith("Sync abgeschlossen")
