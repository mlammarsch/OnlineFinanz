"""Alembic-Umgebung fuer Finanzuebersicht.

Implementiert N-DB-01 (kein rohes SQL ausserhalb der Engine-Konfiguration) und
N-DB-03 (Versionierung des Schemas ueber Migrationen).

Die Datenbank-URL kommt aus den Projekt-Settings (``finue.config``) oder, falls
in ``alembic.ini`` gesetzt, aus der Alembic-Config-Option ``sqlalchemy.url``.
Damit gibt es nur eine Quelle fuer die URL und keine Zugangsdaten in der
Konfigurationsdatei.

``render_as_batch=True`` ist fuer SQLite zwingend: SQLite kann Constraints nicht
per ``ALTER TABLE`` nachziehen, Alembic erstellt die Tabelle dann batch-weise neu.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import MetaData
from sqlalchemy.engine import Connection

import finue.models  # noqa: F401  Sammelimport: registriert ALLE Tabellen
from finue.config import get_settings
from finue.db.base import Base
from finue.db.engine import create_db_engine
from finue.db.mixins import UtcDateTime

# Alembic-Config-Objekt (Werte aus alembic.ini).
config = context.config

# Logging nur konfigurieren, wenn eine Konfigurationsdatei vorliegt und der
# Aufrufer es erlaubt. Der programmatische Runner (``finue.db.migrate``) setzt
# ``configure_logger`` auf False: Alembics ``fileConfig`` wuerde sonst die
# Root-Handler durch den Standard-Handler (stderr, ohne Maskierung) ersetzen und
# den Projekt-LogFilter aushebeln (N-SEC-02). Der CLI-Betrieb setzt das Attribut
# nicht und konfiguriert Logging wie bisher.
if config.config_file_name is not None and bool(config.attributes.get("configure_logger", True)):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# target_metadata erst nach dem Sammelimport lesen, sonst fehlen Tabellen.
target_metadata: MetaData = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Rendert den Projekttyp ``UtcDateTime`` mit passendem Import (N-DB-03).

    Alembic schreibt bei Autogenerate zwar den voll qualifizierten Typnamen,
    aber nicht den zugehoerigen Import. Der Hook ergaenzt
    ``from finue.db.mixins import UtcDateTime`` und rendert den Typ kurz, damit
    neue Migrationsdateien ohne Handnacharbeit laufen. Fuer alle anderen
    Objekte liefert der Hook ``False`` und Alembic nutzt seine Standardausgabe.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("from finue.db.mixins import UtcDateTime")
        return "UtcDateTime(timezone=True)"
    return False


def get_database_url() -> str:
    """Liefert die Datenbank-URL aus Alembic-Config oder Projekt-Settings.

    Die Config-Option hat Vorrang (z. B. fuer einmalige Laeufe gegen eine andere
    Datenbank); sonst gilt ``Settings.database_url`` inklusive der Ableitung aus
    ``DATA_DIR``. Die URL wird nie hier hart kodiert.
    """
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return str(configured_url)
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Fuehrt Migrationen im Offline-Modus aus (SQL-Skript statt Verbindung)."""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Fuehrt Migrationen im Online-Modus gegen die konfigurierte Datenbank aus.

    Die Engine kommt aus ``finue.db.engine.create_db_engine``, damit SQLite-PRAGMAs
    (WAL, busy_timeout, foreign_keys; N-DB-04) auch bei Migrationen gelten.
    """
    engine = create_db_engine(get_database_url())

    with engine.connect() as connection:
        _run_migrations_with_connection(connection)


def _run_migrations_with_connection(connection: Connection) -> None:
    """Konfiguriert den Alembic-Kontext fuer eine offene Verbindung."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
