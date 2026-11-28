"""Aufloesung von Verdachtsdubletten: Zusammenfuehren, Rueckgaengig, Ignorieren.

Buendelt die schreibende Fachlogik der Verdachtsdubletten-Aufloesung, damit
REST (M4/M5) und MCP (M9) dieselbe Schicht nutzen (D10) und der Router nur noch
HTTP uebersetzt.

Referenzierte Anforderungen:

- **F-DUP-03** Ein Verdachtspaar gehoert zum selben Konto, hat denselben Betrag
  in Cent, Buchungstage innerhalb von +/- 2 Tagen und einen abweichenden
  normalisierten Text; Vormerkungen (``status='vorgemerkt'``) nehmen nicht teil
  (F-DUP-04).
- **F-DUP-05** Die Aufloesung erfolgt **nur durch den Nutzer**, nie automatisch:
  - *Zusammenfuehren*: die bleibende Buchung behaelt ihre Bankfelder; Notizen
    werden mit Herkunftsvermerk zusammengefuehrt; "geprueft"/"abgestimmt" sind
    gesetzt, wenn sie bei einer gesetzten waren; die externe Referenz wird
    uebernommen, bei zwei verschiedenen Werten waehlt der Nutzer (sonst 422).
    Die andere Buchung wird nur als ``merged=True`` markiert (unsichtbar),
    Fingerprint und Index bleiben belegt.
  - *Rueckgaengig* stellt beide Buchungen ueber das Aenderungsprotokoll exakt
    wieder her; ein seither geaenderter Bleiber ist ein Konflikt (409).
  - *Ignorieren* speichert "keine Dublette", ohne den Bestand zu aendern.
- **F-CON-01** Setz-Semantik: die Kennzeichen werden gesetzt; ein bereits
  gesetzter Wert aendert nichts weiter.
- **F-CON-02** Optimistisches Sperren ueber die uebergebenen Versionen; ein
  veralteter Stand ist ein Konflikt.
- N-DB-01 (kein rohes Daten-SQL), N-DB-02 (Zeitstempel in UTC).

Die Schicht kennt nur Standardfehler (``LookupError``, ``ValueError``,
``RuntimeError``); die API-Schicht uebersetzt sie in HTTP-Fehler und laedt den
aktuellen Stand fuer ein 409.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.audit import AuditLog
from finue.models.duplicate import DuplicateDecision
from finue.models.transaction import Transaction
from finue.services.duplicate import normalize_field
from finue.services.events import EventBus
from finue.services.search_text import refresh_search_text
from finue.timeutil import utc_now

__all__ = [
    "DUPLICATE_ORIGINS",
    "MERGE_AUDIT_ACTION",
    "SUSPECT_DAY_WINDOW",
    "DuplicateCandidate",
    "DuplicateNotFound",
    "DuplicateResolutionError",
    "DuplicateVersionConflict",
    "IgnoreResult",
    "MergeResult",
    "SuspectedDuplicate",
    "UndoResult",
    "ignore_duplicates",
    "list_suspected_duplicates",
    "merge_duplicates",
    "undo_merge",
]

#: Zulaessige Herkunft eines Aufloesungs-Aufrufs; Wertebereich wie
#: ``transaction_edit.EDIT_ORIGINS`` (F-ABS-01). REST nutzt ``ui``, MCP ``mcp``.
DUPLICATE_ORIGINS: Final = frozenset({"ui", "mcp", "import"})

#: Buchungstag-Fenster der Verdachtserkennung (F-DUP-03, wie
#: ``services/duplicate._SUSPECT_DAY_WINDOW``).
SUSPECT_DAY_WINDOW: Final = 2

#: Status einer gebuchten Buchung; Vormerkungen nehmen nicht teil (F-DUP-04).
BOOKED_STATUS: Final = "gebucht"

#: Aktion der Zusammenfuehrungs-Zeile im Auditprotokoll (wiedergefunden beim
#: Rueckgaengigmachen ueber ``decision_id``).
MERGE_AUDIT_ACTION: Final = "merged"

#: Aktion der Ignorier-Zeile im Auditprotokoll.
IGNORE_AUDIT_ACTION: Final = "ignored"

#: Aktion der Rueckgaengig-Zeile im Auditprotokoll.
UNDO_AUDIT_ACTION: Final = "merge_undone"

#: Deutschsprachige Kurzlabels der Herkunft fuer den Notiz-Herkunftsvermerk
#: (F-DUP-05); unbekannte Herkuenfte werden gross geschrieben uebernommen.
_ORIGIN_LABELS: Final[dict[str, str]] = {"fints": "FinTS", "csv": "CSV", "json": "JSON"}

#: Entscheidungen, die ein Paar als aufgeloest/ignoriert kennzeichnen; solche
#: Paare werden nicht erneut vorgeschlagen (F-DUP-05).
_RESOLVED_DECISIONS: Final = ("zusammengefuehrt", "verworfen")


class DuplicateNotFound(LookupError):
    """Eine Buchung, ein Paar oder eine Entscheidung existiert nicht (404)."""


class DuplicateResolutionError(ValueError):
    """Eine Eingabe der Aufloesung ist ungueltig oder im Zustand nicht moeglich (422)."""


class DuplicateVersionConflict(RuntimeError):
    """Optimistisches Sperren: eine uebergebene Version passt nicht (F-CON-02)."""

    def __init__(self, transaction_id: int, expected_version: int, current_version: int) -> None:
        self.transaction_id = transaction_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Buchung {transaction_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """Eine Seite eines Verdachtspaares mit den fuer die Anzeige noetigen Feldern.

    ``version`` ist die aktuelle Version der Buchung (F-CON-02); ``merged`` ist
    fuer die bleibende Buchung ``False`` und wird fuer die zusammengefuehrte
    Buchung erst nach dem Zusammenfuehren ``True``.
    """

    id: int
    account_id: int
    status: str
    booking_date: date
    value_date: date | None
    amount_cents: int
    currency: str
    counterparty: str | None
    counterparty_iban: str | None
    purpose: str | None
    origin: str
    note: str | None
    checked: bool
    reconciled: bool
    external_ref: str | None
    merged: bool
    version: int


@dataclass(frozen=True, slots=True)
class SuspectedDuplicate:
    """Ein offenes Verdachtspaar mit Vorschlag der bleibenden Buchung (F-DUP-03).

    ``keep`` ist die vorgeschlagene bleibende, ``other`` die zusammenzufuehrende
    Buchung. ``reason`` nennt den Grund des Vorschlags: ``"fints"`` (FinTS vor
    Datei) oder ``"alter"`` (sonst die aeltere).
    """

    account_id: int
    keep: DuplicateCandidate
    other: DuplicateCandidate
    reason: str


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Ergebnis eines Zusammenfuehrens (F-DUP-05)."""

    decision_id: int
    keep: DuplicateCandidate
    other: DuplicateCandidate
    external_ref: str | None


@dataclass(frozen=True, slots=True)
class IgnoreResult:
    """Ergebnis eines Ignorierens (F-DUP-05)."""

    decision_id: int
    keep_id: int
    other_id: int
    version: int


@dataclass(frozen=True, slots=True)
class UndoResult:
    """Ergebnis eines Rueckgaengigmachens (F-DUP-05)."""

    decision_id: int
    keep: DuplicateCandidate
    other: DuplicateCandidate


# ---------------------------------------------------------------------- Lesen


def list_suspected_duplicates(
    session: Session,
    *,
    account_ids: Sequence[int],
) -> list[SuspectedDuplicate]:
    """Liefert die offenen Verdachtspaare der uebergebenen Konten (F-DUP-03/05).

    Beruecksichtigt werden gebuchte, nicht zusammengefuehrte Buchungen. Ein Paar
    ist verdaechtig bei gleichem Betrag, Buchungstagen innerhalb von
    ``SUSPECT_DAY_WINDOW`` Tagen und abweichendem normalisiertem Text
    (Gegenpartei oder Verwendungszweck). Paare mit einer Entscheidung
    (``zusammengefuehrt`` oder ``verworfen``) fehlen, sind also weder aufgeloest
    noch ignoriert und werden nicht erneut angeboten (F-DUP-05).

    Die Liste ist deterministisch: nach Konto und der kleineren Buchungs-ID des
    normierten Paares sortiert.
    """

    if not account_ids:
        return []

    rows = list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.account_id.in_(list(account_ids)),
                Transaction.status == BOOKED_STATUS,
                Transaction.merged.is_(False),
            )
            .order_by(Transaction.account_id, Transaction.amount_cents, Transaction.booking_date, Transaction.id)
        ).all()
    )
    if len(rows) < 2:
        return []

    decided = _load_decided_pairs(session, [row.id for row in rows])

    buckets: dict[tuple[int, int], list[Transaction]] = {}
    for row in rows:
        buckets.setdefault((row.account_id, row.amount_cents), []).append(row)

    pairs: list[SuspectedDuplicate] = []
    for (account_id, _amount), group in buckets.items():
        if len(group) < 2:
            continue
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if not _is_suspected_pair(left, right):
                    continue
                if frozenset((left.id, right.id)) in decided:
                    continue
                keep, other, reason = _suggested_keep(left, right)
                pairs.append(
                    SuspectedDuplicate(
                        account_id=account_id,
                        keep=_candidate(keep),
                        other=_candidate(other),
                        reason=reason,
                    )
                )

    pairs.sort(key=lambda pair: (pair.keep.account_id, pair.keep.id, pair.other.id))
    return pairs


# ------------------------------------------------------------------- Zusammen


def merge_duplicates(
    session: Session,
    *,
    keep_id: int,
    other_id: int,
    versions: dict[int, int],
    external_ref_choice: str | None = None,
    origin: str,
    events: EventBus | None = None,
    now: Callable[[], datetime] | None = None,
) -> MergeResult:
    """Fuehrt eine Verdachtsdublette zusammen (F-DUP-05, F-CON-01/02).

    Die bleibende Buchung ``keep_id`` behaelt alle Bankfelder. Notizen werden
    mit Herkunftsvermerk zusammengefuehrt, "geprueft"/"abgestimmt" sind gesetzt,
    wenn sie bei einer gesetzt waren, und die externe Referenz wird uebernommen.
    Tragen beide verschiedene externe Referenzen, ist ``external_ref_choice``
    Pflicht (sonst 422). Die andere Buchung wird nur als ``merged=True``
    markiert; Fingerprint und Index bleiben belegt.

    ``versions`` muss fuer **beide** Buchungen die bekannte Version tragen
    (F-CON-02); passt eine nicht, ist das ein Konflikt. Es entsteht eine
    :class:`finue.models.duplicate.DuplicateDecision` (``zusammengefuehrt``) und
    ein Audit-Eintrag mit dem Vorher-Stand fuer das Rueckgaengigmachen.

    Raises:
        DuplicateNotFound: Eine der Buchungen existiert nicht.
        DuplicateResolutionError: Ungueltige Eingabe oder Zustand (z. B.
            verschiedene Konten, bereits zusammengefuehrt, fehlende Wahl).
        DuplicateVersionConflict: Eine uebergebene Version ist veraltet.
    """

    checked_origin = _checked_origin(origin)
    if keep_id == other_id:
        raise DuplicateResolutionError("Eine Buchung laesst sich nicht mit sich selbst zusammenfuehren.")

    keep = _require_transaction(session, keep_id)
    other = _require_transaction(session, other_id)

    if keep.account_id != other.account_id:
        raise DuplicateResolutionError("Die Buchungen gehoeren zu verschiedenen Konten.")
    if keep.merged or other.merged:
        raise DuplicateResolutionError("Eine der Buchungen ist bereits zusammengefuehrt.")

    _check_version(keep, versions)
    _check_version(other, versions)

    resolved_ref = _resolve_external_ref(keep.external_ref, other.external_ref, external_ref_choice)

    before = {
        "keep": _keep_snapshot(keep),
        "other": {"id": other.id, "merged": bool(other.merged)},
    }

    # Die Entscheidungszeile zuerst, damit die Abfrage keinen noch nicht
    # geschriebenen Bleiber-Stand vorzeitig schreibt (Autoflush) und die
    # Versionspruefung unten greift.
    decision, created, previous_decision = _upsert_decision(
        session, keep_id=keep.id, other_id=other.id, decision_value="zusammengefuehrt", origin=checked_origin
    )

    merged_note = _merged_note(keep.note, other.note, other.origin)
    keep.note = merged_note
    for flag in ("checked", "reconciled"):
        if not getattr(keep, flag) and getattr(other, flag):
            setattr(keep, flag, getattr(other, flag))
            setattr(keep, f"{flag}_at", getattr(other, f"{flag}_at"))
            setattr(keep, f"{flag}_origin", getattr(other, f"{flag}_origin"))
    keep.external_ref = resolved_ref
    refresh_search_text(keep)

    other.merged = True
    moment = now if now is not None else utc_now
    setattr(decision, "decided_at", moment())

    try:
        session.flush()
    except StaleDataError:
        session.rollback()
        _raise_conflict_after_stale(session, keep_id)

    # Der Bleiber-Stand **nach** dem Zusammenfuehren ist die Vergleichsversion
    # beim Rueckgaengigmachen; eine spaetere Aenderung faellt damit auf (F-DUP-05).
    before["keep"]["version"] = keep.version
    before["decision"] = {"id": decision.id, "created": created, "previous_decision": previous_decision}
    _write_audit(
        session,
        entity_id=decision.id,
        account_id=keep.account_id,
        action=MERGE_AUDIT_ACTION,
        source=checked_origin,
        changes=before,
    )

    try:
        session.commit()
    except StaleDataError:
        session.rollback()
        _raise_conflict_after_stale(session, keep_id)

    session.refresh(keep)
    session.refresh(other)

    _emit(events, keep.account_id, decision.id, keep.id, other.id)
    return MergeResult(
        decision_id=decision.id,
        keep=_candidate(keep),
        other=_candidate(other),
        external_ref=resolved_ref,
    )


def undo_merge(
    session: Session,
    decision_id: int,
    *,
    origin: str,
    events: EventBus | None = None,
    now: Callable[[], datetime] | None = None,
) -> UndoResult:
    """Macht ein Zusammenfuehren rueckgaengig (F-DUP-05, F-CON-02).

    Stellt beide Buchungen exakt wieder her (Notiz, Kennzeichen mit Zeitpunkt
    und Herkunft, externe Referenz, ``merged``) und hebt die Entscheidung auf:
    eine neu angelegte Entscheidungszeile wird entfernt, eine zuvor vorhandene
    auf ihren alten Wert zurueckgesetzt. War die bleibende Buchung seither
    geaendert (abweichende Version), ist das ein Konflikt (409).

    Raises:
        DuplicateNotFound: Die Entscheidung oder eine Buchung existiert nicht.
        DuplicateVersionConflict: Die bleibende Buchung wurde seither geaendert.
    """

    checked_origin = _checked_origin(origin)
    decision = session.get(DuplicateDecision, decision_id)
    if decision is None or decision.decision != "zusammengefuehrt":
        raise DuplicateNotFound(f"Zusammenfuehrung {decision_id} nicht gefunden.")

    entry = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity == "duplicate_decision",
            AuditLog.entity_id == decision_id,
            AuditLog.action == MERGE_AUDIT_ACTION,
        )
        .order_by(AuditLog.id.desc())
    ).first()
    if entry is None or not isinstance(entry.changes, dict):
        raise DuplicateNotFound(f"Rueckgaengig-Daten zu Zusammenfuehrung {decision_id} fehlen.")

    recorded = entry.changes
    before = recorded.get("keep")
    if not isinstance(before, dict):
        raise DuplicateNotFound(f"Rueckgaengig-Daten zu Zusammenfuehrung {decision_id} fehlen.")

    keep_id_raw = before.get("id")
    keep_id = keep_id_raw if isinstance(keep_id_raw, int) else decision.transaction_id
    keep = session.get(Transaction, keep_id)

    other_record = recorded.get("other")
    other_id_raw = other_record.get("id") if isinstance(other_record, dict) else None
    other_id = other_id_raw if isinstance(other_id_raw, int) else decision.other_transaction_id
    if other_id is None:
        raise DuplicateNotFound(f"Buchungen zu Zusammenfuehrung {decision_id} nicht gefunden.")
    other = session.get(Transaction, other_id)
    if keep is None or other is None:
        raise DuplicateNotFound(f"Buchungen zu Zusammenfuehrung {decision_id} nicht gefunden.")

    expected_version = before.get("version")
    if isinstance(expected_version, int) and keep.version != expected_version:
        raise DuplicateVersionConflict(keep.id, expected_version, keep.version)

    _restore_keep(keep, before)
    other.merged = bool(other_record.get("merged", False)) if isinstance(other_record, dict) else False

    decision_record = recorded.get("decision")
    previous_decision = None
    decision_created = True
    if isinstance(decision_record, dict):
        previous_decision = decision_record.get("previous_decision")
        decision_created = bool(decision_record.get("created", True))

    if decision_created:
        session.delete(decision)
    else:
        decision.decision = str(previous_decision) if previous_decision is not None else "verworfen"
        decision.source = checked_origin
        moment = now if now is not None else utc_now
        setattr(decision, "decided_at", moment())

    try:
        session.flush()
    except StaleDataError:
        session.rollback()
        _raise_conflict_after_stale(session, keep.id)

    _write_audit(
        session,
        entity_id=decision_id,
        account_id=keep.account_id,
        action=UNDO_AUDIT_ACTION,
        source=checked_origin,
        changes={"keep_id": keep.id, "other_id": other.id},
    )

    try:
        session.commit()
    except StaleDataError:
        session.rollback()
        _raise_conflict_after_stale(session, keep.id)

    session.refresh(keep)
    session.refresh(other)
    _emit(events, keep.account_id, decision_id, keep.id, other.id)
    return UndoResult(decision_id=decision_id, keep=_candidate(keep), other=_candidate(other))


# ------------------------------------------------------------------ Ignorieren


def ignore_duplicates(
    session: Session,
    *,
    keep_id: int,
    other_id: int,
    versions: dict[int, int] | None = None,
    origin: str,
    events: EventBus | None = None,
    now: Callable[[], datetime] | None = None,
) -> IgnoreResult:
    """Speichert ein Paar als "keine Dublette" (F-DUP-05).

    Das Paar erscheint danach weder in der Liste noch nach erneutem Import oder
    Sync wieder (die Bestandszählung des Imports beruecksichtigt die
    ``verworfen``-Entscheidung). Der Bestand selbst bleibt unveraendert.

    Raises:
        DuplicateNotFound: Eine der Buchungen existiert nicht.
        DuplicateResolutionError: Die Buchungen gehoeren nicht zu einem
            Verdachtspaar desselben Kontos.
        DuplicateVersionConflict: Eine uebergebene Version ist veraltet.
    """

    checked_origin = _checked_origin(origin)
    if keep_id == other_id:
        raise DuplicateResolutionError("Ein Paar aus einer Buchung ist nicht sinnvoll.")

    keep = _require_transaction(session, keep_id)
    other = _require_transaction(session, other_id)

    if keep.account_id != other.account_id:
        raise DuplicateResolutionError("Die Buchungen gehoeren zu verschiedenen Konten.")
    if not _is_suspected_pair(keep, other):
        raise DuplicateResolutionError("Die Buchungen sind kein Verdachtspaar (Betrag, Tag oder Text passen nicht).")

    if versions:
        _check_version(keep, versions)
        _check_version(other, versions)

    decision, _created, _previous = _upsert_decision(
        session, keep_id=keep.id, other_id=other.id, decision_value="verworfen", origin=checked_origin
    )
    moment = now if now is not None else utc_now
    setattr(decision, "decided_at", moment())

    _write_audit(
        session,
        entity_id=decision.id,
        account_id=keep.account_id,
        action=IGNORE_AUDIT_ACTION,
        source=checked_origin,
        changes={"keep_id": keep.id, "other_id": other.id},
    )

    try:
        session.commit()
    except StaleDataError:
        session.rollback()
        _raise_conflict_after_stale(session, keep.id)

    _emit(events, keep.account_id, decision.id, keep.id, other.id)
    return IgnoreResult(decision_id=decision.id, keep_id=keep.id, other_id=other.id, version=decision.version)


# ------------------------------------------------------------------- Helfer


def _candidate(transaction: Transaction) -> DuplicateCandidate:
    """Uebersetzt eine Buchung in ein Anzeigeobjekt (F-DUP-05)."""

    return DuplicateCandidate(
        id=transaction.id,
        account_id=transaction.account_id,
        status=transaction.status,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
        origin=transaction.origin,
        note=transaction.note,
        checked=transaction.checked,
        reconciled=transaction.reconciled,
        external_ref=transaction.external_ref,
        merged=transaction.merged,
        version=transaction.version,
    )


def _checked_origin(origin: object) -> str:
    """Prueft die Herkunft eines Aufloesungs-Aufrufs (F-ABS-01)."""

    if origin not in DUPLICATE_ORIGINS:
        raise DuplicateResolutionError(
            f"Unbekannte Herkunft: {origin!r} (erlaubt: {', '.join(sorted(DUPLICATE_ORIGINS))})."
        )
    return str(origin)


def _require_transaction(session: Session, transaction_id: int) -> Transaction:
    """Liefert eine Buchung oder wirft ``DuplicateNotFound``."""

    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise DuplicateNotFound(f"Buchung {transaction_id} nicht gefunden.")
    return transaction


def _check_version(transaction: Transaction, versions: dict[int, int]) -> None:
    """Prueft die uebergebene Version einer Buchung (F-CON-02)."""

    expected = versions.get(transaction.id)
    if expected is None:
        raise DuplicateResolutionError("Fuer beide Buchungen wird die aktuelle Version erwartet.")
    if transaction.version != expected:
        raise DuplicateVersionConflict(transaction.id, expected, transaction.version)


def _raise_conflict_after_stale(session: Session, transaction_id: int) -> NoReturn:
    """Uebersetzt einen StaleDataError in einen Versionskonflikt bzw. 404."""

    fresh = session.get(Transaction, transaction_id)
    if fresh is None:
        raise DuplicateNotFound(f"Buchung {transaction_id} nicht gefunden.")
    raise DuplicateVersionConflict(transaction_id, 0, fresh.version)


def _text_key(counterparty: str | None, purpose: str | None) -> tuple[str, str]:
    """Liefert den normalisierten Text-Schluessel aus Gegenpartei und Zweck."""

    return (normalize_field(counterparty), normalize_field(purpose))


def _is_suspected_pair(left: Transaction, right: Transaction) -> bool:
    """Prueft die F-DUP-03-Kriterien Betrag, Tagesfenster und abweichender Text."""

    return (
        left.amount_cents == right.amount_cents
        and abs((left.booking_date - right.booking_date).days) <= SUSPECT_DAY_WINDOW
        and _text_key(left.counterparty, left.purpose) != _text_key(right.counterparty, right.purpose)
    )


def _suggested_keep(left: Transaction, right: Transaction) -> tuple[Transaction, Transaction, str]:
    """Waehlt die bleibende Buchung: FinTS vor Datei, sonst die aeltere.

    Returns:
        ``(keep, other, reason)`` mit ``reason`` aus ``"fints"`` oder ``"alter"``.
    """

    left_fints = left.origin == "fints"
    right_fints = right.origin == "fints"
    if left_fints and not right_fints:
        return left, right, "fints"
    if right_fints and not left_fints:
        return right, left, "fints"
    if (left.booking_date, left.id) <= (right.booking_date, right.id):
        return left, right, "alter"
    return right, left, "alter"


def _resolve_external_ref(keep_ref: str | None, other_ref: str | None, choice: str | None) -> str | None:
    """Waehlt die externe Referenz der bleibenden Buchung (F-DUP-05)."""

    if keep_ref and other_ref and keep_ref != other_ref:
        if choice is None:
            raise DuplicateResolutionError(
                "Beide Buchungen tragen eine verschiedene externe Referenz; bitte eine waehlen."
            )
        if choice not in (keep_ref, other_ref):
            raise DuplicateResolutionError("Die gewaehlte externe Referenz stammt nicht aus dem Paar.")
        return choice
    return keep_ref or other_ref


def _merged_note(keep_note: str | None, other_note: str | None, other_origin: str) -> str | None:
    """Fuehrt beide Notizen mit Herkunftsvermerk zusammen (F-DUP-05).

    Der eigene Text bleibt vorne, der beigesteuerte Text der anderen Buchung
    folgt mit dem Vermerk ``[Herkunft]`` in einer neuen Zeile.
    """

    parts: list[str] = []
    if keep_note:
        parts.append(keep_note)
    if other_note:
        label = _ORIGIN_LABELS.get(other_origin, other_origin.upper())
        parts.append(f"[{label}] {other_note}")
    return "\n".join(parts) if parts else None


def _keep_snapshot(transaction: Transaction) -> dict[str, Any]:
    """Sichert den Vorher-Stand einer Buchung fuer das Rueckgaengigmachen (F-DUP-05).

    Zeitstempel werden als ISO-8601-Zeichenketten abgelegt, weil die JSON-Spalte
    des Auditprotokolls keine ``datetime``-Werte traegt.
    """

    def _iso(value: object) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    return {
        "id": transaction.id,
        "version": transaction.version,
        "note": transaction.note,
        "checked": bool(transaction.checked),
        "checked_at": _iso(transaction.checked_at),
        "checked_origin": transaction.checked_origin,
        "reconciled": bool(transaction.reconciled),
        "reconciled_at": _iso(transaction.reconciled_at),
        "reconciled_origin": transaction.reconciled_origin,
        "external_ref": transaction.external_ref,
    }


def _restore_keep(transaction: Transaction, before: dict[str, Any]) -> None:
    """Setzt die Felder einer Buchung exakt auf den gesicherten Stand zurueck."""

    transaction.note = before.get("note")
    transaction.checked = bool(before.get("checked", False))
    setattr(transaction, "checked_at", _parse_iso(before.get("checked_at")))
    transaction.checked_origin = before.get("checked_origin")
    transaction.reconciled = bool(before.get("reconciled", False))
    setattr(transaction, "reconciled_at", _parse_iso(before.get("reconciled_at")))
    transaction.reconciled_origin = before.get("reconciled_origin")
    transaction.external_ref = before.get("external_ref")
    refresh_search_text(transaction)


def _parse_iso(value: object) -> datetime | None:
    """Parst einen ISO-8601-Zeitstempel aus dem Auditprotokoll."""

    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value)


def _upsert_decision(
    session: Session,
    *,
    keep_id: int,
    other_id: int,
    decision_value: str,
    origin: str,
) -> tuple[DuplicateDecision, bool, str | None]:
    """Legt eine Entscheidungszeile an oder aktualisiert die vorhandene.

    Die Paar-Eindeutigkeit ist richtungsabhaengig; eine vorhandene Zeile in
    umgekehrter Richtung wird auf die aktuelle Ausrichtung normalisiert, statt
    eine zweite Zeile anzulegen (Modellhinweis in ``models/duplicate.py``).

    Returns:
        ``(decision, created, previous_decision)``; ``created`` ist ``False``,
        wenn eine vorhandene Zeile aktualisiert wurde.
    """

    decision = session.scalars(
        select(DuplicateDecision).where(
            DuplicateDecision.transaction_id == keep_id,
            DuplicateDecision.other_transaction_id == other_id,
        )
    ).first()
    if decision is not None:
        previous = decision.decision
        decision.decision = decision_value
        decision.source = origin
        return decision, False, previous

    reversed_decision = session.scalars(
        select(DuplicateDecision).where(
            DuplicateDecision.transaction_id == other_id,
            DuplicateDecision.other_transaction_id == keep_id,
        )
    ).first()
    if reversed_decision is not None:
        previous = reversed_decision.decision
        reversed_decision.transaction_id = keep_id
        reversed_decision.other_transaction_id = other_id
        reversed_decision.decision = decision_value
        reversed_decision.source = origin
        return reversed_decision, False, previous

    created_decision = DuplicateDecision(
        transaction_id=keep_id,
        other_transaction_id=other_id,
        decision=decision_value,
        source=origin,
        decided_at=utc_now(),
    )
    session.add(created_decision)
    return created_decision, True, None


def _write_audit(
    session: Session,
    *,
    entity_id: int,
    account_id: int,
    action: str,
    source: str,
    changes: dict[str, Any],
) -> None:
    """Schreibt eine Audit-Zeile zur Dublettenaufloesung (F-MCP-09, F-DUP-05)."""

    session.add(
        AuditLog(
            account_id=account_id,
            entity="duplicate_decision",
            entity_id=entity_id,
            action=action,
            source=source,
            changes=changes,
        )
    )


def _load_decided_pairs(session: Session, transaction_ids: Sequence[int]) -> set[frozenset[int]]:
    """Liest die bereits entschiedenen Paare der uebergebenen Buchungen (F-DUP-05)."""

    if not transaction_ids:
        return set()

    rows = session.execute(
        select(DuplicateDecision.transaction_id, DuplicateDecision.other_transaction_id).where(
            DuplicateDecision.decision.in_(_RESOLVED_DECISIONS),
            DuplicateDecision.transaction_id.in_(list(transaction_ids)),
            DuplicateDecision.other_transaction_id.is_not(None),
        )
    ).all()
    return {frozenset((transaction_id, other_id)) for transaction_id, other_id in rows if other_id is not None}


def _emit(
    events: EventBus | None,
    account_id: int,
    decision_id: int,
    keep_id: int,
    other_id: int,
) -> None:
    """Meldet ``duplicate.changed`` mit IDs, aber ohne Inhalte (F-CON-04, N-SEC-02)."""

    if events is not None:
        events.emit(
            "duplicate.changed",
            account=account_id,
            duplicate=decision_id,
            keep=keep_id,
            other=other_id,
        )
