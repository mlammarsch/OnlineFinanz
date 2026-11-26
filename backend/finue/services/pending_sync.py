"""Vorgemerkte Buchungen ersetzen und Nutzerkennzeichen uebernehmen.

Der Service setzt zwei Anforderungen um:

- F-DUP-04: Vorgemerkte Buchungen nehmen nicht am Fingerprint-Abgleich teil.
  Jeder Sync ersetzt die vorgemerkten Buchungen eines Kontos komplett.
- F-CON-05: Ein Sync aendert nie Notiz, geprueft, abgestimmt oder externe
  Referenz bereits gebuchter Buchungen. Die Uebertragung aus F-DUP-04 ist die
  einzige Ausnahme und betrifft ausschliesslich die in diesem Lauf neu
  gebuchten Buchungen.

Ablauf im Sync (empfohlene Reihenfolge):

1. :func:`replace_pending` loescht alle vorgemerkten Buchungen des Kontos und
   legt die neu gelieferten Vormerkungen an. Die geloeschten Werte werden als
   :class:`PendingSnapshot` zurueckgegeben und zusaetzlich in der Session
   hinterlegt.
2. Der Multimengen-Abgleich legt die gebuchten Umsaetze an (F-DUP-02). Weil die
   alten Vormerkungen bereits geloescht sind, zaehlen sie dort nicht mit
   (F-DUP-04).
3. :func:`transfer_flags` uebertraegt Notiz und Pruef-Flag einer ersetzten
   Vormerkung auf die genau eine passende neu gebuchte Buchung. Passt keine
   oder mehr als eine Buchung, bleibt ein Klartext-Hinweis mit dem Code
   ``VORMERKUNG`` als :class:`finue.models.import_run.ImportRunError` im
   Protokoll.

Kandidatenkriterien nach F-DUP-04: gleicher Betrag in Cent, gleiche
normalisierte Gegenpartei und Buchungstage hoechstens zehn Tage auseinander
(einschliesslich). Nur Vormerkungen mit Notiz oder gesetztem Pruef-Flag werden
uebertragen; ohne Nutzerdaten gibt es nichts zu erhalten und keinen Hinweis.

F-CON-01: Die vorgemerkten Buchungen werden als ORM-Objekte geladen und ueber
``Session.delete()`` entfernt. Die Versionsspalte wirkt damit auch beim
Loeschen; ein zwischenzeitlich geaenderter Datensatz fuehrt zu
``sqlalchemy.orm.exc.StaleDataError`` statt zu stillem Datenverlust.

N-DB-01: Ausschliesslich SQLAlchemy-Ausdruecke, kein rohes SQL. Es finden keine
Bankzugriffe statt. Alle Meldungen sind Klartext ohne PIN, TAN, Schluessel oder
andere Geheimnisse (N-SEC-02).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finue.models.import_run import ImportRun, ImportRunError
from finue.models.transaction import Transaction
from finue.services.duplicate import TransactionData, compute_fingerprint, normalize_field

__all__ = [
    "CODE_PENDING_HINT",
    "MATCH_WINDOW_DAYS",
    "PendingReplacement",
    "PendingSnapshot",
    "PendingTransfer",
    "TransferMatch",
    "replace_pending",
    "transfer_flags",
]

#: Buchungsstatus einer Vormerkung (F-SYN-03, F-DUP-04).
PENDING_STATUS: Final[str] = "vorgemerkt"
#: Buchungsstatus einer gebuchten Buchung (F-DUP-04).
BOOKED_STATUS: Final[str] = "gebucht"
#: Kandidatenfenster fuer die Uebertragung von Notiz und Pruef-Flag (F-DUP-04).
MATCH_WINDOW_DAYS: Final[int] = 10
#: Fehlercode der Protokollzeile, wenn keine eindeutige Uebertragung moeglich ist.
CODE_PENDING_HINT: Final[str] = "VORMERKUNG"
#: Schluessel der in der Session hinterlegten, ersetzten Vormerkungen.
_REMOVED_KEY: Final[str] = "finue.pending_sync.removed"


@dataclass(frozen=True, slots=True)
class PendingSnapshot:
    """Werte einer ersetzten Vormerkung fuer die spaetere Uebertragung.

    Die Momentaufnahme entsteht vor dem Loeschen, weil die Vormerkung danach
    nicht mehr in der Datenbank steht. Sie enthaelt nur Felder, die nach
    F-DUP-04 auf eine gebuchte Buchung uebergehen koennen, sowie die
    Kandidatenkriterien Betrag, Buchungstag und Gegenpartei.
    """

    transaction_id: int
    booking_date: date
    amount_cents: int
    currency: str
    counterparty: str | None
    note: str | None
    checked: bool
    checked_at: datetime | None

    @property
    def has_user_data(self) -> bool:
        """True, wenn Notiz oder Pruef-Flag zu uebertragen sind."""
        return bool(self.note) or self.checked


@dataclass(frozen=True, slots=True)
class PendingReplacement:
    """Ergebnis von :func:`replace_pending` (F-DUP-04).

    Attributes:
        removed: Momentaufnahmen aller ersetzten Vormerkungen in der
            Reihenfolge ihrer IDs vor dem Loeschen.
        inserted: Die neu angelegten, bereits der Session hinzugefuegten
            Vormerkungen in der Reihenfolge der Lieferung.
    """

    removed: tuple[PendingSnapshot, ...]
    inserted: tuple[Transaction, ...]

    @property
    def removed_count(self) -> int:
        """Anzahl der ersetzten Vormerkungen."""
        return len(self.removed)

    @property
    def inserted_count(self) -> int:
        """Anzahl der neu angelegten Vormerkungen."""
        return len(self.inserted)


@dataclass(frozen=True, slots=True)
class TransferMatch:
    """Eine erfolgreiche Uebertragung nach F-DUP-04.

    Attributes:
        pending: Momentaufnahme der ersetzten Vormerkung.
        booked: Die neu gebuchte Buchung, die Notiz und Pruef-Flag erhalten
            hat.
    """

    pending: PendingSnapshot
    booked: Transaction


@dataclass(frozen=True, slots=True)
class PendingTransfer:
    """Ergebnis von :func:`transfer_flags` (F-DUP-04, F-CON-05).

    Attributes:
        transferred: Eindeutig zugeordnete Uebertragungen.
        ambiguous: Vormerkungen mit Nutzerdaten, die keinem oder mehreren
            Kandidaten zugeordnet werden konnten. Fuer sie wurde eine
            Protokollzeile mit :data:`CODE_PENDING_HINT` angelegt, sofern ein
            :class:`finue.models.import_run.ImportRun` uebergeben wurde.
        hints: Die Klartext-Hinweise zu ``ambiguous``, ohne Geheimnisse.
    """

    transferred: tuple[TransferMatch, ...]
    ambiguous: tuple[PendingSnapshot, ...]
    hints: tuple[str, ...]

    @property
    def transferred_count(self) -> int:
        """Anzahl der erfolgreich uebertragenen Vormerkungen."""
        return len(self.transferred)


def replace_pending(
    session: Session,
    account_id: int,
    pending_rows: Sequence[TransactionData],
) -> PendingReplacement:
    """Ersetzt alle vorgemerkten Buchungen eines Kontos komplett (F-DUP-04).

    Die vorhandenen Vormerkungen des Kontos werden geloescht, anschliessend
    werden ``pending_rows`` als neue Vormerkungen angelegt. Ein
    Fingerprint-Abgleich findet nicht statt: Eine Vormerkung mit demselben
    Fingerprint wie eine gebuchte Buchung wird trotzdem angelegt (F-DUP-04).
    Der Vorkommens-Index wird nur so gewaehlt, dass der Unique-Constraint
    ``(account_id, fingerprint, occurrence_index)`` eingehalten wird; er
    beginnt hinter dem hoechsten bereits belegten Index des Kontos.

    Die Versionsspalte der geloeschten Vormerkungen wird ueber
    ``Session.delete()`` geprueft (F-CON-01). Bei einem zwischenzeitlichen
    Update hebt SQLAlchemy ``StaleDataError`` aus, statt den fremden Stand zu
    ueberschreiben.

    Args:
        session: SQLAlchemy-Session. Die Funktion committet nicht, ruft aber
            ``flush()`` auf, damit die Unique-Constraints der neuen
            Vormerkungen sofort greifen.
        account_id: Konto, dessen Vormerkungen ersetzt werden.
        pending_rows: Neu gelieferte Vormerkungen. Alle Zeilen muessen den
            Status ``vorgemerkt`` tragen.

    Returns:
        :class:`PendingReplacement` mit den Momentaufnahmen der ersetzten und
        den neu angelegten Vormerkungen.

    Raises:
        ValueError: Eine Zeile in ``pending_rows`` ist keine Vormerkung.
    """
    _ensure_pending_rows(pending_rows)

    removed_rows = _load_pending_rows(session, account_id)
    removed = tuple(_snapshot(row) for row in removed_rows)
    for row in removed_rows:
        session.delete(row)
    session.flush()

    inserted = _insert_pending_rows(session, account_id, pending_rows)
    _store_removed(session, account_id, removed)
    return PendingReplacement(removed=removed, inserted=inserted)


def transfer_flags(
    session: Session,
    account_id: int,
    newly_booked: Sequence[Transaction],
    *,
    run: ImportRun | None = None,
) -> PendingTransfer:
    """Uebertraegt Notiz und Pruef-Flag ersetzter Vormerkungen (F-DUP-04).

    Fuer jede ersetzte Vormerkung mit Nutzerdaten wird unter ``newly_booked``
    genau ein Kandidat gesucht: gleicher Betrag in Cent, gleiche normalisierte
    Gegenpartei und Buchungstage hoechstens :data:`MATCH_WINDOW_DAYS` Tage
    auseinander (einschliesslich). Nur bei genau einem Kandidaten werden Notiz,
    Pruef-Flag und Pruef-Zeitpunkt uebernommen. Bei null Kandidaten, mehreren
    Kandidaten oder wenn mehrere Vormerkungen dieselbe Buchung beanspruchen,
    wird nichts uebernommen; der Hinweis bleibt als Klartextzeile mit
    :data:`CODE_PENDING_HINT` im Protokoll (``run``), ohne Geheimnisse.

    Bereits gesetzte Nutzerdaten der Zielbuchung werden nie ueberschrieben
    (F-CON-05). Nicht in ``newly_booked`` enthaltene Buchungen werden nie
    veraendert; der Aufrufer uebergibt ausschliesslich die in diesem Lauf neu
    gebuchten Buchungen.

    Die ersetzten Vormerkungen stammen aus :func:`replace_pending`. Wurde
    dieses im selben Session-Lebenszyklus aufgerufen, liegen sie in der
    Session; andernfalls werden die aktuell vorgemerkten Buchungen als
    Kandidaten gelesen (Aufruf vor dem Ersetzen).

    Args:
        session: SQLAlchemy-Session. Die Funktion committet nicht.
        account_id: Konto der Vormerkungen und Buchungen.
        newly_booked: In diesem Lauf neu gebuchte Buchungen.
        run: Optionaler Import-Lauf. Ist er gesetzt, werden die Hinweise als
            :class:`finue.models.import_run.ImportRunError` angehaengt.

    Returns:
        :class:`PendingTransfer` mit den Uebertragungen und Hinweisen.

    Raises:
        sqlalchemy.orm.exc.StaleDataError: Eine Zielbuchung wurde
            zwischenzeitlich geaendert (F-CON-01).
    """
    snapshots = _take_removed(session, account_id)
    if snapshots is None:
        snapshots = tuple(_snapshot(row) for row in _load_pending_rows(session, account_id))

    relevant = [snapshot for snapshot in snapshots if snapshot.has_user_data]
    if not relevant:
        return PendingTransfer(transferred=(), ambiguous=(), hints=())

    matches_by_pending = {snapshot.transaction_id: _candidates(snapshot, newly_booked) for snapshot in relevant}

    # Eine Buchung darf hoechstens eine Vormerkung aufnehmen. Beanspruchen
    # mehrere Vormerkungen dieselbe Buchung, ist die Zuordnung mehrdeutig.
    claims = Counter(
        candidate.id for candidates in matches_by_pending.values() if len(candidates) == 1 for candidate in candidates
    )

    transferred: list[TransferMatch] = []
    ambiguous: list[PendingSnapshot] = []
    hints: list[str] = []

    for snapshot in relevant:
        candidates = matches_by_pending[snapshot.transaction_id]
        reason: str | None = None
        if not candidates:
            reason = "keine passende gebuchte Buchung"
        elif len(candidates) > 1:
            reason = f"{len(candidates)} passende gebuchte Buchungen (mehrdeutig)"
        else:
            target = candidates[0]
            if claims[target.id] > 1:
                reason = "mehrere Vormerkungen beanspruchen dieselbe gebuchte Buchung"
            else:
                _apply_flags(snapshot, target)
                transferred.append(TransferMatch(pending=snapshot, booked=target))
        if reason is not None:
            ambiguous.append(snapshot)
            hints.append(_hint_text(snapshot, reason))

    if run is not None:
        for hint in hints:
            run.errors.append(ImportRunError(row_no=None, code=CODE_PENDING_HINT, message=hint))
        session.flush()

    return PendingTransfer(
        transferred=tuple(transferred),
        ambiguous=tuple(ambiguous),
        hints=tuple(hints),
    )


def _ensure_pending_rows(pending_rows: Sequence[TransactionData]) -> None:
    """Stellt sicher, dass ausschliesslich Vormerkungen geliefert werden."""
    if any(row.status != PENDING_STATUS for row in pending_rows):
        raise ValueError("replace_pending erwartet ausschliesslich vorgemerkte Buchungen")


def _load_pending_rows(session: Session, account_id: int) -> list[Transaction]:
    """Liest alle vorgemerkten Buchungen eines Kontos als ORM-Objekte."""
    statement = (
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.status == PENDING_STATUS,
        )
        .order_by(Transaction.id)
    )
    return list(session.scalars(statement).all())


def _snapshot(row: Transaction) -> PendingSnapshot:
    """Erzeugt die Momentaufnahme einer Vormerkung vor dem Loeschen."""
    return PendingSnapshot(
        transaction_id=row.id,
        booking_date=row.booking_date,
        amount_cents=row.amount_cents,
        currency=row.currency,
        counterparty=row.counterparty,
        note=row.note,
        checked=row.checked,
        # UtcDateTime ist ein TypeDecorator; der Cast haelt mypy bei der
        # ORM-Deklaration, der Wert ist immer ein datetime (N-DB-02).
        checked_at=cast("datetime | None", row.checked_at),
    )


def _insert_pending_rows(
    session: Session,
    account_id: int,
    pending_rows: Sequence[TransactionData],
) -> tuple[Transaction, ...]:
    """Legt die gelieferten Vormerkungen mit freien Vorkommens-Indizes an.

    Der Fingerprint dient nur der Eindeutigkeit des Datenmodells, nicht der
    Dublettenkennung (F-DUP-04). Der naechste Index je Fingerprint liegt hinter
    dem hoechsten bereits belegten Index des Kontos, damit gebuchte Buchungen
    mit gleichem Fingerprint nicht kollidieren.
    """
    if not pending_rows:
        return ()

    fingerprints = [
        compute_fingerprint(
            account_id=account_id,
            booking_date=row.booking_date,
            value_date=row.value_date,
            amount_cents=row.amount_cents,
            currency=row.currency,
            counterparty=row.counterparty,
            counterparty_iban=row.counterparty_iban,
            purpose=row.purpose,
        )
        for row in pending_rows
    ]
    next_index = _next_occurrence_indices(session, account_id, set(fingerprints))

    inserted: list[Transaction] = []
    for row, fingerprint in zip(pending_rows, fingerprints, strict=True):
        occurrence_index = next_index[fingerprint]
        next_index[fingerprint] = occurrence_index + 1
        transaction = Transaction(
            account_id=account_id,
            status=PENDING_STATUS,
            booking_date=row.booking_date,
            value_date=row.value_date,
            amount_cents=row.amount_cents,
            currency=row.currency,
            counterparty=row.counterparty,
            counterparty_iban=row.counterparty_iban,
            purpose=row.purpose,
            search_text=row.search_text,
            # Neue Vormerkungen kommen von der Bank und tragen keine
            # Nutzerdaten; sie entstehen erst durch Bearbeitung (F-CON-05).
            note=None,
            checked=False,
            checked_at=None,
            reconciled=False,
            reconciled_at=None,
            origin=row.origin,
            external_ref=row.external_ref,
            fingerprint=fingerprint,
            occurrence_index=occurrence_index,
            merged=False,
        )
        inserted.append(transaction)

    session.add_all(inserted)
    session.flush()
    return tuple(inserted)


def _next_occurrence_indices(
    session: Session,
    account_id: int,
    fingerprints: set[str],
) -> dict[str, int]:
    """Liest je Fingerprint den naechsten freien Vorkommens-Index des Kontos."""
    rows = session.execute(
        select(Transaction.fingerprint, func.max(Transaction.occurrence_index))
        .where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .group_by(Transaction.fingerprint)
    ).all()
    highest = {fingerprint: max_index for fingerprint, max_index in rows}
    return {fingerprint: (highest[fingerprint] if fingerprint in highest else -1) + 1 for fingerprint in fingerprints}


def _store_removed(session: Session, account_id: int, removed: tuple[PendingSnapshot, ...]) -> None:
    """Hinterlegt die ersetzten Vormerkungen fuer den Folgeschritt in der Session."""
    stored = session.info.setdefault(_REMOVED_KEY, {})
    if isinstance(stored, dict):
        cast(dict[int, tuple[PendingSnapshot, ...]], stored)[account_id] = removed


def _take_removed(session: Session, account_id: int) -> tuple[PendingSnapshot, ...] | None:
    """Holt die in der Session hinterlegten ersetzten Vormerkungen einmalig ab."""
    stored = session.info.get(_REMOVED_KEY)
    if not isinstance(stored, dict):
        return None
    removed = cast(dict[int, tuple[PendingSnapshot, ...]], stored).pop(account_id, None)
    return removed


def _candidates(snapshot: PendingSnapshot, newly_booked: Sequence[Transaction]) -> list[Transaction]:
    """Liefert die gebuchten Buchungen, die die F-DUP-04-Kriterien erfuellen."""
    normalized_counterparty = normalize_field(snapshot.counterparty)
    return [
        booked
        for booked in newly_booked
        if booked.amount_cents == snapshot.amount_cents
        and abs((booked.booking_date - snapshot.booking_date).days) <= MATCH_WINDOW_DAYS
        and normalize_field(booked.counterparty) == normalized_counterparty
    ]


def _apply_flags(snapshot: PendingSnapshot, booked: Transaction) -> None:
    """Uebernimmt Notiz und Pruef-Flag, ohne gesetzte Nutzerdaten zu ueberschreiben."""
    if snapshot.note and not booked.note:
        booked.note = snapshot.note
    if snapshot.checked and not booked.checked:
        booked.checked = True
        # UtcDateTime ist ein TypeDecorator; setattr haelt mypy mit der
        # ORM-Deklaration zufrieden (gleiches Muster wie import_run.py).
        setattr(booked, "checked_at", snapshot.checked_at)


def _hint_text(snapshot: PendingSnapshot, reason: str) -> str:
    """Formuliert den geheimnisfreien Protokollhinweis (N-SEC-02)."""
    amount = _format_amount(snapshot.amount_cents, snapshot.currency)
    return (
        f"Vormerkung vom {snapshot.booking_date.isoformat()} ueber {amount}: {reason}. "
        "Notiz/Pruef-Flag bleibt als Hinweis erhalten."
    )


def _format_amount(amount_cents: int, currency: str) -> str:
    """Formatiert einen Betrag in Cent als Dezimalstring ohne Gleitkomma."""
    sign = "-" if amount_cents < 0 else ""
    cents = abs(amount_cents)
    return f"{sign}{cents // 100},{cents % 100:02d} {currency}"
