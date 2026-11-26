"""Zweite Migration: Bankfehler-Mapping, PIN-Option und Lauf-Gruppe (T-063).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 16:41:30.637896

Erzeugt mit ``uv run alembic revision --autogenerate -m
"bankfehler pin option lauf gruppe" --rev-id 0002`` gegen eine SQLite-Datenbank
auf Revision 0001. Enthaelt:

* neue Tabelle ``bank_error_mapping`` mit Unique ``(code, blz)`` und Index
  (F-SYN-05, F-SYN-09),
* ``bank_access.store_pin`` mit Standard ``true`` (F-SYN-08),
* ``import_run.job_id`` nullable mit Index als Gruppierung eines Batch-Laufs
  (F-SYN-07).

N-DB-01: ausschliesslich Alembic-Operationen, kein rohes SQL fuer Daten.
``store_pin`` erhaelt einen Dialekt-unabhaengigen ``server_default`` (``true()``),
damit Bestandszeilen beim Upgrade den Wert ``true`` bekommen und die Migration
auch auf PostgreSQL/MySQL laeuft. Die Spalten werden direkt per
``add_column`` ergaenzt, damit SQLite die vorhandenen Tabellen nicht neu
aufbaut und Check-Constraints und Daten unangetastet bleiben.

N-DB-03: Migrationen laufen nur vorwaerts. ``downgrade()`` enthaelt daher
bewusst keine Logik und bricht mit ``NotImplementedError`` ab.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Revision-Kennungen fuer Alembic (nicht von Hand aendern).
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Legt Bankfehler-Mapping, PIN-Option und Lauf-Gruppe an (N-DB-01, N-DB-03)."""
    op.create_table(
        "bank_error_mapping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("blz", sa.String(length=8), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bank_error_mapping")),
        sa.UniqueConstraint("code", "blz", name="uq_bank_error_mapping_code_blz"),
    )
    op.create_index("ix_bank_error_mapping_code", "bank_error_mapping", ["code"], unique=False)

    # F-SYN-08: Bestandszeilen erhalten true; true() rendert je Dialekt passend.
    op.add_column(
        "bank_access",
        sa.Column("store_pin", sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    # F-SYN-07: Gruppierung mehrerer Laeufe eines Batch-Anstosses.
    op.add_column("import_run", sa.Column("job_id", sa.String(length=36), nullable=True))
    op.create_index("ix_import_run_job_id", "import_run", ["job_id"], unique=False)


def downgrade() -> None:
    """Rueckbau ist nicht unterstuetzt (N-DB-03: Migrationen nur vorwaerts).

    Bewusst ohne Logik: Ein ungetesteter Rueckbau koennte Datenverlust
    verursachen. Fuer einen Stand vor dieser Migration wird stattdessen ein
    Backup eingespielt und anschliessend vorwaerts migriert (N-DB-03, F-BAK-07).
    """
    raise NotImplementedError("Migrationen laufen nur vorwaerts (N-DB-03)")
