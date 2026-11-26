"""Dritte Migration: Herkunft der Kennzeichen, Bank-Icon/Version, Exportprofile (T-085).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 19:20:00.000000

Enthaelt:

* ``transaction.checked_origin`` und ``transaction.reconciled_origin`` (nullable,
  Wertebereich ``ui``/``mcp``/``import`` wie ``FlagOrigin`` in
  ``services/export_schema.py``) samt CHECK-Bedingungen fuer F-ABS-01 "Herkunft
  der letzten Aenderung",
* ``bank.version`` (F-CON-02), ``bank.icon`` und ``bank.icon_mime`` (F-KTO-10),
* neue Tabelle ``export_profile`` mit eindeutigem Namen, Standardkennzeichen,
  JSON-Optionen, JSON-Spaltenauswahl, Versionsspalte und Zeitstempeln
  (F-EXP-03),
* Indizes fuer Liste/Suche auf ``transaction``: ``(account_id, status,
  booking_date, id)``, ``(booking_date, id)`` und ``amount_cents``.

N-DB-01: ausschliesslich Alembic-Operationen, kein rohes SQL fuer Daten. Die
``PRAGMA foreign_keys``-Aufrufe sind Verbindungs-Konfiguration, kein Daten-SQL
(analog ``db/engine.py``): SQLite kann CHECK-Bedingungen nicht per
``ALTER TABLE`` nachziehen, der Batch-Modus baut die Tabelle daher neu auf. Mit
eingeschalteter Fremdschluesselpruefung wuerde das implizite ``DROP TABLE
transaction`` die Kindzeilen in ``duplicate_decision`` (ON DELETE CASCADE)
mitloeschen; die PRAGMA-Aufrufe schalten die Pruefung fuer den Umbau ab und
danach wieder ein. Die Bestandsdaten bleiben erhalten, neue Spalten sind NULL
bzw. tragen ``version = 1``.

N-DB-03: Migrationen laufen nur vorwaerts. ``downgrade()`` enthaelt daher
bewusst keine Logik und bricht mit ``NotImplementedError`` ab.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import finue.db.mixins


# Revision-Kennungen fuer Alembic (nicht von Hand aendern).
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Ergaenzt Herkunftsspalten, Bank-Icon/Version, Exportprofile und Indizes."""
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    if is_sqlite:
        # Fremdschluesselpruefung fuer den Tabellenumbau aussetzen (N-DB-01).
        op.execute("PRAGMA foreign_keys=OFF")

    # F-ABS-01: Herkunft der Kennzeichen; CHECK-Bedingungen wie FlagOrigin.
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.add_column(sa.Column("checked_origin", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("reconciled_origin", sa.String(length=10), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_transaction_ck_transaction_checked_origin"),
            "checked_origin IS NULL OR checked_origin IN ('ui', 'mcp', 'import')",
        )
        batch_op.create_check_constraint(
            op.f("ck_transaction_ck_transaction_reconciled_origin"),
            "reconciled_origin IS NULL OR reconciled_origin IN ('ui', 'mcp', 'import')",
        )
        # Indizes fuer Liste/Suche (F-SUC-01, F-BUC-01); Feintuning in T-103.
        batch_op.create_index(
            "ix_transaction_account_status_booking_id",
            ["account_id", "status", "booking_date", "id"],
            unique=False,
        )
        batch_op.create_index("ix_transaction_booking_id", ["booking_date", "id"], unique=False)
        batch_op.create_index("ix_transaction_amount_cents", ["amount_cents"], unique=False)

    # F-CON-02/F-KTO-10: Bank wird versioniert und erhaelt ein Icon.
    # Bestandszeilen bekommen version = 1 (server_default fuer NOT NULL).
    op.add_column("bank", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.add_column("bank", sa.Column("icon", sa.LargeBinary(), nullable=True))
    op.add_column("bank", sa.Column("icon_mime", sa.String(length=50), nullable=True))

    # F-EXP-03: speicherbare CSV-Exportprofile.
    op.create_table(
        "export_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", finue.db.mixins.UtcDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", finue.db.mixins.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_profile")),
        sa.UniqueConstraint("name", name="uq_export_profile_name"),
    )

    if is_sqlite:
        # Fremdschluesselpruefung wieder einschalten (N-DB-04).
        op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    """Rueckbau ist nicht unterstuetzt (N-DB-03: Migrationen nur vorwaerts).

    Bewusst ohne Logik: Ein ungetesteter Rueckbau koennte Datenverlust
    verursachen. Fuer einen Stand vor dieser Migration wird stattdessen ein
    Backup eingespielt und anschliessend vorwaerts migriert (N-DB-03, F-BAK-07).
    """
    raise NotImplementedError("Migrationen laufen nur vorwaerts (N-DB-03)")
