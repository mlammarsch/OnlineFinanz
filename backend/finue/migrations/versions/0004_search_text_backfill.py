"""Vierte Migration: Suchspalte ``transaction.search_text`` befuellen (T-087).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26 19:45:00.000000

Bis hierher setzte nur ``demo.py`` die Spalte ``search_text``; Import und Sync
liessen sie leer. Diese Migration befuellt daher alle Bestandszeilen einmalig
nach F-SUC-01 und F-SUC-06. Es gibt keine Schemaaenderung; nur Daten.

Die Normalisierung ist bewusst EINGEFROREN und hier kopiert, nicht aus
:mod:`finue.services.search_text` importiert: Eine spaetere Aenderung an der
Laufzeitregel darf den historischen Backfill-Stand nicht rueckwirkend
verschieben. ``tests/unit/test_search_text.py`` belegt die Paritaet zwischen
dieser eingefrorenen Kopie und der Laufzeitfunktion; laeuft eine der beiden
auseinander, schlaegt der Test rot.

N-DB-01: Es gibt kein rohes SQL. Die Tabelle wird ueber SQLAlchemy
reflektiert, gelesen und mit ``sqlalchemy.update`` geschrieben; die Bausteine
werden in Python normalisiert (NFC, ``casefold``) und batchweise aktualisiert.

N-DB-03: Migrationen laufen nur vorwaerts. ``downgrade()`` enthaelt daher
bewusst keine Logik und bricht mit ``NotImplementedError`` ab.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Sequence, Union

import sqlalchemy as sa
from alembic import op

# Revision-Kennungen fuer Alembic (nicht von Hand aendern).
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Batch-Groesse des Backfills: begrenzt Speicher und Transaktionsdauer.
_BATCH_SIZE: Final[int] = 500

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalize(value: str | None) -> str:
    """Eingefrorene Normalisierung eines Suchbausteins (F-SUC-01, F-SUC-06).

    Regel wie :func:`finue.services.search_text.normalize_search_text`: NFC,
    ``casefold``, Zeilenumbrueche/Mehrfach-Leerzeichen zu einem Leerzeichen,
    trimmen.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFC", value)
    text = text.casefold()
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _build_search_text(
    counterparty: str | None,
    purpose: str | None,
    note: str | None,
    counterparty_iban: str | None,
    external_ref: str | None,
) -> str:
    """Eingefrorene Kopie von :func:`finue.services.search_text.build_search_text`.

    Reihenfolge der Bausteine: Gegenpartei, Verwendungszweck, Notiz, IBAN,
    externe Referenz; danach die IBAN zusaetzlich ohne Leerzeichen, falls sie
    sich davon unterscheidet.
    """
    parts: list[str] = []
    for value in (counterparty, purpose, note, counterparty_iban, external_ref):
        normalized = _normalize(value)
        if normalized:
            parts.append(normalized)

    normalized_iban = _normalize(counterparty_iban)
    compact_iban = normalized_iban.replace(" ", "")
    if compact_iban and compact_iban != normalized_iban:
        parts.append(compact_iban)

    return " ".join(parts)


def upgrade() -> None:
    """Befuellt ``transaction.search_text`` fuer alle Bestandszeilen (F-SUC-01)."""
    connection = op.get_bind()
    metadata = sa.MetaData()
    transaction = sa.Table("transaction", metadata, autoload_with=connection)

    last_id = 0
    while True:
        rows = connection.execute(
            sa.select(
                transaction.c.id,
                transaction.c.counterparty,
                transaction.c.purpose,
                transaction.c.note,
                transaction.c.counterparty_iban,
                transaction.c.external_ref,
                transaction.c.search_text,
            )
            .where(transaction.c.id > last_id)
            .order_by(transaction.c.id)
            .limit(_BATCH_SIZE)
        ).all()
        if not rows:
            break

        for row in rows:
            text = _build_search_text(
                row.counterparty,
                row.purpose,
                row.note,
                row.counterparty_iban,
                row.external_ref,
            )
            if text != row.search_text:
                connection.execute(sa.update(transaction).where(transaction.c.id == row.id).values(search_text=text))

        last_id = rows[-1].id


def downgrade() -> None:
    """Rueckbau ist nicht unterstuetzt (N-DB-03: Migrationen nur vorwaerts).

    Bewusst ohne Logik: Ein ungetesteter Rueckbau koennte Datenverlust
    verursachen. Fuer einen Stand vor dieser Migration wird stattdessen ein
    Backup eingespielt und anschliessend vorwaerts migriert (N-DB-03, F-BAK-07).
    """
    raise NotImplementedError("Migrationen laufen nur vorwaerts (N-DB-03)")
