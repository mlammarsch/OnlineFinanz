"""Test fuer den programmatischen Migrations-Runner (T-030).

Referenziert N-DB-03 (Schema-Versionierung ueber Alembic-Migrationen) und
N-OPS-04 (Start erkennt den Schemastand und hebt ihn bei Bedarf an).

Der Test arbeitet gegen eine frisch erstellte, leere Datei-basierte
SQLite-Datenbank im temporaeren Verzeichnis (``tmp_path``, wird von pytest
aufgeraeumt): vor dem Upgrade ist sie nicht aktuell, nach ``upgrade_to_head``
ist sie aktuell.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from finue.db.engine import create_db_engine
from finue.db.migrate import (
    current_revision,
    head_revision,
    is_schema_current,
    upgrade_to_head,
)
from finue.log_filter import LogFilter
from finue.logging_setup import JSONFormatter, configure_logging


def test_is_schema_current_transitions_after_upgrade(tmp_path: Path) -> None:
    """Leere DB ist nicht aktuell, nach upgrade_to_head ist sie aktuell (N-DB-03, N-OPS-04)."""
    db_path = tmp_path / "migrate_runner.db"
    url = f"sqlite:///{db_path.as_posix()}"

    engine = create_db_engine(url)
    try:
        # Frische DB ohne Schema: noch keine Revision, nicht aktuell.
        assert current_revision(engine) is None
        assert is_schema_current(engine) is False

        upgrade_to_head(url)

        # Nach dem Upgrade steht die DB auf head und gilt als aktuell.
        assert head_revision() is not None
        assert current_revision(engine) == head_revision()
        assert is_schema_current(engine) is True
    finally:
        engine.dispose()


def test_upgrade_to_head_keeps_project_logging(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """upgrade_to_head laesst das Projekt-Logging unangetastet (N-SEC-02, T-033).

    Regression: ``env.py`` rief ``fileConfig`` auf, sobald eine Config-Datei
    gesetzt war. Das ersetzte die Root-Handler durch Alembics Standard-Handler
    (stderr, ohne Maskierung) und hebelte damit den Projekt-LogFilter aus.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        configure_logging("INFO")
        url = f"sqlite:///{(tmp_path / 'logging_runner.db').as_posix()}"
        upgrade_to_head(url)

        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler.formatter, JSONFormatter)
        assert any(isinstance(log_filter, LogFilter) for log_filter in handler.filters)

        # Der Filter muss auch wirken, nicht nur vorhanden sein (N-SEC-02).
        logging.getLogger("finue.test").warning("pin=123456")
        captured = capsys.readouterr()
        assert "123456" not in captured.out
        assert "***" in captured.out
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
