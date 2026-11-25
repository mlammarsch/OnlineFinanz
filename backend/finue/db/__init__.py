"""Datenbank-Modul für Finanzübersicht Backend.

Stellt DeclarativeBase, Engine-Fabrik und Session-Factory zur Verfügung.
Implementiert N-DB-01 (kein rohes SQL für Daten) und N-DB-04 (SQLite WAL).
"""

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory

__all__ = [
    "Base",
    "create_db_engine",
    "make_session_factory",
]
