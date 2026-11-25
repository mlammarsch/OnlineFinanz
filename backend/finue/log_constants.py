"""Gemeinsame Konstanten für Logging-Konfiguration.

Definiert Standard-Felder für LogRecords, die nicht als extra-Felder
behandelt werden sollen.
"""

# Standard-Felder in LogRecord, die nicht als extra-Felder auftauchen sollen
STANDARD_FIELDS = {
    "name",
    "msg",
    "args",
    "created",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "thread",
    "threadName",
    "exc_info",
    "exc_text",
    "stack_info",
    "asctime",
    "taskName",  # Zusätzlich: Task-Name soll nicht als extra auftauchen
}
