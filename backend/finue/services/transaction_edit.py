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
  mit, wird sie geprueft; ein veralteter Stand ist ein Konflikt. Gilt fuer die
  **ersetzenden** Aenderungen (Notiz ersetzen, externe Referenz, T-109).
- **F-CON-01** Notiz "anhaengen" ist eine **atomare** Operation: ein einzelner
  UPDATE-Ausdruck haengt an, ohne das gelesene Feld in Python fortzuschreiben;
  zwei gleichzeitige Aufrufe verlieren dadurch keinen Text.
- **F-ABS-05** Optionale externe Referenz je Buchung (Freitext, setzbar ueber
  UI und MCP, durchsuchbar).
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

from sqlalchemy import CursorResult, case, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.audit import AuditLog
from finue.models.import_run import ImportRun
from finue.models.transaction import Transaction
from finue.services.events import EventBus
from finue.services.search_text import refresh_search_text
from finue.timeutil import utc_now

__all__ = [
    "EDIT_ORIGINS",
    "EXTERNAL_REF_MAX_LENGTH",
    "FLAGS",
    "NOTE_APPEND_SEPARATOR",
    "ImportRunReference",
    "PendingReconciliationError",
    "TransactionDetail",
    "TransactionEditValidationError",
    "TransactionFieldResult",
    "TransactionFlagResult",
    "TransactionNotFound",
    "TransactionVersionConflict",
    "append_note",
    "latest_import_run",
    "set_external_ref",
    "set_flag",
    "set_note",
    "transaction_detail",
]

#: Zulaessige Herkunft eines Edit-Aufrufs; Wertebereich wie
#: ``export_schema.FlagOrigin`` und die CHECK-Constraints auf ``transaction``
#: (F-ABS-01). REST nutzt ``ui``, MCP ``mcp``, der Datei-Import ``import``.
EDIT_ORIGINS: Final = frozenset({"ui", "mcp", "import"})

#: Setzbare Kennzeichen (F-ABS-01).
FLAGS: Final = frozenset({"checked", "reconciled"})

#: Trennzeichen beim Anhaengen an eine Notiz (F-CON-01). Ein Zeilenumbruch,
#: weil Notizen freier Text sind (``build_search_text`` fasst Zeilenumbrueche
#: fuer die Suche ohnehin zu einem Leerzeichen zusammen).
NOTE_APPEND_SEPARATOR: Final = "\n"

#: Laenge der Spalte ``transaction.external_ref`` (F-ABS-05); laengere Werte
#: waeren auf strengeren Dialekten (z. B. PostgreSQL) ein Fehler.
EXTERNAL_REF_MAX_LENGTH: Final = 255

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
class TransactionFieldResult:
    """Ergebnis einer Textfeld-Aenderung (Notiz, externe Referenz; T-109).

    ``field`` ist ``note`` oder ``external_ref``. ``changed`` ist ``False``,
    wenn der Zielwert bereits gleich war (Setz-Semantik, kein Versionssprung,
    kein Audit, kein Ereignis). ``search_text`` ist der neu gesetzte Inhalt der
    Suchspalte (F-SUC-01, F-ABS-05).
    """

    transaction_id: int
    account_id: int
    field: str
    value: str | None
    changed: bool
    version: int
    search_text: str


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


def _checked_note_text(text: object) -> str | None:
    """Prueft den Zielwert einer Notiz (F-CON-02, F-ABS-05).

    ``None`` und der leere String loeschen die Notiz; jeder andere String wird
    unveraendert uebernommen (Notizen sind freier Text).
    """

    if text is None:
        return None
    if not isinstance(text, str):
        raise TransactionEditValidationError("Die Notiz muss eine Zeichenkette oder leer sein.")
    return text or None


def _checked_append_text(text: object) -> str:
    """Prueft den anzuhaengenden Notiztext (F-CON-01)."""

    if not isinstance(text, str) or text.strip() == "":
        raise TransactionEditValidationError("Der anzuhaengende Notiztext darf nicht leer sein.")
    return text


def _checked_external_ref(value: object) -> str | None:
    """Prueft den Zielwert der externen Referenz (F-ABS-05)."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TransactionEditValidationError("Die externe Referenz muss eine Zeichenkette oder leer sein.")
    if len(value) > EXTERNAL_REF_MAX_LENGTH:
        raise TransactionEditValidationError(
            f"Die externe Referenz darf hoechstens {EXTERNAL_REF_MAX_LENGTH} Zeichen lang sein."
        )
    return value or None


def _same_text(current: str | None, target: str | None) -> bool:
    """Vergleicht zwei Textfeld-Werte mit Setz-Semantik (F-CON-01)."""

    return (current or None) == (target or None)


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


def _write_audit(
    session: Session,
    transaction: Transaction,
    *,
    source: str,
    changes: dict[str, Any],
) -> None:
    """Schreibt einen Audit-Eintrag je Aenderung (F-MCP-09, F-ABS-01/05)."""

    session.add(
        AuditLog(
            account_id=transaction.account_id,
            entity="transaction",
            entity_id=transaction.id,
            action="updated",
            source=source,
            changes=changes,
        )
    )


def _append_audit(
    session: Session,
    transaction: Transaction,
    *,
    flag: str,
    previous: bool,
    value: bool,
    origin: str,
) -> None:
    """Schreibt einen Audit-Eintrag je Kennzeichen-Aenderung (F-MCP-09, F-ABS-01)."""

    _write_audit(session, transaction, source=origin, changes={flag: {"from": previous, "to": value}})


def _commit_or_conflict(session: Session, transaction_id: int, expected_version: int | None) -> None:
    """Schreibt die Aenderung; ein parallel geaenderter Stand ist ein Konflikt (F-CON-02)."""

    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        fresh = session.get(Transaction, transaction_id)
        if fresh is None:
            raise TransactionNotFound(f"Buchung {transaction_id} nicht gefunden.") from exc
        raise TransactionVersionConflict(
            transaction_id,
            expected_version if expected_version is not None else 0,
            fresh.version,
        ) from exc


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


# --------------------------------------------------------- Notiz / Referenz


def set_note(
    session: Session,
    transaction_id: int,
    *,
    text: str | None,
    origin: str,
    expected_version: int | None = None,
    events: EventBus | None = None,
) -> TransactionFieldResult:
    """Ersetzt die Notiz einer Buchung (F-CON-01/02, F-ABS-05).

    Der Zielwert wird **gesetzt** (ersetzt), nicht an das Bestehende
    angefuegt; ``None`` oder der leere String loeschen die Notiz. Ist der Wert
    bereits gleich, bleibt die Buchung unveraendert (kein Versionssprung, kein
    Audit, kein Ereignis). Andernfalls werden Notiz und ``search_text`` neu
    gesetzt, ein Audit-Eintrag geschrieben und ``transaction.changed`` gemeldet.

    Args:
        session: Offene SQLAlchemy-Session.
        transaction_id: ID der Buchung.
        text: Neuer Notiztext oder ``None`` zum Loeschen.
        origin: Herkunft des Aufrufs (``ui``, ``mcp`` oder ``import``).
        expected_version: Bekannte Version (F-CON-02); passt sie nicht, ist das
            ein Konflikt.
        events: Optionaler Ereignisbus; ist er gesetzt, meldet der Kern
            ``transaction.changed``.

    Returns:
        Das Ergebnis mit ``changed``, Version und neuem ``search_text``.

    Raises:
        TransactionNotFound: Die Buchung existiert nicht.
        TransactionEditValidationError: Notiz oder Herkunft sind ungueltig.
        TransactionVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
    """

    checked_origin = _checked_origin(origin)
    checked_text = _checked_note_text(text)
    transaction = _require_transaction(session, transaction_id)

    if expected_version is not None and transaction.version != expected_version:
        raise TransactionVersionConflict(transaction_id, expected_version, transaction.version)

    previous = transaction.note
    if _same_text(previous, checked_text):
        # F-CON-01: gleicher Wert aendert nichts und hebt die Version nicht an.
        return TransactionFieldResult(
            transaction_id=transaction.id,
            account_id=transaction.account_id,
            field="note",
            value=previous,
            changed=False,
            version=transaction.version,
            search_text=transaction.search_text,
        )

    transaction.note = checked_text
    refresh_search_text(transaction)
    _write_audit(session, transaction, source=checked_origin, changes={"note": {"from": previous, "to": checked_text}})
    _commit_or_conflict(session, transaction_id, expected_version)
    session.refresh(transaction)

    if events is not None:
        events.emit("transaction.changed", account=transaction.account_id, transaction=transaction.id)

    return TransactionFieldResult(
        transaction_id=transaction.id,
        account_id=transaction.account_id,
        field="note",
        value=transaction.note,
        changed=True,
        version=transaction.version,
        search_text=transaction.search_text,
    )


def append_note(
    session: Session,
    transaction_id: int,
    *,
    text: str,
    origin: str,
    events: EventBus | None = None,
) -> TransactionFieldResult:
    """Haengt einen Text an die Notiz an - atomar (F-CON-01, F-ABS-05).

    Der Anhang ist ein **einzelner** UPDATE-Ausdruck (``note = note || sep ||
    text`` bzw. ``text``, wenn die Notiz leer ist). Es wird bewusst nichts
    vorher gelesen und in Python fortgeschrieben; zwei gleichzeitige Aufrufe
    reihen sich an der Schreibsperre auf und verlieren dadurch keinen Text.
    Die Suchspalte wird anschliessend aus dem aktuellen Stand neu gebaut
    (F-SUC-01, F-ABS-05).

    Trennzeichen: :data:`NOTE_APPEND_SEPARATOR` (Zeilenumbruch).

    Args:
        session: Offene SQLAlchemy-Session.
        transaction_id: ID der Buchung.
        text: Anzuhaengender Text; darf nach Trimmen nicht leer sein.
        origin: Herkunft des Aufrufs (``ui``, ``mcp`` oder ``import``).
        events: Optionaler Ereignisbus; ist er gesetzt, meldet der Kern
            ``transaction.changed``.

    Returns:
        Das Ergebnis mit dem neuen Notizstand, Version und ``search_text``.

    Raises:
        TransactionNotFound: Die Buchung existiert nicht.
        TransactionEditValidationError: Text oder Herkunft sind ungueltig.
    """

    checked_origin = _checked_origin(origin)
    checked_text = _checked_append_text(text)

    statement = (
        update(Transaction)
        .where(Transaction.id == transaction_id)
        .values(
            note=case(
                (or_(Transaction.note.is_(None), Transaction.note == ""), checked_text),
                else_=Transaction.note + NOTE_APPEND_SEPARATOR + checked_text,
            )
        )
    )
    outcome = cast("CursorResult[Any]", session.execute(statement))
    if outcome.rowcount == 0:
        session.rollback()
        raise TransactionNotFound(f"Buchung {transaction_id} nicht gefunden.")

    # Frisch laden: die ORM-Instanz darf den neuen Stand nicht ueberschreiben.
    transaction = session.get(Transaction, transaction_id, populate_existing=True)
    if transaction is None:
        session.rollback()
        raise TransactionNotFound(f"Buchung {transaction_id} nicht gefunden.")

    refresh_search_text(transaction)
    _write_audit(session, transaction, source=checked_origin, changes={"note": {"appended": checked_text}})
    session.commit()
    session.refresh(transaction)

    if events is not None:
        events.emit("transaction.changed", account=transaction.account_id, transaction=transaction.id)

    return TransactionFieldResult(
        transaction_id=transaction.id,
        account_id=transaction.account_id,
        field="note",
        value=transaction.note,
        changed=True,
        version=transaction.version,
        search_text=transaction.search_text,
    )


def set_external_ref(
    session: Session,
    transaction_id: int,
    *,
    value: str | None,
    origin: str,
    expected_version: int | None = None,
    events: EventBus | None = None,
) -> TransactionFieldResult:
    """Ersetzt die externe Referenz einer Buchung (F-ABS-05, F-CON-02).

    Setz-Semantik wie :func:`set_note`: gleicher Wert aendert nichts. Andernfalls
    werden Referenz und ``search_text`` gesetzt, ein Audit-Eintrag geschrieben und
    ``transaction.changed`` gemeldet.

    Args:
        session: Offene SQLAlchemy-Session.
        transaction_id: ID der Buchung.
        value: Neue externe Referenz oder ``None`` zum Loeschen.
        origin: Herkunft des Aufrufs (``ui``, ``mcp`` oder ``import``).
        expected_version: Bekannte Version (F-CON-02).
        events: Optionaler Ereignisbus.

    Returns:
        Das Ergebnis mit ``changed``, Version und neuem ``search_text``.

    Raises:
        TransactionNotFound: Die Buchung existiert nicht.
        TransactionEditValidationError: Referenz oder Herkunft sind ungueltig.
        TransactionVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
    """

    checked_origin = _checked_origin(origin)
    checked_ref = _checked_external_ref(value)
    transaction = _require_transaction(session, transaction_id)

    if expected_version is not None and transaction.version != expected_version:
        raise TransactionVersionConflict(transaction_id, expected_version, transaction.version)

    previous = transaction.external_ref
    if _same_text(previous, checked_ref):
        return TransactionFieldResult(
            transaction_id=transaction.id,
            account_id=transaction.account_id,
            field="external_ref",
            value=previous,
            changed=False,
            version=transaction.version,
            search_text=transaction.search_text,
        )

    transaction.external_ref = checked_ref
    refresh_search_text(transaction)
    _write_audit(
        session,
        transaction,
        source=checked_origin,
        changes={"external_ref": {"from": previous, "to": checked_ref}},
    )
    _commit_or_conflict(session, transaction_id, expected_version)
    session.refresh(transaction)

    if events is not None:
        events.emit("transaction.changed", account=transaction.account_id, transaction=transaction.id)

    return TransactionFieldResult(
        transaction_id=transaction.id,
        account_id=transaction.account_id,
        field="external_ref",
        value=transaction.external_ref,
        changed=True,
        version=transaction.version,
        search_text=transaction.search_text,
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
