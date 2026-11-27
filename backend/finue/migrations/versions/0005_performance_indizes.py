"""Fuenfte Migration: abdeckende Performance-Indizes auf ``transaction`` (T-103).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-27 14:40:00.000000

Enthaelt zwei abdeckende Indizes fuer Suche und Buchungsliste, empirisch
begruendet im Performance-Block T-103 (N-PERF-01, F-BUC-06). Die
``EXPLAIN QUERY PLAN``-Auswertung zeigt, dass die Suche mit fuehrendem Wildcard
(``LIKE '%begriff%'``) den Index nicht suchend nutzen kann, dass ein abdeckender
Index aber den Zeilen-Nachschlag erspart (D5/F-SUC-06: LIKE auf der
normalisierten Suchspalte plus Indizes):

* ``ix_transaction_account_booking_search`` auf ``(account_id, booking_date,
  merged, search_text, amount_cents)``: deckt die Trefferzahl- und
  Treffersummen-Abfrage der Suche ab (F-SUC-01/02). Gemessen sank die Suche im
  Standardzeitraum (365 Tage, alle Konten) von rund 480 ms auf rund 200 ms.
* ``ix_transaction_account_status_booking_cover`` auf ``(account_id, status,
  merged, booking_date, id, amount_cents)``: deckt die Saldo-/Tagessummen-
  Aggregate der Buchungsliste und der Gruppenansicht ab (F-BUC-02/03/05).
  Gemessen sank die Gruppenansicht (100.000 Buchungen, 5 Konten) von rund 800 ms
  auf rund 70 ms.

N-DB-01: ausschliesslich Alembic-Operationen, kein rohes SQL fuer Daten (die
``create_index``-Aufrufe erzeugen DDL, keine Datenabfragen). N-DB-03:
Migrationen laufen nur vorwaerts; ``downgrade()`` bricht mit
``NotImplementedError`` ab.
"""

from typing import Sequence, Union

from alembic import op

# Revision-Kennungen fuer Alembic (nicht von Hand aendern).
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Legt die beiden abdeckenden Performance-Indizes an (T-103)."""
    op.create_index(
        "ix_transaction_account_booking_search",
        "transaction",
        ["account_id", "booking_date", "merged", "search_text", "amount_cents"],
        unique=False,
    )
    op.create_index(
        "ix_transaction_account_status_booking_cover",
        "transaction",
        ["account_id", "status", "merged", "booking_date", "id", "amount_cents"],
        unique=False,
    )


def downgrade() -> None:
    """Rueckbau ist nicht unterstuetzt (N-DB-03: Migrationen nur vorwaerts).

    Bewusst ohne Logik: Ein ungetesteter Rueckbau koennte Datenverlust
    verursachen. Fuer einen Stand vor dieser Migration wird stattdessen ein
    Backup eingespielt und anschliessend vorwaerts migriert (N-DB-03, F-BAK-07).
    """
    raise NotImplementedError("Migrationen laufen nur vorwaerts (N-DB-03)")
