"""Modelle für Dubletten-Entscheidungen.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-DUP-03 (Verdachtsdubletten),
F-DUP-05 (Auflösung durch Nutzer: Zusammenführen oder Ignorieren) und F-CON-01
(Versionsspalte für optimistische Sperre).

Duplicate decisions werden nie gelöscht; Zusammenführen ist nur eine Markierung
in der Transaction.merged-Spalte (siehe F-DUP-05). SQL behandelt NULL in Unique
als verschieden, daher wird die Eindeutigkeit für Einzelentscheidungen ohne Partner
von der Service-Schicht sichergestellt.

Konventionen und Semantik:
- transaction ist die bleibende Buchung, other_transaction die als zusammengeführt
  markierte.
- Paar-Eindeutigkeit ist richtungsabhängig: (A,B) und (B,A) sind verschieden.
  Die Service-Schicht muss das Paar normieren oder in beide Richtungen prüfen.
- Rückgängig-Semantik und Vorwerte (Notiz, Flags, externe Referenz) liegen im
  Änderungsprotokoll. Die Zeile wird bei erneutem Zusammenführen aktualisiert
  statt neu angelegt.
- Der Wert 'behalten' (beide Buchungen behalten) ist nicht als Standard nach
  Import definiert, sondern als ausdrückliche Nutzerentscheidung zu verstehen.
"""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, UtcDateTime, VersionedMixin
from finue.timeutil import utc_now

if TYPE_CHECKING:
    from finue.models.transaction import Transaction


class DuplicateDecision(Base, VersionedMixin, TimestampMixin):
    """Entscheidung über eine Verdachtsdublette zwischen zwei Buchungen.

    Implementiert N-DB-01 (kein rohes SQL), F-DUP-03 (Verdachtsdubletten),
    F-DUP-05 (Auflösung: Zusammenführen oder Ignorieren) und F-CON-01
    (Versionsspalte für optimistische Sperre).

    Eine DuplicateDecision speichert die Entscheidung des Nutzers zu einem
    Paar von möglicherweise identischen Buchungen:
    - 'behalten': Ausdrückliche Nutzerentscheidung, beide Buchungen behalten.
    - 'zusammengefuehrt': Die other_transaction wurde als Duplikat der
      transaction markiert und ist unsichtbar (Transaction.merged=True).
      Die Markierung ist rückgängig machbar über das Protokoll.
    - 'verworfen': Das Paar ist kein Duplikat; nicht erneut vorschlagen.

    Jede DuplicateDecision ist an eine transaction_id gebunden. Die
    other_transaction_id kann NULL sein für Einzelentscheidungen ohne Partner
    oder eine andere transaction_id für Paar-Entscheidungen.

    Wichtig: Diese Klasse speichert die Entscheidung nur, löscht nie.
    """

    __tablename__ = "duplicate_decision"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Primäre Buchung (nicht NULL, CASCADE löscht Entscheidung bei Buchung-Löschen)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transaction.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Andere Buchung (nullable für Einzelentscheidungen, CASCADE löscht auch diese)
    other_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transaction.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Entscheidung: behalten, zusammengefuehrt oder verworfen
    decision: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    # Zeitpunkt der Entscheidung in UTC (wird von utc_now gesetzt)
    decided_at: Mapped[UtcDateTime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
    )

    # Quelle der Entscheidung (z.B. 'ui', 'api', 'script')
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    # Constraints
    __table_args__ = (
        # Entscheidung muss einen erlaubten Wert haben
        CheckConstraint(
            "decision IN ('behalten', 'zusammengefuehrt', 'verworfen')",
            name="ck_duplicate_decision_decision",
        ),
        # other_transaction_id darf nicht gleich transaction_id sein
        # (NULL ist erlaubt für Einzelentscheidungen)
        CheckConstraint(
            "other_transaction_id IS NULL OR other_transaction_id <> transaction_id",
            name="ck_duplicate_decision_no_self_reference",
        ),
        # Eindeutigkeit des Paares: (transaction_id, other_transaction_id)
        # SQL behandelt NULL als verschieden, daher wird die Eindeutigkeit
        # für Einzelentscheidungen (other_transaction_id IS NULL) von der
        # Service-Schicht sichergestellt.
        UniqueConstraint(
            "transaction_id",
            "other_transaction_id",
            name="uq_duplicate_decision_pair",
        ),
    )

    # Relationen zu Transaction (explizite foreign_keys für zwei FKs auf dieselbe Tabelle)
    transaction: Mapped["Transaction"] = relationship(
        "Transaction",
        foreign_keys=[transaction_id],
        primaryjoin="DuplicateDecision.transaction_id == Transaction.id",
        viewonly=True,
    )

    other_transaction: Mapped["Transaction | None"] = relationship(
        "Transaction",
        foreign_keys=[other_transaction_id],
        primaryjoin="DuplicateDecision.other_transaction_id == Transaction.id",
        viewonly=True,
    )


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da transaction.py nur in TYPE_CHECKING DuplicateDecision importiert.
from finue.models import transaction  # noqa: E402,F401
