"""Programmatischer Alembic-Migrations-Runner.

Stellt Funktionen bereit, um das Schema einer Datenbank auf den aktuellen Stand
zu heben und die Revisionen zu vergleichen. Implementiert N-DB-03
(Schema-Versionierung ueber Alembic-Migrationen) und N-OPS-04 (Start erkennt den
Schemastand und hebt ihn bei Bedarf an).

Bewusst ohne Subprozess: alle Aufrufe laufen ueber die Python-API von Alembic
(``alembic.config``, ``alembic.command``, ``alembic.script``,
``alembic.runtime.migration``), nicht ueber die Alembic-CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

# Dieselbe Konfigurationsdatei wie fuer den CLI-Betrieb (T-028). Der Pfad wird
# aus der Modullage abgeleitet, damit keine absolute Pfadangabe noetig ist.
ALEMBIC_INI: Final[Path] = Path(__file__).resolve().parents[3] / "alembic.ini"


def _build_config(url: str | None = None) -> Config:
    """Laedt ``alembic.ini`` und setzt optional die Datenbank-URL.

    Ohne ``url`` gilt die in ``env.py`` aufgeloeste Projekt-URL. Mit ``url``
    bekommt die Config-Option ``sqlalchemy.url`` Vorrang (siehe ``env.py``),
    sodass die Funktionen gezielt gegen eine beliebige Datenbank laufen, etwa
    eine Test-Datenbank.
    """
    if not ALEMBIC_INI.is_file():
        raise FileNotFoundError(f"alembic.ini fehlt: {ALEMBIC_INI}")
    config = Config(str(ALEMBIC_INI))
    # Programmatische Aufrufe duerfen das App-Logging nicht ersetzen: ``env.py``
    # wertet dieses Attribut aus und ruft dann kein ``fileConfig`` auf. Sonst
    # wuerde Alembics Standard-Handler (stderr, ohne Maskierung) die
    # Projekt-Konfiguration ueberschreiben (N-SEC-02). Der CLI-Betrieb setzt das
    # Attribut nicht und konfiguriert Logging wie bisher.
    config.attributes["configure_logger"] = False
    if url is not None:
        config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade_to_head(url: str) -> None:
    """Hebt das Schema der Datenbank unter ``url`` auf ``head`` an.

    Nutzt die Alembic-Programm-API (``alembic.command.upgrade``) mit einer
    Config aus ``alembic.ini``; kein CLI-Subprozess. Die Migration selbst liest
    die URL aus der gesetzten Config-Option (N-DB-03, N-OPS-04).
    """
    command.upgrade(_build_config(url), "head")


def current_revision(engine: Engine) -> str | None:
    """Liefert die aktuelle Schema-Revision der Datenbank oder ``None``.

    Liest die Revision ueber ``alembic.runtime.migration.MigrationContext``
    direkt aus der Datenbank. Eine leere Datenbank ohne
    ``alembic_version``-Tabelle ergibt ``None``.
    """
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()


def head_revision() -> str | None:
    """Liefert die Ziel-Revision aus den vorhandenen Migrationsskripten.

    Die Revision kommt aus ``alembic.script.ScriptDirectory`` ueber
    ``alembic.ini``; bei einer leeren Migrationshistorie ist das Ergebnis
    ``None``.
    """
    script = ScriptDirectory.from_config(_build_config())
    return script.get_current_head()


def is_schema_current(engine: Engine) -> bool:
    """Prueft, ob die Datenbank bereits auf ``head`` steht (N-OPS-04).

    Vergleicht ``current_revision`` mit ``head_revision``. Eine frische, leere
    Datenbank ist nicht aktuell; nach ``upgrade_to_head`` ist sie es.
    """
    return current_revision(engine) == head_revision()
