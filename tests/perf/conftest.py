"""Fixtures fuer den Performance-Block (N-PERF-01, F-BUC-06).

Der Datenbestand (5 Konten, 3 Jahre, 100.000 synthetische Buchungen) wird
**einmal je Testlauf** erzeugt; alle Perf-Tests teilen ihn (Session-Scope), damit
die Erzeugungszeit die Messungen nicht verfaelscht. Der Generator liegt in
``scripts/perf_data.py`` (zugleich CLI); hier wird nur der Skriptordner in den
Importpfad aufgenommen und die Engine/Session bereitgestellt.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from finue.db.engine import create_db_engine, make_session_factory

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from perf_data import (  # noqa: E402  (Import erst nach sys.path-Anpassung)
    REPORT_ENV,
    PerfInfo,
    generate_perf_database,
)


@pytest.fixture(scope="session", autouse=True)
def _fresh_report() -> None:
    """Leert den Messbericht zu Beginn des Perf-Laufs (falls gesetzt)."""

    report = os.environ.get(REPORT_ENV)
    if report:
        Path(report).write_text("", encoding="utf-8")


@pytest.fixture(scope="session")
def perf_db(tmp_path_factory: pytest.TempPathFactory) -> PerfInfo:
    """Erzeugt den Performance-Bestand einmal je Testlauf."""

    db_path = tmp_path_factory.mktemp("perf") / "perf.db"
    return generate_perf_database("sqlite:///" + db_path.as_posix())


@pytest.fixture(scope="session")
def perf_engine(perf_db: PerfInfo) -> Iterator[Engine]:
    """Engine auf den Performance-Bestand (Session-Scope)."""

    engine = create_db_engine(perf_db.db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def perf_session(perf_engine: Engine) -> Iterator[Session]:
    """Lese-Session auf den Performance-Bestand (warme Verbindung)."""

    session = make_session_factory(perf_engine)()
    try:
        yield session
    finally:
        session.close()
