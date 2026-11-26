"""Marker fuer die Integrationstests der FinTS-Sync-Fluesse."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Registriert den Marker ``integration`` ohne Aenderung an pyproject."""

    config.addinivalue_line(
        "markers",
        "integration: End-to-End-Test mit Temp-SQLite und Fake-FinTS-Client",
    )
