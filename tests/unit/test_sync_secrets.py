"""Geheimnis-Leckagetests fuer den gesamten lesenden Sync-Pfad (T-080).

Referenzierte Anforderungen:

- N-SEC-01: PINs liegen nur Fernet-verschluesselt in der Datenbank; der
  Schluessel bleibt ausserhalb von Datenbank, Repo und Logs.
- N-SEC-02: PIN, TAN, Schluessel, Passphrasen und Banktexte mit Geheimnissen
  erscheinen weder in JSON-Logs noch in ``ImportRun.message``,
  ``ImportRunError``, Fehler-Mapping-Texten, Exception-Strings/``repr`` oder
  einem Datenbankdump.
- N-SEC-03: Registry-Dialogdaten bleiben ausschliesslich im Arbeitsspeicher
  und gelangen weder in eine Datei noch in die Datenbank.
- F-SYN-04: TAN-Abfrage, falsche TAN, Abbruch und Timeout.
- F-SYN-05/F-SYN-07: Bankfehler werden mit Klartext protokolliert, aber ohne
  Fremdgeheimnisse; je Konto entsteht eine Protokollzeile.

Alle Marker-Geheimnisse, Konten, Betraege und Banktexte sind frei erfunden.
Der Fake-Client liefert alle Antworten; es findet kein Bankzugriff statt (D8).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import pickle
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fints.client import NeedTANResponse
from fints.exceptions import FinTSClientError, FinTSClientPINError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.logging_setup import configure_logging
from finue.models.account import Account
from finue.models.bank import BankAccess
from finue.models.import_run import ImportRun
from finue.services.bank_access import PinRequiredError, create_access
from finue.services.bank_error_defaults import seed_defaults
from finue.services.bank_errors import ResolvedError, resolve_error
from finue.services.fints_client import TanHandoff, TanRequired
from finue.services.fints_types import (
    AccessLockedError,
    FinTSBankError,
    PinWrongError,
    SyncCancelledError,
    TanChallenge,
    TanTimeoutError,
)
from finue.services.pin_crypto import PinCipher, create_dev_key_file
from finue.services.sync_registry import SyncEvent, SyncRegistry, SyncStatus
from finue.services.sync_service import SyncService

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fints_fake.py"
_SPEC = importlib.util.spec_from_file_location("fints_fake_sync_secrets", _FIXTURE)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - Ladefehler der Umgebung
    raise RuntimeError("fints_fake.py konnte nicht geladen werden")
fints_fake = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fints_fake
_SPEC.loader.exec_module(fints_fake)

FakeClient = fints_fake.FakeFinTS3PinTanClient
FakeScript = fints_fake.FakeScript
FakeBankError = fints_fake.FakeBankError
FAKE_PRODUCT_ID = fints_fake.FAKE_PRODUCT_ID
FAKE_USER_ID = fints_fake.FAKE_USER_ID
FAKE_BANK_IDENTIFIER = fints_fake.FAKE_BANK_IDENTIFIER
FAKE_SERVER = fints_fake.FAKE_SERVER
fake_accounts = fints_fake.fake_accounts
fake_balance = fints_fake.fake_balance
script_happy_path = fints_fake.script_happy_path
script_tan_text = fints_fake.script_tan_text

UTC = timezone.utc

#: Frei erfundene Marker-Geheimnisse. Sie sind absichtlich eindeutig, damit
#: jedes Vorkommen in einer Ausgabe zweifelsfrei erkannt wird.
PIN_MARKER = "FANTASIE-PIN-MARKER-4711"
TAN_MARKER = "FANTASIE-TAN-MARKER-8151"
PASSPHRASE_MARKER = "FANTASIE-PASSPHRASE-MARKER-9001"
DIALOG_MARKER = b"FANTASIE-DIALOG-MARKER-7007"
CLIENT_MARKER = b"FANTASIE-CLIENT-MARKER-7008"

_MARKERS: tuple[str, ...] = (
    PIN_MARKER,
    TAN_MARKER,
    PASSPHRASE_MARKER,
    DIALOG_MARKER.decode("ascii"),
    CLIENT_MARKER.decode("ascii"),
)
_MARKER_BYTES: tuple[bytes, ...] = tuple(marker.encode("utf-8") for marker in _MARKERS)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Stelle Handler und Level des Root-Loggers nach jedem Test wieder her."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


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
            self.client = self._client_type(script=self._script, **self._client_kwargs, **kwargs)
        return self.client


class WrongTanClient(FakeClient):
    """Fake-Client, der eine falsche TAN mit Banktext samt Marker ablehnt."""

    def __init__(self, *args: Any, wrong_tan: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._wrong_tan = wrong_tan

    def send_tan(self, challenge: Any, tan: str | None = None) -> Any:
        if tan is not None and tan == self._wrong_tan:
            text = f"9942: PIN falsch; tan={tan}"
            error = FinTSClientPINError(text)
            setattr(error, "bank_code", "9942")
            setattr(error, "original_text", text)
            raise error
        return super().send_tan(challenge, tan)


class DialogSecretClient(FakeClient):
    """Fake-Client mit erkennbaren Geheimnis-Markern in den Dialogdaten.

    ``pause_dialog`` und ``deconstruct`` liefern absichtlich Markerbytes. So
    laesst sich pruefen, dass die Registry diese Daten nur im Arbeitsspeicher
    haelt (N-SEC-03).
    """

    def pause_dialog(self) -> bytes:
        super().pause_dialog()
        return DIALOG_MARKER

    def deconstruct(self, including_private: bool = False) -> bytes:
        super().deconstruct(including_private)
        return CLIENT_MARKER


class FakeClock:
    """Injizierbare Uhr fuer den deterministischen 5-Minuten-Timeout."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass()
class SecretEnvironment:
    """Testumgebung aus Datenbank, Zugang und Konten."""

    factory: sessionmaker[Session]
    access: BankAccess
    accounts: tuple[Account, ...]
    db_path: Path
    key_file: Path | None = None


def _settings(key_file: Path | None = None) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "fints_product_id": FAKE_PRODUCT_ID,
    }
    if key_file is not None:
        kwargs["key_file"] = key_file
    return Settings(**kwargs)


def _make_environment(
    tmp_path: Path,
    *,
    store_pin: bool = False,
    cipher: PinCipher | None = None,
    key_file: Path | None = None,
    pin: str = PIN_MARKER,
) -> SecretEnvironment:
    """Legt eine Datei-SQLite mit Zugang und zwei aktiven FinTS-Konten an."""

    db_path = tmp_path / "secrets.db"
    engine = create_db_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        access = create_access(
            session,
            blz=FAKE_BANK_IDENTIFIER,
            login_name=FAKE_USER_ID,
            cipher=cipher,
            store_pin=store_pin,
            pin=pin if store_pin else None,
            bank_name="Erfundene Testbank",
        )
        access.bank.fints_url = FAKE_SERVER
        first = Account(
            name="Erfundenes Girokonto",
            iban=fints_fake.FAKE_IBAN_GIRO,
            currency="EUR",
            source="fints",
            bank_access_id=access.id,
            active=True,
        )
        second = Account(
            name="Erfundene Kreditkarte",
            iban=fints_fake.FAKE_IBAN_CARD,
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
        reloaded = session.get(BankAccess, access_id)
        assert reloaded is not None
        accounts = tuple(session.get(Account, account_id) for account_id in account_ids)
        assert all(account is not None for account in accounts)

    return SecretEnvironment(
        factory=factory,
        access=reloaded,
        accounts=accounts,  # type: ignore[arg-type]
        db_path=db_path,
        key_file=key_file,
    )


def _service(
    environment: SecretEnvironment,
    client_factory: Callable[..., Any],
    *,
    registry: SyncRegistry | None = None,
    cipher: PinCipher | None = None,
) -> SyncService:
    return SyncService(
        environment.factory,
        registry=registry if registry is not None else SyncRegistry(),
        settings=_settings(environment.key_file),
        client_factory=client_factory,
        cipher=cipher,
        sleep=lambda _seconds: None,
        wait_seconds=0.0,
    )


def _probe_log() -> None:
    """Schreibe einen Logeintrag mit Geheimnis-Markern ueber den JSON-Handler."""

    logging.getLogger("t080.probe").info(
        "Kontext pin=%s tan=%s passphrase=%s",
        PIN_MARKER,
        TAN_MARKER,
        PASSPHRASE_MARKER,
    )


def _json_log_output(capsys: pytest.CaptureFixture[str]) -> tuple[str, list[dict[str, Any]]]:
    """Lies die JSON-Logausgabe und pruefe, dass jede Zeile gueltiges JSON ist."""

    out = capsys.readouterr().out
    records: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return out, records


def _assert_surfaces_clean(surfaces: dict[str, str | bytes]) -> None:
    """Pruefe, dass kein Marker in einer der uebergebenen Ausgaben vorkommt."""

    for label, value in surfaces.items():
        if isinstance(value, bytes):
            for marker in _MARKER_BYTES:
                assert marker not in value, f"{marker!r} gefunden in {label}"
        else:
            for marker in _MARKERS:
                assert marker not in value, f"{marker} gefunden in {label}"


def _db_dump(factory: sessionmaker[Session]) -> str:
    """Vollstaendiger Spalten-Dump aller ORM-Tabellen."""

    chunks: list[str] = []
    with factory() as session:
        for table in Base.metadata.sorted_tables:
            for row in session.execute(select(table)).mappings():
                for column, value in row.items():
                    chunks.append(f"{table.name}.{column}={value!r}")
    return "\n".join(chunks)


def _db_file_bytes(db_path: Path) -> bytes:
    """Rohe SQLite-Datei inklusive WAL und Shared-Memory-Datei."""

    data = db_path.read_bytes()
    for suffix in ("-wal", "-shm"):
        extra = db_path.with_name(db_path.name + suffix)
        if extra.exists():
            data += extra.read_bytes()
    return data


def _import_run_text(factory: sessionmaker[Session]) -> str:
    """Alle protokollierten Meldungen und Fehlerzeilen als Text."""

    chunks: list[str] = []
    with factory() as session:
        for run in session.scalars(select(ImportRun)):
            chunks.append(repr(run.message))
            for error in run.errors:
                chunks.append(repr(error.code))
                chunks.append(repr(error.message))
    return "\n".join(chunks)


def _handoff_with_markers() -> TanHandoff[object]:
    """TanHandoff, dessen Dialog- und Clientdaten Marker enthalten."""

    response = NeedTANResponse(
        None,
        SimpleNamespace(challenge="Bitte TAN eingeben", challenge_hhduc=None),
        resume_method="send_tan",
        tan_request_structured=False,
        decoupled=False,
    )
    return TanHandoff(
        challenge=TanChallenge(kind="text", challenge="Bitte TAN eingeben"),
        dialog_state=DIALOG_MARKER,
        client_state=CLIENT_MARKER,
        response=response,
        mapper=lambda value: value,
    )


def _domain_exception_texts() -> list[str]:
    """Strings und ``repr`` der geheimnisrelevanten Fehlerklassen."""

    handoff = _handoff_with_markers()
    return [
        str(FinTSBankError("7777", f"pin={PIN_MARKER}")),
        repr(FinTSBankError("7777", f"pin={PIN_MARKER}")),
        str(PinWrongError("9942", f"tan={TAN_MARKER}")),
        repr(PinWrongError("9942", f"tan={TAN_MARKER}")),
        str(AccessLockedError("3938", f"passphrase={PASSPHRASE_MARKER}")),
        repr(AccessLockedError("3938", f"passphrase={PASSPHRASE_MARKER}")),
        str(TanTimeoutError()),
        repr(TanTimeoutError()),
        str(SyncCancelledError()),
        repr(SyncCancelledError()),
        str(PinRequiredError()),
        repr(PinRequiredError()),
        repr(handoff),
        repr(TanRequired(handoff)),
        repr(
            ResolvedError(
                code="7777",
                message=f"pin={PIN_MARKER}",
                recommendation="Empfehlung",
                retryable=False,
                source="unbekannt",
            )
        ),
    ]


def test_domain_errors_mapping_and_repr_are_secret_free_n_sec_02(tmp_path: Path) -> None:
    """N-SEC-02: Fehlerklassen, Mapping-Repr und Dialog-Handoff leaken nichts."""

    for text in _domain_exception_texts():
        for marker in _MARKERS:
            assert marker not in text, f"{marker} in Fehlerdarstellung: {text}"

    environment = _make_environment(tmp_path)
    original = f"7777: Fremdtext der Bank; pin={PIN_MARKER} tan={TAN_MARKER} passphrase={PASSPHRASE_MARKER}"
    with environment.factory() as session:
        resolved = resolve_error(session, "7777", None, original)

    assert resolved.source == "unbekannt"
    assert "Fremdtext der Bank" in resolved.message
    assert "***" in resolved.message
    for marker in _MARKERS:
        assert marker not in resolved.message
        assert marker not in resolved.recommendation
        assert marker not in repr(resolved)


def test_success_keeps_markers_out_of_logs_db_and_result_f_syn_07_n_sec_02(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F-SYN-07, N-SEC-02: Erfolgreicher Sync ohne Geheimnisse in Ausgaben."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(script_happy_path())
    service = _service(environment, factory)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=PIN_MARKER)
    _probe_log()

    assert result.status == "erfolgreich"
    assert result.account_results[0].status == "erfolgreich"
    out, records = _json_log_output(capsys)
    assert records
    assert "***" in out

    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "DB-Datei": _db_file_bytes(environment.db_path),
        "SyncResult.repr": repr(result),
        "SyncJob.repr": repr(result.job),
        "AccountSyncResult.repr": repr(result.account_results[0]),
    }
    _assert_surfaces_clean(surfaces)

    with environment.factory() as session:
        run = session.scalars(select(ImportRun).where(ImportRun.kind == "sync")).one()
        assert run.status == "erfolgreich"
        assert "Sync abgeschlossen" in (run.message or "")


def test_wrong_pin_keeps_bank_text_markers_out_of_every_surface_n_sec_02(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """N-SEC-02: Falsche PIN mit markiertem Banktext bleibt in der DB sauber."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    script = FakeScript(
        name="wrong_pin_marker",
        accounts=fake_accounts(),
        balance=(
            FakeBankError(
                FinTSClientPINError,
                "9942",
                f"9942: PIN falsch; pin={PIN_MARKER} tan={TAN_MARKER}",
            ),
        ),
    )
    factory = SharedClientFactory(script)
    service = _service(environment, factory)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=PIN_MARKER)
    _probe_log()

    assert result.status == "fehler"
    assert result.account_results[0].status == "fehler"
    assert result.job.status is SyncStatus.FAILED

    out, _records = _json_log_output(capsys)
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "DB-Datei": _db_file_bytes(environment.db_path),
        "SyncResult.repr": repr(result),
        "message": result.account_results[0].message or "",
    }
    _assert_surfaces_clean(surfaces)

    with environment.factory() as session:
        run = session.scalars(select(ImportRun).where(ImportRun.kind == "sync")).one()
        assert "PIN nicht akzeptiert" in (run.message or "")
        assert run.errors[0].code == "9942"


def test_wrong_tan_is_reported_without_tan_or_bank_markers_n_sec_02(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F-SYN-04, N-SEC-02: Falsche TAN wird ohne TAN-Wert protokolliert."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    factory = SharedClientFactory(
        script_tan_text(),
        client_type=WrongTanClient,
        client_kwargs={"wrong_tan": TAN_MARKER},
    )
    service = _service(environment, factory)
    account = environment.accounts[0]

    paused = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=PIN_MARKER)
    assert paused.status == "wartet_auf_tan"
    result = service.submit_tan(paused.job_id, TAN_MARKER)
    _probe_log()

    assert result.status == "fehler"
    assert result.account_results[0].status == "fehler"
    assert result.job.status is SyncStatus.FAILED

    out, _records = _json_log_output(capsys)
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "DB-Datei": _db_file_bytes(environment.db_path),
        "SyncResult.repr": repr(result),
        "message": result.account_results[0].message or "",
    }
    _assert_surfaces_clean(surfaces)

    with environment.factory() as session:
        run = session.scalars(select(ImportRun).where(ImportRun.kind == "sync")).one()
        assert "PIN nicht akzeptiert" in (run.message or "")
        assert run.errors[0].code == "9942"


def test_bank_error_foreign_text_is_masked_before_persistence_f_syn_05_n_sec_02(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F-SYN-05, N-SEC-02: Unbekannter Code mit Fremdtext wird maskiert."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    foreign = f"7777: Fremdtext der Bank; pin={PIN_MARKER} tan={TAN_MARKER} passphrase={PASSPHRASE_MARKER}"
    script = FakeScript(
        name="unknown_bank_error",
        accounts=fake_accounts(),
        balance=(FakeBankError(FinTSClientError, "7777", foreign),),
    )
    factory = SharedClientFactory(script)
    service = _service(environment, factory)
    account = environment.accounts[0]

    result = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=PIN_MARKER)
    _probe_log()

    assert result.status == "fehler"
    assert result.account_results[0].status == "fehler"

    out, _records = _json_log_output(capsys)
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "DB-Datei": _db_file_bytes(environment.db_path),
        "SyncResult.repr": repr(result),
        "message": result.account_results[0].message or "",
    }
    _assert_surfaces_clean(surfaces)

    with environment.factory() as session:
        run = session.scalars(select(ImportRun).where(ImportRun.kind == "sync")).one()
        # F-SYN-05: Der Originaltext bleibt erkennbar, die Geheimnisse nicht.
        assert "Fremdtext der Bank" in (run.message or "")
        assert "***" in (run.message or "")
        assert "***" in run.errors[0].message


def test_cancel_keeps_completed_account_and_leaks_no_dialog_state_f_syn_10_n_sec_03(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F-SYN-10, N-SEC-03: Abbruch mit TAN-Pause ohne Dialogdaten in Dateien."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    script = FakeScript(
        name="cancel_with_dialog_marker",
        accounts=fake_accounts(),
        balance=(
            fake_balance("100,00"),
            fints_fake.FakeTanFlow(
                challenges=(fints_fake.FakeTanChallenge("Bitte TAN fuer Konto 2 eingeben"),),
                result=fake_balance("200,00"),
            ),
        ),
    )
    factory = SharedClientFactory(script, client_type=DialogSecretClient)
    service = _service(environment, factory)

    paused = service.start_sync(environment.access.id, supplied_pin=PIN_MARKER)
    assert paused.status == "wartet_auf_tan"
    assert paused.job.dialog_state is not None
    handoff = paused.job.dialog_state
    assert isinstance(handoff, TanHandoff)
    assert handoff.dialog_state == DIALOG_MARKER
    assert handoff.client_state == CLIENT_MARKER

    cancelled = service.cancel(paused.job_id)
    _probe_log()

    assert cancelled.status == "abgebrochen"
    assert cancelled.job.status is SyncStatus.ABORTED
    assert cancelled.job.dialog_state is None
    first, second = environment.accounts
    by_account = {item.account_id: item.status for item in cancelled.account_results}
    assert by_account[first.id] == "erfolgreich"
    assert by_account[second.id] == "abgebrochen"

    out, _records = _json_log_output(capsys)
    file_markers = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "Dateien im Testordner": file_markers,
        "SyncResult.repr": repr(cancelled),
        "SyncJob.repr": repr(cancelled.job),
        "Handoff.repr": repr(handoff),
    }
    _assert_surfaces_clean(surfaces)


def test_timeout_discards_dialog_without_any_secret_f_syn_04_n_sec_03(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F-SYN-04, N-SEC-03: Timeout verwirft Dialogdaten ohne Leckage."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    start = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    registry = SyncRegistry(clock=clock)
    factory = SharedClientFactory(script_tan_text(), client_type=DialogSecretClient)
    service = _service(environment, factory, registry=registry)
    account = environment.accounts[0]

    paused = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=PIN_MARKER)
    assert paused.status == "wartet_auf_tan"
    assert paused.job.dialog_state is not None

    clock.advance(timedelta(minutes=5))
    timed_out = service.expire_tan_timeout(paused.job_id)
    _probe_log()

    assert timed_out is not None
    assert timed_out.status == "zeitueberschreitung"
    assert timed_out.job.status is SyncStatus.TIMED_OUT
    assert timed_out.job.dialog_state is None
    assert timed_out.account_results[0].status == "zeitueberschreitung"

    out, _records = _json_log_output(capsys)
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "DB-Datei": _db_file_bytes(environment.db_path),
        "SyncResult.repr": repr(timed_out),
        "SyncJob.repr": repr(timed_out.job),
    }
    _assert_surfaces_clean(surfaces)


def test_bank_access_stores_pin_encrypted_and_never_leaks_it_n_sec_01_n_sec_02(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """N-SEC-01/02: Angelegter Zugang speichert die PIN nur verschluesselt."""

    configure_logging("DEBUG")
    key_file = create_dev_key_file(tmp_path / "dev.key")
    cipher = PinCipher(settings=_settings(key_file))
    environment = _make_environment(
        tmp_path,
        store_pin=True,
        cipher=cipher,
        key_file=key_file,
    )
    factory = SharedClientFactory(script_happy_path())
    service = _service(environment, factory, cipher=cipher)
    account = environment.accounts[0]

    token = environment.access.pin_encrypted
    assert token is not None
    assert PIN_MARKER.encode("utf-8") not in token
    assert cipher.decrypt_pin(token) == PIN_MARKER
    assert environment.access.store_pin is True

    result = service.start_sync(environment.access.id, account_ids=[account.id])
    _probe_log()

    assert result.status == "erfolgreich"

    key_text = key_file.read_text(encoding="ascii").strip()
    out, _records = _json_log_output(capsys)
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "DB-Datei": _db_file_bytes(environment.db_path),
        "Zugang.repr": repr(environment.access),
        "Zugang.str": str(environment.access),
        "Cipher.repr": repr(cipher),
        "SyncResult.repr": repr(result),
    }
    _assert_surfaces_clean(surfaces)
    for label, value in surfaces.items():
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        assert key_text not in text, f"Schluessel gefunden in {label}"


def test_registry_dialog_state_stays_in_memory_only_n_sec_03(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """N-SEC-03: Dialogdaten stehen weder in Dateien/DB noch in ``repr``."""

    configure_logging("DEBUG")
    environment = _make_environment(tmp_path)
    events: list[SyncEvent] = []
    registry = SyncRegistry()
    registry.add_listener(events.append)
    factory = SharedClientFactory(script_tan_text(), client_type=DialogSecretClient)
    service = _service(environment, factory, registry=registry)
    account = environment.accounts[0]

    paused = service.start_sync(environment.access.id, account_ids=[account.id], supplied_pin=PIN_MARKER)
    _probe_log()

    assert paused.status == "wartet_auf_tan"
    job = paused.job
    handoff = job.dialog_state
    assert isinstance(handoff, TanHandoff)
    assert handoff.dialog_state == DIALOG_MARKER
    assert handoff.client_state == CLIENT_MARKER

    with pytest.raises(TypeError) as pickle_error:
        pickle.dumps(job)
    for marker in _MARKERS:
        assert marker not in str(pickle_error.value)

    out, _records = _json_log_output(capsys)
    file_markers = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    surfaces: dict[str, str | bytes] = {
        "JSON-Log": out,
        "ImportRun.message/ImportRunError": _import_run_text(environment.factory),
        "DB-Dump": _db_dump(environment.factory),
        "Dateien im Testordner": file_markers,
        "SyncResult.repr": repr(paused),
        "SyncJob.repr": repr(job),
        "Handoff.repr": repr(handoff),
        "SyncEvent.repr": "\n".join(repr(event) for event in events),
    }
    _assert_surfaces_clean(surfaces)

    assert registry.is_active(environment.access.id) is True
    assert service.get_challenge(paused.job_id) == paused.challenge
