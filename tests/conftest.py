"""Gemeinsame Test-Fixtures der Finanzuebersicht.

Stellt eine App mit temporaerer datei-basierter SQLite-Datenbank (WAL, N-DB-04)
und einen laufenden ``TestClient`` bereit. Die App wird ueber ``create_app`` mit
eigenen Settings gebaut, damit keine globalen Umgebungsvariablen noetig sind
(N-OPS-05). Die Fixtures sind bewusst generisch; Fachtests koennen sie
weiterverwenden.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finue.config import Settings
from finue.main import create_app


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    """Settings mit temporaerer SQLite-Datei (WAL) im Testverzeichnis (N-DB-04)."""

    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    return Settings(database_url=url, data_dir=tmp_path)


@pytest.fixture
def app(sqlite_settings: Settings) -> FastAPI:
    """App-Instanz mit Test-Settings (Lifespan startet erst im ``with``-Block)."""

    return create_app(sqlite_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Laufender ``TestClient`` mit ausgefuehrtem Lifespan (Migration, Container)."""

    with TestClient(app) as test_client:
        yield test_client
