"""ORM-Modelle für Finanzübersicht Backend.

Dieses Paket registriert ALLE Modellmodule in genau einem gemeinsamen
``Base.metadata``. Der Sammelimport ist zwingend, damit ``Base.metadata``
vollständig ist (alle Tabellen, Indizes und Foreign Keys) – Alembic
autogenerate und die Registertests aus T-027 sehen sonst nur die Tabellen der
zufällig zuerst importierten Module (siehe Improve-Queue zu T-027/T-028).

Implementiert N-DB-01 (kein rohes SQL für Daten), N-DB-02 (Beträge in Cent,
Zeitstempel in UTC), N-SEC-01 (PINs nur verschlüsselt, kein Klartext-Feld) und
F-CON-01/F-CON-02 (Versionsspalte für optimistisches Sperren).

Die Reihenfolge der Importe ist unkritisch: Jedes Modul registriert seine
Abhängigkeiten über einen Import am Modulende selbst, sodass auch der Import
eines einzelnen Moduls alle betroffenen Mapper konfiguriert (N-DB-01). Der
Import hier lädt zusätzlich die verbleibenden Module, damit das Metadata
vollständig wird.
"""

from finue.db.base import Base
from finue.models.account import Account, AccountGroup
from finue.models.audit import AuditLog
from finue.models.backup import BackupJob, BackupRun
from finue.models.bank import Bank, BankAccess
from finue.models.bank_error import BankErrorMapping
from finue.models.duplicate import DuplicateDecision
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.import_template import ImportTemplate
from finue.models.security import McpToken, UserSession
from finue.models.standing_order import StandingOrder
from finue.models.transaction import Transaction

__all__ = [
    "Account",
    "AccountGroup",
    "AuditLog",
    "BackupJob",
    "BackupRun",
    "Bank",
    "BankAccess",
    "BankErrorMapping",
    "Base",
    "DuplicateDecision",
    "ImportRun",
    "ImportRunError",
    "ImportTemplate",
    "McpToken",
    "StandingOrder",
    "Transaction",
    "UserSession",
]
