"""Tests für Zeitzonen-Hilfsfunktionen. Implementiert N-OPS-06."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from finue.config import get_settings
from finue.timeutil import get_tz, to_local, utc_now


class TestGetTz:
    """Tests für get_tz()-Funktion."""

    def test_get_tz_default_berlin(self, monkeypatch):
        """Liest Zeitzone Europe/Berlin aus Settings."""
        # Cache leeren
        get_settings.cache_clear()
        # Standard ist Europe/Berlin
        tz = get_tz()
        assert isinstance(tz, ZoneInfo)
        assert str(tz) == "Europe/Berlin"

    def test_get_tz_utc(self, monkeypatch):
        """Liest Zeitzone UTC aus Settings."""
        get_settings.cache_clear()

        # Passe Umgebungsvariable an
        monkeypatch.setenv("TZ", "UTC")
        tz = get_tz()
        assert isinstance(tz, ZoneInfo)
        assert str(tz) == "UTC"

    def test_get_tz_invalid_raises_valueerror(self, monkeypatch):
        """Wirft klare ValueError für ungültige Zeitzone."""
        get_settings.cache_clear()
        monkeypatch.setenv("TZ", "Invalid/Zone")

        with pytest.raises(ValueError) as exc_info:
            get_tz()
        assert "Invalid/Zone" in str(exc_info.value)
        assert "Ungültige Zeitzone" in str(exc_info.value)


class TestUtcNow:
    """Tests für utc_now()-Funktion."""

    def test_utc_now_returns_aware_datetime(self):
        """Gibt aware datetime in UTC zurück."""
        now = utc_now()
        assert isinstance(now, datetime)
        assert now.tzinfo is not None
        assert now.tzinfo == timezone.utc

    def test_utc_now_is_approximate_current_time(self):
        """Gibt aktuelle Zeit zurück (innerhalb Sekunde)."""
        before = datetime.now(timezone.utc)
        result = utc_now()
        after = datetime.now(timezone.utc)

        assert before <= result <= after


class TestToLocal:
    """Tests für to_local()-Funktion."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Leert Settings-Cache vor jedem Test."""
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_to_local_berlin_winter(self, monkeypatch):
        """Konvertiert UTC zu Europe/Berlin im Winter (UTC+1)."""
        monkeypatch.setenv("TZ", "Europe/Berlin")

        # 2026-01-15 12:00:00 UTC
        utc_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        local_dt = to_local(utc_dt)

        assert local_dt.tzinfo == ZoneInfo("Europe/Berlin")
        # Winterzeit UTC+1
        assert local_dt.hour == 13
        assert local_dt.day == 15
        assert local_dt.month == 1

    def test_to_local_berlin_summer(self, monkeypatch):
        """Konvertiert UTC zu Europe/Berlin im Sommer (UTC+2)."""
        monkeypatch.setenv("TZ", "Europe/Berlin")

        # 2026-07-15 12:00:00 UTC
        utc_dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        local_dt = to_local(utc_dt)

        assert local_dt.tzinfo == ZoneInfo("Europe/Berlin")
        # Sommerzeit UTC+2
        assert local_dt.hour == 14
        assert local_dt.day == 15
        assert local_dt.month == 7

    def test_to_local_naive_datetime(self, monkeypatch):
        """Interpretiert naive datetime als UTC."""
        monkeypatch.setenv("TZ", "Europe/Berlin")

        # Naive datetime (kein tzinfo)
        naive_dt = datetime(2026, 1, 15, 12, 0, 0)
        local_dt = to_local(naive_dt)

        assert local_dt.tzinfo == ZoneInfo("Europe/Berlin")
        # Winterzeit UTC+1
        assert local_dt.hour == 13

    def test_to_local_utc_timezone(self, monkeypatch):
        """Konvertiert zu UTC-Zeitzone."""
        monkeypatch.setenv("TZ", "UTC")

        utc_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        local_dt = to_local(utc_dt)

        assert local_dt.tzinfo == ZoneInfo("UTC")
        # UTC bleibt UTC
        assert local_dt.hour == 12
        assert local_dt.day == 15

    def test_to_local_different_timezone(self, monkeypatch):
        """Konvertiert zu beliebiger anderer Zeitzone."""
        monkeypatch.setenv("TZ", "America/New_York")

        # 2026-01-15 12:00:00 UTC
        utc_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        local_dt = to_local(utc_dt)

        assert local_dt.tzinfo == ZoneInfo("America/New_York")
        # New York Winter ist UTC-5
        assert local_dt.hour == 7
        assert local_dt.day == 15


class TestIntegration:
    """Integrationstests für Zeitzonen-Funktionen."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Leert Settings-Cache vor jedem Test."""
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_full_workflow_berlin(self, monkeypatch):
        """Testet vollständigen Workflow: UTC -> lokal (Berlin)."""
        monkeypatch.setenv("TZ", "Europe/Berlin")

        tz = get_tz()
        now_utc = utc_now()
        now_local = to_local(now_utc)

        assert now_local.tzinfo == tz
        assert isinstance(now_utc, datetime)
        assert isinstance(now_local, datetime)
        # Differenz zwischen UTC und Berlin sollte 1 oder 2 Stunden sein
        offset = (now_local.hour - now_utc.hour) % 24
        assert offset in (1, 2)

    def test_roundtrip_conversion(self, monkeypatch):
        """Testet Roundtrip: UTC -> lokal -> UTC."""
        monkeypatch.setenv("TZ", "Europe/Berlin")

        original = datetime(2026, 7, 15, 10, 30, 0, tzinfo=timezone.utc)
        local = to_local(original)
        # Konvertiere zurück zu UTC
        back_to_utc = local.astimezone(timezone.utc)

        assert original == back_to_utc
