"""Tests für Konfigurationsverwaltung.

Implementiert N-DB-01 (kein rohes SQL), N-OPS-05 (Konfigurationsstandards)
und N-OPS-06 (sichere Pfadverwaltung). Testet Standardwerte und Env-Override.
"""

from pathlib import Path

import pytest

from finue.config import get_settings


@pytest.fixture
def clear_settings_cache():
    """Fixture zum Leeren des Settings-Cache vor und nach jedem Test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestSettingsDefaults:
    """Tests für Standardwerte der Konfiguration."""

    def test_default_data_dir(self, clear_settings_cache, monkeypatch):
        """Teste Default-Wert für data_dir.

        Implementiert N-OPS-05, N-OPS-06.
        """
        monkeypatch.delenv("DATA_DIR", raising=False)
        settings = get_settings()
        assert settings.data_dir == Path("./data")

    def test_default_log_level(self, clear_settings_cache, monkeypatch):
        """Teste Default-Wert für log_level.

        Implementiert N-OPS-05.
        """
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        settings = get_settings()
        assert settings.log_level == "INFO"

    def test_default_tz(self, clear_settings_cache, monkeypatch):
        """Teste Default-Wert für tz.

        Implementiert N-OPS-05.
        """
        monkeypatch.delenv("TZ", raising=False)
        settings = get_settings()
        assert settings.tz == "Europe/Berlin"

    def test_default_key_file(self, clear_settings_cache, monkeypatch):
        """Teste Default-Wert für key_file.

        Implementiert N-OPS-06. Nur Pfad, keine Secrets.
        """
        monkeypatch.delenv("KEY_FILE", raising=False)
        settings = get_settings()
        assert settings.key_file == Path("./key")

    def test_default_database_url(self, clear_settings_cache, monkeypatch):
        """Teste Standard-DATABASE_URL-Berechnung von data_dir.

        Implementiert N-DB-01, N-OPS-05. DATABASE_URL wird automatisch aus
        DATA_DIR abgeleitet, falls nicht explizit gesetzt.
        """
        monkeypatch.delenv("DATA_DIR", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings = get_settings()
        expected = f"sqlite:///{settings.data_dir.as_posix()}/finue.db"
        assert settings.database_url == expected


class TestSettingsEnvOverride:
    """Tests für Umgebungsvariablen-Override."""

    def test_override_data_dir(self, clear_settings_cache, monkeypatch):
        """Teste Env-Override für data_dir.

        Implementiert N-OPS-05, N-OPS-06.
        """
        monkeypatch.setenv("DATA_DIR", "/custom/data")
        settings = get_settings()
        assert settings.data_dir == Path("/custom/data")

    def test_override_log_level(self, clear_settings_cache, monkeypatch):
        """Teste Env-Override für log_level.

        Implementiert N-OPS-05.
        """
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        settings = get_settings()
        assert settings.log_level == "DEBUG"

    def test_override_tz(self, clear_settings_cache, monkeypatch):
        """Teste Env-Override für tz.

        Implementiert N-OPS-05.
        """
        monkeypatch.setenv("TZ", "UTC")
        settings = get_settings()
        assert settings.tz == "UTC"

    def test_override_key_file(self, clear_settings_cache, monkeypatch):
        """Teste Env-Override für key_file.

        Implementiert N-OPS-06. Nur Pfad, keine Secrets.
        """
        monkeypatch.setenv("KEY_FILE", "/etc/finue/secret.key")
        settings = get_settings()
        assert settings.key_file == Path("/etc/finue/secret.key")

    def test_override_database_url(self, clear_settings_cache, monkeypatch):
        """Teste Env-Override für DATABASE_URL.

        Implementiert N-DB-01, N-OPS-05. Explizite DATABASE_URL überschreibt
        die Berechnung von data_dir.
        """
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/finue")
        settings = get_settings()
        assert settings.database_url == "postgresql://user:pass@localhost/finue"

    def test_database_url_with_custom_data_dir(self, clear_settings_cache, monkeypatch):
        """Teste DATABASE_URL-Berechnung mit benutzerdefiniertem data_dir.

        Implementiert N-DB-01, N-OPS-05.
        """
        monkeypatch.setenv("DATA_DIR", "/custom/path")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings = get_settings()
        expected = f"sqlite:///{settings.data_dir.as_posix()}/finue.db"
        assert settings.database_url == expected


class TestSettingsCaching:
    """Tests für get_settings()-Caching."""

    def test_caching_returns_same_instance(self, clear_settings_cache, monkeypatch):
        """Teste, dass get_settings() die gleiche Instanz zurückgibt.

        Implementiert N-OPS-05.
        """
        monkeypatch.delenv("DATA_DIR", raising=False)
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_cache_clear_creates_new_instance(self, clear_settings_cache, monkeypatch):
        """Teste, dass Cache-Clear eine neue Instanz erzeugt.

        Implementiert N-OPS-05.
        """
        monkeypatch.delenv("DATA_DIR", raising=False)
        settings1 = get_settings()
        get_settings.cache_clear()
        settings2 = get_settings()
        assert settings1 is not settings2
        assert settings1.data_dir == settings2.data_dir
