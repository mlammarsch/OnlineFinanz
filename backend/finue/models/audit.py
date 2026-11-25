"""Modelle für Änderungsprotokolle und Auditlogs.

Implementiert N-DB-01 (kein rohes SQL für Daten), F-MCP-09 (Auditprotokoll
für MCP-Zugriffe), F-KTO-07 (Löschen des Kontos entfernt seine Protokolle),
F-DUP-05 (Rückgängig-Semantik für Zusammenführung), und N-SEC-02
(keine Geheimnisse in den Logs).

Das Auditprotokoll ist append-only via ORM-Event (before_update): Einträge
dürfen nicht aktualisiert werden, aber beim Löschen eines Kontos CASCADE-
gelöscht werden (F-KTO-07). Core-Updates und Bulk-UPDATEs werden NICHT
abgefangen — das ist ein Schutz gegen Programmierfehler, keine Datenbank-Garantie
(N-DB-01). SQLAlchemy-Events verhindern nur ORM-basierte Updates mit RuntimeError.
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finue.db.base import Base
from finue.db.mixins import UtcDateTime
from finue.timeutil import utc_now

if TYPE_CHECKING:
    from finue.models.account import Account


class AuditLog(Base):
    """Änderungsprotokoll für Transaktionen, Konten und andere Entitäten.

    Implementiert F-MCP-09 (Auditprotokoll für MCP), F-KTO-07 (Konto-Löschung
    entfernt seine Protokolle), F-DUP-05 (Rückgängig machen von Zusammenführung),
    und N-SEC-02 (keine PINs, TANs, Schlüssel, Tokens oder Passwörter in changes).

    Append-only Semantik: Einträge werden nicht aktualisiert, sondern nur neu
    hinzugefügt oder beim Löschen eines übergeordneten Kontos gelöscht (CASCADE).
    Die Durchsetzung von N-SEC-02 liegt in der Service-Schicht und Log-Filtern,
    nicht im Modell selbst.

    Für MCP-Token-Bezug: token_id wird derzeit ohne FK gespeichert, weil die
    mcp_token-Tabelle erst in T-024 hinzukommt. Eine Migration wird das FK
    nachträglich hinzufügen.

    Hinweise für die Service-Schicht (F-MCP-09):
    - Werkzeugname steht in `action` oder `changes` (Konvention folgt)
    - Bei mehreren betroffenen Buchungen: eine Zeile je Buchung oder Liste in `changes`
    - `token_id` ist FK mit ON DELETE SET NULL (folgt per Migration)
    - Token-Name muss bei Widerruf erhalten bleiben (Audit-Trail bei Zugriff auf gelöschtem Token)
    """

    __tablename__ = "audit_log"

    # Primärschlüssel
    id: Mapped[int] = mapped_column(primary_key=True)

    # Zeitstempel in UTC (append-only, wird bei Erstellung gesetzt)
    ts: Mapped[UtcDateTime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
    )

    # Optional: Bezug zu einem Konto (wird bei Konto-Löschung CASCADE-gelöscht)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Art der Entität (z. B. 'transaction', 'account', 'duplicate_decision')
    entity: Mapped[str] = mapped_column(String(50), nullable=False)

    # ID der Entität innerhalb der Tabelle (optional für globale Ereignisse)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Aktion (z. B. 'created', 'updated', 'deleted', 'merged')
    action: Mapped[str] = mapped_column(String(50), nullable=False)

    # Quelle der Änderung (muss in ('ui', 'mcp', 'sync', 'import', 'system') sein)
    source: Mapped[str] = mapped_column(String(10), nullable=False)

    # Optionale Änderungen als JSON (Vorher-/Nachher-Werte für Rückgängig)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Token-ID für MCP-basierte Zugriffe (nullable, FK folgt in Migration)
    token_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Constraints
    __table_args__ = (
        # Source muss einen erlaubten Wert haben
        CheckConstraint(
            "source IN ('ui', 'mcp', 'sync', 'import', 'system')",
            name="ck_audit_log_source",
        ),
        # Indizes für häufige Abfragen
        Index("ix_audit_log_account_ts", "account_id", "ts"),
        Index("ix_audit_log_entity_id", "entity", "entity_id"),
    )

    # Optionale viewonly-Relation zu Account (falls die FK einfach geladen werden kann)
    account: Mapped["Account | None"] = relationship(
        "Account",
        foreign_keys=[account_id],
        viewonly=True,
    )


# SQLAlchemy-Event: before_update auf AuditLog verhindern
# Append-only: Updates sind nicht erlaubt, nur Insert und Delete (bei CASCADE)
@event.listens_for(AuditLog, "before_update", propagate=True)
def prevent_audit_log_update(mapper, connection, target):  # type: ignore[no-untyped-def]
    """Verhindert Updates auf AuditLog-Einträge (append-only).

    Löschen bleibt möglich wegen CASCADE beim Konto-Löschen. Das ist kein
    manuelles Löschen durch den Nutzer, sondern eine Integritätsmassnahme
    (F-KTO-07: Konto löschen entfernt seine Protokolle).
    """
    raise RuntimeError("Auditprotokolle können nicht aktualisiert werden; nur Löschen bei Konto-Löschung ist zulässig")


# Registrierung der Relationen. Zirkelimport vermeiden durch Import am Ende des
# Moduls, da account.py nur in TYPE_CHECKING AuditLog importiert (falls überhaupt).
# Hier wird account importiert, damit configure_mappers() allein mit finue.models.audit erfolgreich ist.
from finue.models import account  # noqa: E402,F401
