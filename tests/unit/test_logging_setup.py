"""Tests für JSON-Logging-Konfiguration.

Implementiert N-OPS-05 (strukturiertes JSON-Logging auf stdout mit Log-Level
per Umgebungsvariable). Testet gültiges JSON, Feldstruktur und Log-Level-Effektivität.
"""

import json
import logging

from finue.logging_setup import configure_logging


class TestConfigureLogging:
    """Tests für configure_logging()-Funktion.

    Implementiert N-OPS-05: JSON-Logging, Log-Level, capsys-Integration.
    """

    def test_configure_logging_outputs_json_to_stdout(self, capsys):
        """Teste, dass configure_logging() gültiges JSON auf stdout schreibt.

        Implementiert N-OPS-05. Initialisiert Logging, schreibt Test-Nachricht,
        verifiziert gültiges JSON mit erforderlichen Feldern ts, level, logger, message.
        """
        configure_logging("INFO")
        logger = logging.getLogger("test_logger")
        logger.info("Test message")

        captured = capsys.readouterr()
        assert captured.out, "Logging-Ausgabe sollte nicht leer sein"

        # Parse JSON
        line = captured.out.strip()
        log_obj = json.loads(line)

        # Verifiziere erforderliche Felder
        assert "ts" in log_obj
        assert "level" in log_obj
        assert "logger" in log_obj
        assert "message" in log_obj

        assert log_obj["level"] == "INFO"
        assert log_obj["logger"] == "test_logger"
        assert log_obj["message"] == "Test message"

    def test_configure_logging_respects_log_level(self, capsys):
        """Teste, dass Log-Level-Filter wirksam ist.

        Implementiert N-OPS-05. Konfiguriere mit WARNING, schreibe DEBUG und INFO
        (sollten ignoriert werden), schreibe WARNING (sollte erscheinen).
        """
        configure_logging("WARNING")
        logger = logging.getLogger("test_level_logger")

        # Diese sollten nicht erscheinen
        logger.debug("Debug message")
        logger.info("Info message")

        # Dieser sollte erscheinen
        logger.warning("Warning message")

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line]

        assert len(lines) == 1, f"Erwartet 1 Zeile, erhalten {len(lines)}"
        log_obj = json.loads(lines[0])
        assert log_obj["level"] == "WARNING"
        assert log_obj["message"] == "Warning message"

    def test_configure_logging_includes_extra_fields(self, capsys):
        """Teste, dass extra-Felder in JSON enthalten sind.

        Implementiert N-OPS-05. Schreibe mit extra={...}, verifiziere
        dass diese Felder im "extra"-Feld des JSON erscheinen.
        """
        configure_logging("INFO")
        logger = logging.getLogger("test_extra_logger")

        logger.info("Message with extra", extra={"user_id": 123, "request_id": "abc"})

        captured = capsys.readouterr()
        line = captured.out.strip()
        log_obj = json.loads(line)

        assert "extra" in log_obj
        assert log_obj["extra"]["user_id"] == 123
        assert log_obj["extra"]["request_id"] == "abc"

    def test_configure_logging_no_duplicate_handlers(self, capsys):
        """Teste, dass mehrfaches Aufrufen keine doppelten Handler anlegt.

        Implementiert N-OPS-05. Rufe configure_logging() zweimal auf,
        schreibe Nachricht, verifiziere dass sie nur einmal erscheint.
        """
        configure_logging("INFO")
        configure_logging("INFO")

        logger = logging.getLogger("test_dup_logger")
        logger.info("Single message")

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line]

        assert len(lines) == 1, f"Erwartet 1 Zeile (kein Duplikat), erhalten {len(lines)}"

    def test_configure_logging_timestamp_iso_utc(self, capsys):
        """Teste, dass Timestamp im ISO 8601 UTC-Format ist.

        Implementiert N-OPS-05. Verifiziere, dass ts im Format
        YYYY-MM-DDTHH:MM:SS.ffffff+00:00 ist.
        """
        configure_logging("INFO")
        logger = logging.getLogger("test_ts_logger")
        logger.info("Timestamp test")

        captured = capsys.readouterr()
        line = captured.out.strip()
        log_obj = json.loads(line)

        ts = log_obj["ts"]
        # ISO 8601 UTC Timestamp hat Format: 2026-11-25T12:34:56.123456+00:00
        assert "T" in ts, "Timestamp sollte ISO 8601 Format sein"
        assert "+00:00" in ts or "Z" in ts, "Timestamp sollte UTC sein"

    def test_configure_logging_debug_level(self, capsys):
        """Teste DEBUG-Level.

        Implementiert N-OPS-05. Konfiguriere mit DEBUG, schreibe DEBUG-Nachricht,
        verifiziere dass sie erscheint.
        """
        configure_logging("DEBUG")
        logger = logging.getLogger("test_debug_logger")
        logger.debug("Debug message")

        captured = capsys.readouterr()
        line = captured.out.strip()
        log_obj = json.loads(line)

        assert log_obj["level"] == "DEBUG"
        assert log_obj["message"] == "Debug message"

    def test_configure_logging_error_level(self, capsys):
        """Teste ERROR-Level.

        Implementiert N-OPS-05. Konfiguriere mit ERROR, schreibe ERROR-Nachricht,
        verifiziere dass sie erscheint mit korrektem Level.
        """
        configure_logging("ERROR")
        logger = logging.getLogger("test_error_logger")
        logger.error("Error message")

        captured = capsys.readouterr()
        line = captured.out.strip()
        log_obj = json.loads(line)

        assert log_obj["level"] == "ERROR"
        assert log_obj["message"] == "Error message"
