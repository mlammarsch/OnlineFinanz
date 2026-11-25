"""Datenbank-Basis für SQLAlchemy ORM.

Implementiert N-DB-01 (kein rohes SQL für Daten) durch DeclarativeBase und
Naming-Convention für Alembic-Migrationen. Alle Tabellen-Klassen erben von Base.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Naming-Convention für Alembic-Migrationen
_naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Basis-Klasse für alle ORM-Modelle mit Naming-Convention für Migrationen.

    Die Naming-Convention sorgt dafür, dass Alembic konsistente Namen für
    Indizes, Constraints und Foreign Keys generiert.
    """

    metadata = MetaData(naming_convention=_naming_convention)
