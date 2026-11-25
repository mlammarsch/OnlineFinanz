"""Register- und Nicht-Ziele-Tests für das ORM-Metadata (T-027).

Referenziert N-DB-01 (kein rohes SQL für Daten), N-DB-02 (Beträge als ganze
Cent), F-CON-01/F-CON-02 (Versionsspalte für optimistisches Sperren) und die
Nicht-Ziele aus ``AGENTS.md`` (keine Kategorien, Schlagworte, Budgets,
Überweisungen).

Das Modul prüft das Modell-Register generisch über ``Base.metadata`` statt über
eine je Prüfung hartcodierte Tabellenliste:

* vollständige Tabellenliste des gemeinsamen Metadata (Sammelimport in
  ``finue.models``),
* verbotene Namensbestandteile in Tabellen- und Spaltennamen,
* Integer-Typ für Betrags- und Größen-Spalten,
* Versionsspalte für jede veränderbare Tabelle.

Die Versionspflicht ist generisch formuliert: Geprüft werden ALLE Tabellen im
Metadata; nur die in ``UNVERSIONED_TABLES`` ausdrücklich dokumentierten Tabellen
sind ausgenommen. Eine künftig hinzugefügte Tabelle ohne Versionsspalte lässt
``test_unversioned_tables_are_exactly_documented`` scheitern, solange sie nicht
bewusst in ``UNVERSIONED_TABLES`` aufgenommen wird.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Final

from sqlalchemy import Integer

import finue.models  # noqa: F401  (registriert alle Tabellen im gemeinsamen Metadata)
from finue.db.base import Base

# Erwartete Tabellen aus allen Modell-Tasks T-017 bis T-026. Die Gleichheit in
# test_all_expected_tables_registered erzwingt, dass eine neue Tabelle hier
# bewusst eingetragen wird.
EXPECTED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "account",
        "account_group",
        "audit_log",
        "backup_job",
        "backup_run",
        "bank",
        "bank_access",
        "duplicate_decision",
        "import_run",
        "import_run_error",
        "import_template",
        "mcp_token",
        "standing_order",
        "transaction",
        "user_session",
    }
)

# Nicht-Ziele aus AGENTS.md: diese Bestandteile dürfen in keinem Tabellen- oder
# Spaltennamen vorkommen (case-insensitive Teilstring-Suche).
FORBIDDEN_NAME_PARTS: Final[tuple[str, ...]] = (
    "category",
    "kategorie",
    "tag",
    "budget",
    "transfer",
    "ueberweisung",
)

# Tabellen ohne Versionsspalte (F-CON-01/F-CON-02). Zwei Gruppen:
# * append-only Protokoll-/Lauf-Tabellen ohne optimistisches Sperren
#   (AuditLog, BackupRun, ImportRunError, siehe Modul-Docstrings),
# * bewusst unversionierte Tabellen: Bank ist Stammdaten-Referenz ohne
#   Nutzer-Bearbeitungspfad (T-017: nur BankAccess hat VersionedMixin);
#   UserSession/McpToken nutzen Setz-Semantik bzw. Heartbeat und sind in
#   F-CON-02 nicht als versionierte Entitäten genannt (T-024, security.py).
UNVERSIONED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "audit_log",
        "backup_run",
        "import_run_error",
        "bank",
        "user_session",
        "mcp_token",
    }
)

# Betrags- und Größen-Spalten folgen der Namenskonvention aus N-DB-02
# ("_cents" für Geldbeträge in ganzen Cent) bzw. benennen eine Dateigröße
# ("_bytes"). Diese Spalten müssen ganzzahlig sein, nie Gleitkomma.
AMOUNT_COLUMN_SUFFIXES: Final[tuple[str, ...]] = ("_cents", "_bytes")


def test_all_expected_tables_registered() -> None:
    """Alle erwarteten Tabellen liegen in genau einem Metadata (N-DB-01)."""
    assert set(Base.metadata.tables) == set(EXPECTED_TABLES)


def test_no_forbidden_table_or_column_names() -> None:
    """Keine Kategorien, Tags, Budgets oder Überweisungen (Nicht-Ziele)."""
    found: list[str] = []
    for table in Base.metadata.tables.values():
        names = [table.name, *(column.name for column in table.columns)]
        for name in names:
            lowered = name.lower()
            for part in FORBIDDEN_NAME_PARTS:
                if part in lowered:
                    found.append(f"{name!r} enthält {part!r}")
    assert found == [], f"Verbotene Namensbestandteile gefunden: {found}"


def test_amount_columns_are_integer() -> None:
    """Betrags-/Größenspalten sind Integer, keine Gleitkommazahlen (N-DB-02)."""
    non_integer: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name.endswith(AMOUNT_COLUMN_SUFFIXES) and not isinstance(column.type, Integer):
                non_integer.append(f"{table.name}.{column.name}: {column.type!r}")
    assert non_integer == [], f"Betragsspalten ohne Integer-Typ: {non_integer}"


def test_every_mutable_table_has_version_column() -> None:
    """Jede veränderbare Tabelle hat eine version-Spalte (F-CON-01/F-CON-02).

    Ausgenommen sind ausschließlich die in ``UNVERSIONED_TABLES`` dokumentierten
    Tabellen. Die Prüfung läuft generisch über alle registrierten Tabellen.
    """
    missing = [
        name
        for name, table in Base.metadata.tables.items()
        if "version" not in table.columns and name not in UNVERSIONED_TABLES
    ]
    assert missing == [], f"Tabellen ohne version-Spalte: {missing}"


def test_unversioned_tables_are_exactly_documented() -> None:
    """Die Menge der Tabellen ohne version entspricht der Dokumentation.

    Dieser Wächter stellt sicher, dass eine neu hinzugefügte Tabelle ohne
    Versionsspalte nicht stillschweigend durchrutscht: sie verändert die Menge
    und lässt den Test scheitern, bis sie bewusst bewertet wurde.
    """
    without_version = {name for name, table in Base.metadata.tables.items() if "version" not in table.columns}
    assert without_version == set(UNVERSIONED_TABLES)


def test_package_import_configures_mappers_without_circular_import() -> None:
    """Der Sammelimport allein konfiguriert alle Mapper (kein Zirkelimport).

    Läuft isoliert in einem Subprozess, damit nur ``import finue.models`` und
    ``configure_mappers()`` bewertet werden.
    """
    code = (
        "from sqlalchemy.orm import configure_mappers\n"
        "import finue.models\n"
        "configure_mappers()\n"
        "print(len(finue.models.Base.metadata.tables))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, f"Mapper-Konfiguration fehlgeschlagen: {result.stderr}"
    assert int(result.stdout.strip()) == len(EXPECTED_TABLES)
