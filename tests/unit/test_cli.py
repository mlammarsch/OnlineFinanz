"""Tests fuer die Kommandozeile ``python -m finue`` (T-037).

Referenziert N-OPS-01 (Demo-Daten per Befehl) und N-DB-03 (Schema-Migration
per Befehl). ``migrate`` und ``demo`` laufen gegen eine temporaere
Datei-Datenbank (``tmp_path``). ``serve`` wird ohne echten Serverstart
strukturell geprueft: die Argumente werden geparst und ``uvicorn.run`` wird
ersetzt, um die fest kodierte Worker-Anzahl (D3) zu belegen.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from finue import __main__ as cli
from finue.config import get_settings
from finue.db.engine import create_db_engine, make_session_factory
from finue.db.migrate import current_revision, head_revision
from finue.demo import DEMO_TRANSACTION_COUNT
from finue.models.account import Account
from finue.models.transaction import Transaction


@pytest.fixture()
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Setzt ``DATABASE_URL`` auf eine temporaere SQLite-Datei und leert den Settings-Cache."""
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        yield url
    finally:
        get_settings.cache_clear()


def _counts(url: str) -> tuple[int, int]:
    """Zaehlt Konten und Buchungen in der Datenbank unter ``url``."""
    engine = create_db_engine(url)
    try:
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            accounts = session.query(Account).count()
            transactions = session.query(Transaction).count()
            return accounts, transactions
    finally:
        engine.dispose()


def test_migrate_command_upgrades_schema(database_url: str) -> None:
    """``migrate`` hebt die temporaere Datenbank auf head (N-DB-03)."""
    assert cli.main(["migrate"]) == 0

    engine = create_db_engine(database_url)
    try:
        assert head_revision() is not None
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()


def test_demo_command_seeds_demo_data(database_url: str) -> None:
    """``demo`` migriert und legt den Demo-Bestand an (N-OPS-01)."""
    assert cli.main(["demo"]) == 0

    accounts, transactions = _counts(database_url)
    assert accounts == 3
    assert transactions == DEMO_TRANSACTION_COUNT


def test_demo_reset_makes_command_repeatable(database_url: str) -> None:
    """``demo --reset`` laeuft nach einem vorherigen Lauf ohne Fehler erneut (N-OPS-01)."""
    assert cli.main(["demo"]) == 0
    assert cli.main(["demo", "--reset"]) == 0

    accounts, transactions = _counts(database_url)
    assert accounts == 3
    assert transactions == DEMO_TRANSACTION_COUNT


def test_serve_runs_single_worker_and_targets_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve`` startet ``finue.main:app`` mit genau einem Worker (D3).

    ``uvicorn.run`` wird ersetzt, damit kein echter Server startet; die
    aufgezeichneten Argumente belegen Modulpfad und Worker-Anzahl.
    """
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve"]) == 0
    assert captured["app"] == "finue.main:app"
    assert captured["workers"] == 1
    assert captured["host"] == cli.DEFAULT_HOST
    assert captured["port"] == cli.DEFAULT_PORT


def test_serve_does_not_offer_worker_option() -> None:
    """Die Worker-Anzahl ist nicht per CLI aenderbar (D3): ``--workers`` wird abgelehnt."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--workers", "2"])

    assert excinfo.value.code == 2
