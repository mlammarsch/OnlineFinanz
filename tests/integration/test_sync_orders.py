"""Integrationstest: Auftragsbestand im Sync (F-AUF-04, F-AUF-05, F-SYN-03).

Der Test faehrt den vollstaendigen SyncService mit Temp-SQLite und dem
Fake-FinTS-Client (D8) ohne Netz und ohne echte Zugangsdaten:

- F-AUF-04: Ein Abruf mit Auftraegen ersetzt den alten Bestand und setzt den
  Zeitstempel des letzten Abrufs.
- F-AUF-05: ``empty`` und ``unsupported`` brechen den Sync nicht ab; der
  Kontostatus bleibt ``erfolgreich``, der Bereich ist ``unavailable`` ohne
  Zeilen. Datei-Konten bleiben ohne Bereich.
- F-SYN-10: Ein Abbruch waehrend des Auftragsabrufs speichert keine Auftraege
  des laufenden Kontos; der vorherige Bestand bleibt unangetastet.
- F-AUF-03: Der Abruf ist rein lesend; der Fake meldet keine schreibende
  Operation.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models import Account, BankAccess, ImportRun, StandingOrder
from finue.services.bank_access import create_access
from finue.services.fints_types import FetchedStandingOrder
from finue.services.standing_orders import (
    ORDERS_UNAVAILABLE_MESSAGE,
    get_orders_state,
    replace_standing_orders,
)
from finue.services.sync_registry import SyncEvent, SyncStatus
from finue.services.sync_service import SyncService

pytestmark = pytest.mark.integration

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_orders", _FIXTURE)
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
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
script_orders_present = fints_fake.script_orders_present
script_orders_empty = fints_fake.script_orders_empty
script_orders_unsupported = fints_fake.script_orders_unsupported

UTC = timezone.utc
OLD_SEEN = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


@dataclass()
class SyncEnvironment:
    """Testumgebung aus Datenbank, Zugang und einem FinTS-Konto."""

    factory: sessionmaker[Session]
    access: BankAccess
    account: Account


class SharedClientFactory:
    """Liefert immer denselben Fake-Client fuer alle Dialoge."""

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


class BlockingOrdersClient(FakeClient):
    """Blockiert den ersten Auftragsabruf bis zur Testfreigabe."""

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
        self._blocked = False

    def _fetch_with_touchdowns(
        self,
        dialog: object,
        segment_factory: Callable[[str | None], object],
        response_processor: Callable[[object], object],
        *args: object,
        **kwargs: object,
    ) -> object:
        if not self._blocked:
            self._blocked = True
            self._started.set()
            if not self._release.wait(5):
                raise RuntimeError("Testfreigabe fuer den Auftragsabruf fehlt")
        return super()._fetch_with_touchdowns(dialog, segment_factory, response_processor, *args, **kwargs)


def _settings() -> Settings:
    """Settings ohne .env, damit der Test isoliert bleibt."""

    return Settings(_env_file=None, fints_product_id=FAKE_PRODUCT_ID)


def _make_environment(tmp_path: Path) -> SyncEnvironment:
    """Legt eine Datei-SQLite mit Zugang und einem aktiven FinTS-Konto an."""

    db_path = (tmp_path / "orders-sync.db").as_posix()
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
        account = Account(
            name="Erfundenes Girokonto",
            iban=FAKE_IBAN_GIRO,
            currency="EUR",
            source="fints",
            bank_access_id=access.id,
            active=True,
        )
        session.add(account)
        session.commit()
        access_id = access.id
        account_id = account.id

    with factory() as session:
        stored_access = session.get(BankAccess, access_id)
        stored_account = session.get(Account, account_id)
        assert stored_access is not None
        assert stored_account is not None

    return SyncEnvironment(
        factory=factory,
        access=stored_access,
        account=stored_account,
    )


def _script_with_balance(script: FakeScript) -> FakeScript:
    """Ergaenzt ein Skript um genau eine Saldoantwort fuer das Testkonto."""

    return replace(
        script,
        name="orders-e2e",
        accounts=fake_accounts(),
        balance=(fake_balance("100,00"),),
    )


def _service(
    environment: SyncEnvironment,
    client_factory: Callable[..., Any],
) -> SyncService:
    return SyncService(
        environment.factory,
        settings=_settings(),
        client_factory=client_factory,
    )


def _seed_old_order(environment: SyncEnvironment) -> int:
    """Legt einen alten Auftrag an, der ersetzt oder erhalten bleiben muss."""

    old = FetchedStandingOrder(
        kind="dauerauftrag",
        counterparty="Erfundene Altvermieterin",
        counterparty_iban="DE02120300000000202051",
        purpose="Erfundene alte Miete",
        amount_cents=50000,
        currency="EUR",
        interval="monatlich",
        next_execution=date(2026, 12, 1),
    )
    with environment.factory() as session:
        rows = replace_standing_orders(session, environment.account.id, (old,), OLD_SEEN)
        session.commit()
        return rows[0].id


def _stored_orders(environment: SyncEnvironment) -> list[StandingOrder]:
    with environment.factory() as session:
        return list(
            session.scalars(
                select(StandingOrder)
                .where(StandingOrder.account_id == environment.account.id)
                .order_by(StandingOrder.id)
            ).all()
        )


def _state(environment: SyncEnvironment) -> Any:
    with environment.factory() as session:
        return get_orders_state(session, environment.account.id)


def test_sync_replaces_order_stock_end_to_end_f_auf_04(tmp_path: Path) -> None:
    """F-AUF-04: Der Sync ersetzt den alten Bestand und setzt den Zeitstempel."""

    environment = _make_environment(tmp_path)
    _seed_old_order(environment)
    factory = SharedClientFactory(_script_with_balance(script_orders_present()))
    service = _service(environment, factory)

    result = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert result.status == "erfolgreich"
    assert result.account_results[0].status == "erfolgreich"
    assert factory.created == 1

    rows = _stored_orders(environment)
    assert len(rows) == 2
    assert "Erfundene Altvermieterin" not in {row.counterparty for row in rows}
    assert [row.kind for row in rows] == ["dauerauftrag", "terminauftrag"]
    assert rows[0].counterparty == "Erfundene Vermietung GmbH"
    assert rows[0].amount_cents == 75000
    assert rows[1].counterparty == "Erfundene Versicherung AG"
    assert rows[1].amount_cents == 12000
    assert all(row.last_seen_at > OLD_SEEN for row in rows)

    state = _state(environment)
    assert state is not None
    assert state.status == "available"
    assert state.fetched_at is not None
    assert state.fetched_at.tzinfo is not None
    assert state.message is None
    assert len(state.orders) == 2


def test_sync_empty_orders_deletes_stock_and_stays_successful_f_auf_05(
    tmp_path: Path,
) -> None:
    """F-AUF-05: Eine leere Antwort loescht den Bestand, ohne den Sync zu brechen."""

    environment = _make_environment(tmp_path)
    _seed_old_order(environment)
    factory = SharedClientFactory(_script_with_balance(script_orders_empty()))
    service = _service(environment, factory)

    result = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert result.status == "erfolgreich"
    assert _stored_orders(environment) == []

    state = _state(environment)
    assert state is not None
    assert state.status == "unavailable"
    assert state.fetched_at is None
    assert state.orders == ()
    assert state.message == ORDERS_UNAVAILABLE_MESSAGE


def test_sync_unsupported_orders_stays_successful_f_auf_05(tmp_path: Path) -> None:
    """F-AUF-05: Nicht unterstuetzter Abruf ist kein Sync-Fehler."""

    environment = _make_environment(tmp_path)
    _seed_old_order(environment)
    factory = SharedClientFactory(_script_with_balance(script_orders_unsupported()))
    service = _service(environment, factory)

    result = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)

    assert result.status == "erfolgreich"
    assert _stored_orders(environment) == []

    state = _state(environment)
    assert state is not None
    assert state.status == "unavailable"
    assert state.orders == ()
    assert state.message == ORDERS_UNAVAILABLE_MESSAGE
    calls = factory.client.method_log
    assert "get_balance" in calls
    for forbidden in (
        "sepa_transfer",
        "simple_sepa_transfer",
        "sepa_debit",
        "simple_sepa_debit",
    ):
        assert forbidden not in calls


def test_abort_during_orders_saves_no_orders_for_running_account_f_syn_10(
    tmp_path: Path,
) -> None:
    """F-SYN-10: Abbruch im Auftragsabruf laesst den alten Bestand unberuehrt."""

    environment = _make_environment(tmp_path)
    old_id = _seed_old_order(environment)
    started = threading.Event()
    release = threading.Event()
    factory = SharedClientFactory(
        _script_with_balance(script_orders_present()),
        client_type=BlockingOrdersClient,
        client_kwargs={"started": started, "release": release},
    )
    service = _service(environment, factory)
    events: list[SyncEvent] = []
    service.registry.add_listener(events.append)
    results: dict[str, Any] = {}

    def run_sync() -> None:
        try:
            results["value"] = service.start_sync(environment.access.id, supplied_pin=FAKE_PIN)
        except BaseException as exc:  # pragma: no cover - Fehler im Thread sichtbar machen
            results["error"] = exc

    thread = threading.Thread(target=run_sync)
    thread.start()
    try:
        assert started.wait(5), "Der Auftragsabruf wurde nicht erreicht."
        job_id = next(event.job_id for event in events if event.status is SyncStatus.RUNNING)
        cancelled = service.cancel(job_id)
        assert cancelled.status == "laeuft"
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    # F-SYN-10: Der Abbruch waehrend des laufenden Netzaufrufs beendet den Lauf
    # als ``abgebrochen`` (analog zur TAN-Pause), statt den Fehler nach oben zu
    # reichen.
    assert "error" not in results
    assert results["value"].status == "abgebrochen"

    with environment.factory() as session:
        runs = list(
            session.scalars(
                select(ImportRun).where(
                    ImportRun.account_id == environment.account.id,
                    ImportRun.kind == "sync",
                )
            ).all()
        )
    assert len(runs) == 1
    assert runs[0].status == "abgebrochen"

    rows = _stored_orders(environment)
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert rows[0].last_seen_at == OLD_SEEN

    state = _state(environment)
    assert state is not None
    assert state.status == "available"
    assert [row.id for row in state.orders] == [old_id]
