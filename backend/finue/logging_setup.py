"""JSON-Logging-Konfiguration für Finanzübersicht Backend.

Implementiert N-OPS-05 (strukturiertes JSON-Logging auf stdout mit Log-Level
per Umgebungsvariable) und hängt den Filter für Geheimnisse ein (N-SEC-02).
Handler nutzt sys.stdout zur Aufrufzeit (für pytest capsys).
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from finue.log_constants import STANDARD_FIELDS
from finue.log_filter import UNREPRESENTABLE, LogFilter, mask_text


class JSONFormatter(logging.Formatter):
    """Formatter für JSON-Logging.

    Konvertiert LogRecords zu JSON-Zeilen mit Feldern: ts (ISO 8601 UTC),
    level, logger, message, extra (zusätzliche Felder aus LogRecord.__dict__).

    Hinweis: exc_info wird bewusst nicht ausgegeben, weil Tracebacks sonst
    ebenfalls maskiert werden müssten (N-SEC-02). Lässt sich ein Eintrag nicht
    serialisieren, wird er ohne extra mit Hinweisfeld ausgegeben, nie verworfen.
    """

    @staticmethod
    def _safe_json_default(obj: Any) -> str:
        """Sicheres ``default`` für json.dumps(): str() maskiert, nie eine Exception."""
        try:
            return mask_text(str(obj))
        except Exception:
            return UNREPRESENTABLE

    def format(self, record: logging.LogRecord) -> str:
        """Formatiere LogRecord als JSON-Zeile.

        Args:
            record: LogRecord-Objekt vom logging-System.

        Returns:
            JSON-String mit ts, level, logger, message und ggf. extra.
        """
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        except Exception:
            ts = ""
        try:
            message = record.getMessage()
        except Exception:
            message = UNREPRESENTABLE

        log_dict: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in STANDARD_FIELDS}
        if extra:
            log_dict["extra"] = extra

        try:
            return json.dumps(log_dict, ensure_ascii=False, default=self._safe_json_default)
        except Exception:
            # Eintrag nie verlieren: ohne extra, mit Hinweisfeld
            log_dict.pop("extra", None)
            log_dict["extra_fehler"] = "extra nicht serialisierbar"
            try:
                return json.dumps(log_dict, ensure_ascii=False, default=self._safe_json_default)
            except Exception:
                return json.dumps({"message": UNREPRESENTABLE, "extra_fehler": "Eintrag nicht serialisierbar"})


def configure_logging(level: str = "INFO") -> None:
    """Konfiguriere JSON-Logging auf stdout.

    Implementiert N-OPS-05. Entfernt existierende Handler und fügt
    einen neuen StreamHandler mit LogFilter (N-SEC-02) und JSONFormatter
    ein. Mehrfachaufrufe erzeugen keine doppelten Handler.

    Args:
        level: Log-Level (z.B. "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL").
               Standard: "INFO".
    """
    root_logger = logging.getLogger()

    for existing in root_logger.handlers[:]:
        root_logger.removeHandler(existing)

    log_level = getattr(logging, level.upper(), logging.INFO)
    if not isinstance(log_level, int):
        log_level = logging.INFO
    root_logger.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.addFilter(LogFilter())
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)
