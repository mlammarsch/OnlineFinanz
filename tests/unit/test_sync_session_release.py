"""Tests fuer die DB-Session-Freigabe waehrend der TAN-Pause (T-111, N-DB-04).

Referenzierte Anforderungen:

- N-DB-04: Waehrend ``AWAITING_TAN`` haelt der Sync weder eine DB-Session noch
  eine Verbindung offen (der Verbindungspool ist leer), und parallele
  Schreibvorgaenge laufen ohne Sperrwartezeit (SQLite WAL mit
  ``busy_timeout``). Nach der Freigabe oeffnet der Lauf eine frische Session;
  das Ergebnis bleibt dasselbe. Abbruch und Timeout geben Session und Dialog
  frei. Der Laufzustand traegt waehrend der Pause nur IDs und Werte, kein
  ORM-Objekt.
- F-SYN-04/F-SYN-07/F-SYN-10: Pause, Wiederaufnahme, Abbruch und Timeout
  verhalten sich unveraendert.

Die Tests pruefen den Verbindungspool direkt (``Engine.pool.checkedout()``):
Vor dem Fix blieb die Session des Laufs offen und der Zaehler stand waehrend
der Pause auf 1. Alle Konten, Betraege und TANs sind frei erfunden (D8); der
Fake-Client liefert die Antworten, es findet kein Bankzugriff statt.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import BankAccess
from finue.models.import_run import ImportRun
from finue.services.bank_access import create_access
from finue.services.bank_error_defaults import seed_defaults
from finue.services.sync_registry import SyncRegistry, SyncStatus
from finue.services.sync_service import SyncService

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_release", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FAKE_PIN = fints_fake.FAKE_PIN
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_IBAN_GIRO = fints_fake.FAKE_IBAN_GIRO
FAKE_IBAN_CARD = fints_fake.FAKE_IBAN_CARD
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
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
class ReleaseEnvironment:
    """Testumgebung aus Datenbank, Engine, Zugang und Konten."""

    factory: sessionmaker[Session]
    engine: Engine
    access: BankAccess
    accounts: tuple[Account, ...]
    db_path: Path


class SharedClientFactory:
    """Liefert immer denselben Fake-Client fuer alle Dialoge eines Tests."""

    def __init__(self, script: FakeScript, *, client_type: type[Any] = FakeClient) -> None:
        self._script = script
        self._client_type = client_type
        self.client: Any | None = None

    def __call__(self, **kwargs: Any) -> Any:
        if self.client is None:
            self.client = self._client_type(script=self._script, **kwargs)
        return self.client


class RecordingTanClient(FakeClient):
    """Fake-Client, der Text-TANs mitschreibt."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tan_values: list[str] = []

    def send_tan(self, challenge: Any, tan: str | None = None) -> Any:
        if tan:
            self.tan_values.append(tan)
        return super().send_tan(challenge, tan)


def _settings() -> Settings:
    return Settings(_env_file=None, fints_product_id=FAKE_PRODUCT_ID)


def _make_environment(base: Path, name: str = "release.db") -> ReleaseEnvironment:
    """Legt eine Datei-SQLite mit Zugang und zwei aktiven FinTS-Konten an."""

    directory = base / name.replace(".db", "")
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / name
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
        stored_access = session.get(BankAccess, access_id)
        assert stored_access is not None
        accounts = tuple(session.get(Account, account_id) for account_id in account_ids)
        assert all(account is not None for account in accounts)

    return ReleaseEnvironment(
        factory=factory,
        engine=engine,
        access=stored_access,
        accounts=accounts,  # type: ignore[arg-type]
        db_path=db_path,
    )


def _service(
    environment: ReleaseEnvironment,
    client_factory: Callable[..., Any],
    *,
    registry: SyncRegistry | None = None,
) -> SyncService:
    return SyncService(
        environment.factory,
        registry=registry if registry is not None else SyncRegistry(),
        settings=_settings(),
        client_factory=client_factory,
        sleep=lambda _seconds: None,
        wait_seconds=0.0,
    )


def _sync_runs(factory: sessionmaker[Session]) -> list[ImportRun]:
    with factory() as session:
        return list(session.scalars(select(ImportRun).where(ImportRun.kind == "sync").order_by(ImportRun.id)).all())


def _pause(
    environment: ReleaseEnvironment,
    client_factory: Callable[..., Any],
) -> tuple[SyncService, Any]:
    """Startet einen Einzel-Sync bis zur TAN-Pause."""

    service = _service(environment, client_factory)
    account = environment.accounts[0]
    paused = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)
    assert paused.status == "wartet_auf_tan"
    return service, paused


def test_no_session_or_connection_open_while_awaiting_tan_n_db_04(tmp_path: Path) -> None:
    """N-DB-04: waehrend AWAITING_TAN ist keine Session und keine Verbindung offen."""

    environment = _make_environment(tmp_path)
    service, paused = _pause(environment, SharedClientFactory(script_tan_text(), client_type=RecordingTanClient))

    # Der Verbindungspool ist leer: die Session des Laufs ist geschlossen.
    assert environment.engine.pool.checkedout() == 0
    context = service._runs[paused.job_id]
    assert context.session is None

    # Die Protokollzeile ``wartet_auf_tan`` wurde vor dem Schliessen committet.
    runs = _sync_runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].status == "wartet_auf_tan"
    assert runs[0].account_id == environment.accounts[0].id

    # Der Laufzustand traegt nur IDs und Werte, kein ORM-Objekt.
    for field in dataclasses.fields(context):
        value = getattr(context, field.name)
        assert not isinstance(value, (Session, Account, BankAccess, ImportRun))


def test_parallel_write_runs_without_lock_wait_while_awaiting_tan_n_db_04(tmp_path: Path) -> None:
    """N-DB-04: paralleler Schreibvorgang wartet nicht auf eine Sperre des Syncs."""

    environment = _make_environment(tmp_path)
    service, paused = _pause(environment, SharedClientFactory(script_tan_text()))
    account = environment.accounts[0]

    assert environment.engine.pool.checkedout() == 0

    started = time.monotonic()
    with environment.factory() as session:
        session.execute(text("SELECT 1"))
        row = session.get(Account, account.id)
        assert row is not None
        row.balance_cents = 4242
        session.commit()
    elapsed = time.monotonic() - started

    # Ohne offene Schreibsperre; ein blockierter Schreibvorgang wuerde erst
    # nach dem busy_timeout (5 s) durchlaufen.
    assert elapsed < 2.0

    with environment.factory() as session:
        stored = session.get(Account, account.id)
        assert stored is not None
        assert stored.balance_cents == 4242

    # Die Pause bleibt bestehen; der Sync hat die Fremdschreibung nicht beruehrt.
    assert paused.job.status is SyncStatus.AWAITING_TAN
    assert environment.engine.pool.checkedout() == 0
    assert service._runs[paused.job_id].session is None


def test_resume_after_session_release_matches_unpaused_run_n_db_04(tmp_path: Path) -> None:
    """N-DB-04/F-SYN-04: die Wiederaufnahme liefert dasselbe Ergebnis wie ohne Pause."""

    direct_environment = _make_environment(tmp_path, "direct.db")
    direct_script = FakeScript(name="direct", accounts=fake_accounts(), balance=(fake_balance(),))
    direct_service = _service(direct_environment, SharedClientFactory(direct_script))
    direct_account = direct_environment.accounts[0]
    direct_result = direct_service.start_sync(
        direct_environment.access.id,
        account_ids=[direct_account.id],
        supplied_pin=FAKE_PIN,
    )
    assert direct_result.status == "erfolgreich"

    paused_environment = _make_environment(tmp_path, "paused.db")
    factory = SharedClientFactory(script_tan_text(), client_type=RecordingTanClient)
    service, paused = _pause(paused_environment, factory)
    account = paused_environment.accounts[0]

    assert paused_environment.engine.pool.checkedout() == 0
    assert service._runs[paused.job_id].session is None
    waiting_runs = _sync_runs(paused_environment.factory)
    assert len(waiting_runs) == 1
    waiting_run_id = waiting_runs[0].id

    completed = service.submit_tan(paused.job_id, TAN_TEXT)

    assert completed.status == "erfolgreich"
    assert completed.account_results[0].status == "erfolgreich"
    assert completed.job.status is SyncStatus.COMPLETED
    # Nach dem Lauf ist wieder keine Verbindung gebunden.
    assert paused_environment.engine.pool.checkedout() == 0
    assert service._runs.get(paused.job_id) is None

    # Dieselbe Protokollzeile wurde fortgeschrieben (keine Zweitzeile).
    resumed_runs = _sync_runs(paused_environment.factory)
    assert len(resumed_runs) == 1
    assert resumed_runs[0].id == waiting_run_id
    assert resumed_runs[0].status == "erfolgreich"

    # Ergebnisgleichheit zum Lauf ohne TAN-Pause.
    with direct_environment.factory() as session:
        direct_stored = session.get(Account, direct_account.id)
        assert direct_stored is not None
        direct_balance = direct_stored.balance_cents
    with paused_environment.factory() as session:
        paused_stored = session.get(Account, account.id)
        assert paused_stored is not None
        paused_balance = paused_stored.balance_cents

    assert paused_balance == direct_balance
    assert resumed_runs[0].new_count == _sync_runs(direct_environment.factory)[0].new_count
    assert factory.client is not None
    assert factory.client.tan_values == [TAN_TEXT]


def test_cancel_while_awaiting_tan_releases_session_and_dialog_n_db_04(tmp_path: Path) -> None:
    """N-DB-04/F-SYN-10: Abbruch waehrend der Pause gibt Session und Dialog frei."""

    environment = _make_environment(tmp_path)
    service, paused = _pause(environment, SharedClientFactory(script_tan_text()))
    account = environment.accounts[0]

    assert environment.engine.pool.checkedout() == 0

    cancelled = service.cancel(paused.job_id)

    assert cancelled.status == "abgebrochen"
    assert cancelled.job.status is SyncStatus.ABORTED
    assert cancelled.job.dialog_state is None
    assert service._runs.get(paused.job_id) is None
    assert service.registry.is_active(environment.access.id) is False
    assert environment.engine.pool.checkedout() == 0

    runs = _sync_runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].account_id == account.id
    assert runs[0].status == "abgebrochen"


def test_timeout_while_awaiting_tan_releases_session_and_dialog_n_db_04(tmp_path: Path) -> None:
    """N-DB-04/F-SYN-04: der 5-Minuten-Timeout schliesst die Session sauber."""

    environment = _make_environment(tmp_path)
    start = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    registry = SyncRegistry(clock=clock)
    factory = SharedClientFactory(script_tan_text())
    service = _service(environment, factory, registry=registry)
    account = environment.accounts[0]

    paused = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=FAKE_PIN)
    assert paused.status == "wartet_auf_tan"
    assert paused.job.deadline == start + timedelta(minutes=5)
    assert environment.engine.pool.checkedout() == 0
    assert service._runs[paused.job_id].session is None

    clock.advance(timedelta(minutes=5))
    timed_out = service.expire_tan_timeout(paused.job_id)

    assert timed_out is not None
    assert timed_out.status == "zeitueberschreitung"
    assert timed_out.job.status is SyncStatus.TIMED_OUT
    assert timed_out.job.dialog_state is None
    assert service._runs.get(paused.job_id) is None
    assert service.registry.is_active(environment.access.id) is False
    assert environment.engine.pool.checkedout() == 0

    runs = _sync_runs(environment.factory)
    assert len(runs) == 1
    assert runs[0].account_id == account.id
    assert runs[0].status == "zeitueberschreitung"
