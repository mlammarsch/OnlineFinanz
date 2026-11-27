"""Edit-Kern des Buchungsbestands: Kennzeichen setzen und Buchungsdetail.

Buendelt die schreibende Fachlogik der Einzelbuchung, damit REST (M4/M5) und
MCP (M9) dieselbe Schicht nutzen (D10) und jeder Aufrufer nur HTTP bzw.
Werkzeugaufruf uebersetzt. Der Edit-Kern kennt den Aufrufer ueber den
Parameter ``origin`` (``ui`` fuer REST, ``mcp`` fuer MCP, ``import`` fuer den
Datei-Import); er wird als Herkunft der letzten Kennzeichen-Aenderung gespeichert
(F-ABS-01) und als ``source`` ins Auditprotokoll uebernommen (F-MCP-09).

Referenzierte Anforderungen:

- **F-ABS-01** "abgestimmt" ist ein Kennzeichen mit Zeitpunkt und Herkunft der
  letzten Aenderung; vorgemerkte Buchungen lassen sich nicht abstimmen.
- **F-ABS-02** Setzen und Zuruecksetzen einzeln (der Batch-Aufruf folgt in T-110
  auf demselben Kern).
- **F-CON-01** Kennzeichen werden **gesetzt**, nicht umgeschaltet: dieselbe
  Anforderung fuehrt zum selben Ergebnis. Ein bereits gesetzter Wert aendert
  nichts (kein Versionssprung, kein Zeitstempel, kein Audit, kein Ereignis).
- **F-CON-02** Optimistisches Sperren: gibt der Aufrufer eine bekannte Version
  mit, wird sie geprueft; ein veralteter Stand ist ein Konflikt.
- **F-DET-01** Einzelbuchung mit Quelle, Import-Lauf, Kennzeichen samt
  Zeitpunkt/Herkunft und externer Referenz.
- N-DB-01 (kein rohes SQL), N-DB-02 (Zeitstempel in UTC).

Die Schicht kennt nur Standardfehler (``LookupError``, ``ValueError``,
``RuntimeError``); die API-Schicht uebersetzt sie in HTTP-Fehler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.audit import AuditLog
from finue.models.import_run import ImportRun
from finue.models.transaction import Transaction
from finue.services.events import EventBus
from finue.timeutil import utc_now

__all__ = [
    "EDIT_ORIGINS",
    "FLAGS",
    "ImportRunReference",
    "PendingReconciliationError",
    "TransactionDetail",
    "TransactionEditValidationError",
    "TransactionFlagResult",
    "TransactionNotFound",
    "TransactionVersionConflict",
    "latest_import_run",
    "set_flag",
    "transaction_detail",
]

#: Zulaessige Herkunft eines Edit-Aufrufs; Wertebereich wie
#: ``export_schema.FlagOrigin`` und die CHECK-Constraints auf ``transaction``
#: (F-ABS-01). REST nutzt ``ui``, MCP ``mcp``, der Datei-Import ``import``.
EDIT_ORIGINS: Final = frozenset({"ui", "mcp", "import"})

#: Setzbare Kennzeichen (F-ABS-01).
FLAGS: Final = frozenset({"checked", "reconciled"})

#: Status einer vorgemerkten Buchung; sie laesst sich nicht abstimmen (F-ABS-01).
PENDING_STATUS: Final = "vorgemerkt"

#: Abbildung Quelle -> Art des Import-Laufs (F-DET-01, ImportRun.kind).
_ORIGIN_TO_RUN_KIND: Final = {"fints": "sync", "csv": "csv", "json": "json"}


class TransactionNotFound(LookupError):
    """Die angeforderte Buchung existiert nicht."""


class TransactionEditValidationError(ValueError):
    """Eine Eingabe des Edit-Kerns ist ungueltig (Kennzeichen, Wert, Herkunft)."""


class PendingReconciliationError(ValueError):
    """Eine vorgemerkte Buchung soll abgestimmt werden (F-ABS-01)."""

    def __init__(self, transaction_id: int) -> None:
        self.transaction_id = transaction_id
        super().__init__(
            f"Die vorgemerkte Buchung {transaction_id} laesst sich nicht abstimmen. "
            "Bitte zuerst die Buchung uebernehmen."
        )


class TransactionVersionConflict(RuntimeError):
    """Optimistisches Sperren: die bekannte Buchungsversion passt nicht (F-CON-02)."""

    def __init__(self, transaction_id: int, expected_version: int, current_version: int) -> None:
        self.transaction_id = transaction_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Buchung {transaction_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


@dataclass(frozen=True, slots=True)
class TransactionFlagResult:
    """Ergebnis einer Kennzeichen-Aenderung (F-ABS-01, F-CON-01).

    ``changed`` ist ``False``, wenn der gewuenschte Wert bereits gesetzt war
    (Setz-Semantik, kein Versionssprung). ``changed_at`` und ``origin`` sind der
    gespeicherte Stand des Kennzeichens.
    """

    transaction_id: int
    account_id: int
    flag: str
    value: bool
    changed: bool
    version: int
    changed_at: datetime | None
    origin: str | None


@dataclass(frozen=True, slots=True)
class ImportRunReference:
    """Verweis auf den Import-Lauf einer Buchung (F-DET-01).

    Der Bestand fuehrt (Stand T-108) keine direkte Verknuepfung Buchung -> Lauf;
    der Verweis ist der juengste Lauf des Kontos mit passender Art
    (FinTS -> ``sync``, CSV -> ``csv``, JSON -> ``json``). Die Naht fuer eine
    spaetere explizite Spalte bleibt das Feld ``import_run``.
    """

    id: int
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class TransactionDetail:
    """Alle Felder einer Einzelbuchung fuer den Detailbereich (F-DET-01)."""

    id: int
    account_id: int
    account_name: str
    status: str
    origin: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None
    amount_cents: int
    currency: str
    booking_date: date
    value_date: date | None
    note: str | None
    checked: bool
    checked_at: datetime | None
    checked_origin: str | None
    reconciled: bool
    reconciled_at: datetime | None
    reconciled_origin: str | None
    external_ref: str | None
    merged: bool
    version: int
    import_run: ImportRunReference | None


# ------------------------------------------------------------------- Pruefungen


def _checked_flag(flag: object) -> str:
    """Prueft den Namen eines Kennzeichens."""

    if flag not in FLAGS:
        raise TransactionEditValidationError(f"Unbekanntes Kennzeichen: {flag!r}.")
    return str(flag)


def _checked_origin(origin: object) -> str:
    """Prueft die Herkunft eines Edit-Aufrufs (F-ABS-01)."""

    if origin not in EDIT_ORIGINS:
        raise TransactionEditValidationError(
            f"Unbekannte Herkunft: {origin!r} (erlaubt: {', '.join(sorted(EDIT_ORIGINS))})."
        )
    return str(origin)


def _checked_value(value: object) -> bool:
    """Prueft den Zielwert eines Kennzeichens."""

    if not isinstance(value, bool):
        raise TransactionEditValidationError("Der Kennzeichen-Wert muss wahr oder falsch sein.")
    return value


def _require_transaction(session: Session, transaction_id: int) -> Transaction:
    """Liefert eine Buchung oder wirft ``TransactionNotFound``."""

    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise TransactionNotFound(f"Buchung {transaction_id} nicht gefunden.")
    return transaction


def _current_origin(transaction: Transaction, flag: str) -> str | None:
    """Liest die gespeicherte Herkunft eines Kennzeichens."""

    value: Any = getattr(transaction, f"{flag}_origin")
    return value if isinstance(value, str) else None


def _current_time(transaction: Transaction, flag: str) -> datetime | None:
    """Liest den gespeicherten Zeitpunkt eines Kennzeichens."""

    value: Any = getattr(transaction, f"{flag}_at")
    return value if isinstance(value, datetime) else None


def _apply_flag(transaction: Transaction, flag: str, value: bool, moment: datetime, origin: str) -> None:
    """Setzt Kennzeichen, Zeitpunkt und Herkunft (F-ABS-01).

    Die Zeitstempelspalten sind als ``UtcDateTime`` typisiert; sie werden wie in
    ``import_run``/``sync_persist`` per ``setattr`` gesetzt.
    """

    if flag == "checked":
        transaction.checked = value
        setattr(transaction, "checked_at", moment)
        transaction.checked_origin = origin
        return
    transaction.reconciled = value
    setattr(transaction, "reconciled_at", moment)
    transaction.reconciled_origin = origin


def _append_audit(
    session: Session,
    transaction: Transaction,
    *,
    flag: str,
    previous: bool,
    value: bool,
    origin: str,
) -> None:
    """Schreibt einen Audit-Eintrag je Aenderung (F-MCP-09, F-ABS-01)."""

    session.add(
        AuditLog(
            account_id=transaction.account_id,
            entity="transaction",
            entity_id=transaction.id,
            action="updated",
            source=origin,
            changes={flag: {"from": previous, "to": value}},
        )
    )


# ------------------------------------------------------------------- Kennzeichen


def set_flag(
    session: Session,
    transaction_id: int,
    *,
    flag: str,
    value: bool,
    origin: str,
    expected_version: int | None = None,
    events: EventBus | None = None,
    now: Callable[[], datetime] | None = None,
) -> TransactionFlagResult:
    """Setzt ein Kennzeichen einer Buchung (F-ABS-01/02, F-CON-01/02).

    Setz-Semantik: Der Zielwert wird **gesetzt**, nicht umgeschaltet. Ist der
    Wert bereits gesetzt, bleibt die Buchung unveraendert (kein Versionssprung,
    kein neuer Zeitstempel, kein Audit, kein Ereignis). Andernfalls werden Wert,
    Zeitpunkt (Uhr) und Herkunft gespeichert, ein Audit-Eintrag geschrieben und
    ``transaction.changed`` veroeffentlicht.

    Args:
        session: Offene SQLAlchemy-Session.
        transaction_id: ID der Buchung.
        flag: ``checked`` oder ``reconciled``.
        value: Zielwert des Kennzeichens.
        origin: Herkunft des Aufrufs (``ui``, ``mcp`` oder ``import``).
        expected_version: Optionale bekannte Version (F-CON-02); passt sie nicht,
            ist das ein Konflikt.
        events: Optionaler Ereignisbus; ist er gesetzt, meldet der Kern
            ``transaction.changed``.
        now: Optionale Uhr; Standard ist die Systemzeit in UTC.

    Returns:
        Das Ergebnis mit ``changed``, Version und gespeichertem Kennzeichen.

    Raises:
        TransactionNotFound: Die Buchung existiert nicht.
        TransactionEditValidationError: Kennzeichen, Wert oder Herkunft sind ungueltig.
        PendingReconciliationError: Eine vorgemerkte Buchung soll abgestimmt werden (F-ABS-01).
        TransactionVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
    """

    checked_flag = _checked_flag(flag)
    checked_value = _checked_value(value)
    checked_origin = _checked_origin(origin)
    clock = now if now is not None else utc_now

    transaction = _require_transaction(session, transaction_id)

    if checked_flag == "reconciled" and checked_value and transaction.status == PENDING_STATUS:
        raise PendingReconciliationError(transaction_id)

    if expected_version is not None and transaction.version != expected_version:
        raise TransactionVersionConflict(transaction_id, expected_version, transaction.version)

    current = bool(getattr(transaction, checked_flag))
    if current == checked_value:
        # F-CON-01: Setz-Semantik - gleicher Wert aendert nichts und hebt die
        # Version nicht an.
        return TransactionFlagResult(
            transaction_id=transaction.id,
            account_id=transaction.account_id,
            flag=checked_flag,
            value=checked_value,
            changed=False,
            version=transaction.version,
            changed_at=_current_time(transaction, checked_flag),
            origin=_current_origin(transaction, checked_flag),
        )

    moment = clock()
    _apply_flag(transaction, checked_flag, checked_value, moment, checked_origin)
    _append_audit(
        session,
        transaction,
        flag=checked_flag,
        previous=current,
        value=checked_value,
        origin=checked_origin,
    )

    try:
        session.commit()
    except StaleDataError as exc:
        # Zwei gleichzeitige gleiche Aenderungen ergeben dasselbe Ergebnis
        # (F-CON-01): Hat eine parallele Anfrage den Wert bereits gesetzt, gilt
        # der Aufruf als angenommen.
        session.rollback()
        fresh = session.get(Transaction, transaction_id)
        if fresh is None:
            raise TransactionNotFound(f"Buchung {transaction_id} nicht gefunden.") from exc
        if bool(getattr(fresh, checked_flag)) == checked_value:
            return TransactionFlagResult(
                transaction_id=fresh.id,
                account_id=fresh.account_id,
                flag=checked_flag,
                value=checked_value,
                changed=True,
                version=fresh.version,
                changed_at=_current_time(fresh, checked_flag),
                origin=_current_origin(fresh, checked_flag),
            )
        raise TransactionVersionConflict(
            transaction_id,
            expected_version if expected_version is not None else 0,
            fresh.version,
        ) from exc

    session.refresh(transaction)
    if events is not None:
        events.emit("transaction.changed", account=transaction.account_id, transaction=transaction.id)

    return TransactionFlagResult(
        transaction_id=transaction.id,
        account_id=transaction.account_id,
        flag=checked_flag,
        value=checked_value,
        changed=True,
        version=transaction.version,
        changed_at=moment,
        origin=checked_origin,
    )


# ---------------------------------------------------------------------- Detail


def latest_import_run(session: Session, transaction: Transaction) -> ImportRunReference | None:
    """Liefert den juengsten passenden Import-Lauf der Buchung (F-DET-01).

    Der Bestand fuehrt (Stand T-108) keine direkte Verknuepfung Buchung -> Lauf.
    Der Verweis ist der juengste Lauf des Kontos mit zur Quelle passender Art
    (FinTS -> ``sync``, CSV -> ``csv``, JSON -> ``json``); gibt es keinen, ist
    der Wert ``None``. Die Regel steht in ``decisions.md``.
    """

    kind = _ORIGIN_TO_RUN_KIND.get(transaction.origin)
    if kind is None:
        return None

    statement = (
        select(ImportRun)
        .where(ImportRun.account_id == transaction.account_id, ImportRun.kind == kind)
        .order_by(ImportRun.started_at.desc(), ImportRun.id.desc())
        .limit(1)
    )
    run = session.scalars(statement).first()
    if run is None:
        return None
    return ImportRunReference(
        id=run.id,
        kind=run.kind,
        status=run.status,
        started_at=cast(datetime, run.started_at),
        finished_at=cast("datetime | None", run.finished_at),
    )


def transaction_detail(session: Session, transaction_id: int) -> TransactionDetail:
    """Liefert alle Felder einer Einzelbuchung (F-DET-01).

    Raises:
        TransactionNotFound: Die Buchung existiert nicht.
    """

    transaction = _require_transaction(session, transaction_id)
    account = transaction.account
    return TransactionDetail(
        id=transaction.id,
        account_id=transaction.account_id,
        account_name=account.name if account is not None else "",
        status=transaction.status,
        origin=transaction.origin,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        note=transaction.note,
        checked=transaction.checked,
        checked_at=_current_time(transaction, "checked"),
        checked_origin=transaction.checked_origin,
        reconciled=transaction.reconciled,
        reconciled_at=_current_time(transaction, "reconciled"),
        reconciled_origin=transaction.reconciled_origin,
        external_ref=transaction.external_ref,
        merged=transaction.merged,
        version=transaction.version,
        import_run=latest_import_run(session, transaction),
    )
