"""Normalisierung und Fingerprint fuer die Dublettenkennung.

Implementiert F-DUP-01: Der Fingerprint einer Buchung ist ein SHA-256-Hex
ueber die normalisierten Felder Konto, Buchungstag, Wertstellung, Betrag in
Cent, Waehrung, Gegenpartei, Gegenpartei-IBAN und Verwendungszweck.

Normalisierung je Textfeld:

1. Unicode NFC.
2. Grossschreibung.
3. Zeilenumbrueche entfernen. MT940 bricht Verwendungszwecke nach 27 bzw.
   65 Zeichen um; die Fortsetzung wird ohne Trennzeichen angehaengt, damit
   ein Umbruch mitten im Wort denselben Text ergibt wie die ungebrochene
   Variante.
4. Mehrfach-Leerzeichen zu einem Leerzeichen zusammenfassen.
5. Trimmen.

Der Fingerprint ist deterministisch: Semantisch gleiche Buchungen mit
abweichender Schreibweise (Case, Leerraum, Zeilenumbrueche, Unicode-
Zerlegung) ergeben denselben Hash. Nicht enthalten sind Notiz, geprueft,
abgestimmt, externe Referenz und Status (F-DUP-01, F-DUP-05).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from finue.models.duplicate import DuplicateDecision
from finue.models.transaction import Transaction

# Eindeutiger Trenner zwischen den Feldern. Das Unit Separator (U+001F) kommt
# in Banktexten nicht vor und verhindert, dass Feldgrenzen durch den Inhalt
# verschoben werden.
_FIELD_SEPARATOR = "\x1f"

_WHITESPACE_RE = re.compile(r"\s+")

# F-DUP-03: Buchungstag-Fenster fuer Verdachtsdubletten (jeweils +/- Tage).
_SUSPECT_DAY_WINDOW = 2

#: Buchungsstatus einer gebuchten Buchung. Vormerkungen (``vorgemerkt``)
#: nehmen nach F-DUP-04 nicht am Fingerprint-Abgleich teil.
_BOOKED_STATUS: Final[str] = "gebucht"

__all__ = [
    "ReconcileResult",
    "SuspectedPair",
    "TransactionData",
    "compute_fingerprint",
    "find_suspected_duplicates",
    "normalize_field",
    "reconcile_multiset",
]


def normalize_field(text: str | None) -> str:
    """Normalisiert ein Textfeld exakt nach F-DUP-01.

    Args:
        text: Rohtext aus FinTS, CSV oder JSON. ``None`` und der leere
            String werden als leeres Feld behandelt.

    Returns:
        Normalisierter Text in NFC, Grossschreibung, ohne Zeilenumbrueche,
        mit zusammengefassten Leerzeichen und ohne Rand-Leerraum.
    """
    if not text:
        return ""

    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.upper()
    # splitlines() entfernt CR, LF, CRLF und weitere Unicode-Zeilenenden.
    # Das Join ohne Trenner ist fuer MT940-Umbrueche mitten im Wort noetig.
    normalized = "".join(normalized.splitlines())
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def compute_fingerprint(
    account_id: int,
    booking_date: date,
    value_date: date | None,
    amount_cents: int,
    currency: str,
    counterparty: str | None,
    counterparty_iban: str | None,
    purpose: str | None,
) -> str:
    """Berechnet den SHA-256-Fingerprint einer Buchung nach F-DUP-01.

    Die Felder werden in der von F-DUP-01 vorgegebenen Reihenfolge mit einem
    eindeutigen Trenner verkettet und als UTF-8 gehasht. Textfelder werden
    vorher mit :func:`normalize_field` normalisiert; Datumswerte werden als
    ISO-8601-Datum und der Betrag als ganze Centzahl abgebildet.

    Args:
        account_id: ID des Kontos.
        booking_date: Buchungstag.
        value_date: Wertstellung oder ``None``, wenn die Bank keine liefert.
        amount_cents: Betrag in Cent (Vorzeichen bleibt erhalten).
        currency: Waehrung, z. B. ``"EUR"``.
        counterparty: Gegenpartei oder ``None``.
        counterparty_iban: Gegenpartei-IBAN oder ``None``.
        purpose: Verwendungszweck oder ``None``.

    Returns:
        SHA-256-Hex mit 64 Zeichen in Kleinschreibung.
    """
    fields = [
        str(account_id),
        booking_date.isoformat(),
        value_date.isoformat() if value_date is not None else "",
        str(amount_cents),
        normalize_field(currency),
        normalize_field(counterparty),
        normalize_field(counterparty_iban),
        normalize_field(purpose),
    ]
    payload = _FIELD_SEPARATOR.join(fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TransactionData:
    """Eingehende Buchung fuer den Multimengen-Abgleich (F-DUP-02).

    Die Felder entsprechen den Persistenzfeldern aus
    :class:`finue.models.transaction.Transaction`. ``account_id`` fehlt
    bewusst: Es ist Argument von :func:`reconcile_multiset`, damit eine
    Lieferung nicht versehentlich Buchungen auf fremde Konten schreibt.

    Notiz, geprueft/abgestimmt samt Zeitpunkten und Herkunft sowie externe
    Referenz sind optionale Zusatzfelder fuer neu anzulegende Buchungen
    (F-ABS-01, F-IMP-07). Sie nehmen nicht am Fingerprint teil (F-ABS-06).
    """

    booking_date: date
    amount_cents: int
    currency: str
    origin: str = "fints"
    value_date: date | None = None
    counterparty: str | None = None
    counterparty_iban: str | None = None
    purpose: str | None = None
    status: str = "gebucht"
    note: str | None = None
    external_ref: str | None = None
    checked: bool = False
    checked_at: datetime | None = None
    checked_origin: str | None = None
    reconciled: bool = False
    reconciled_at: datetime | None = None
    reconciled_origin: str | None = None
    search_text: str = ""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Ergebnis des Multimengen-Abgleichs (F-DUP-02).

    Attributes:
        to_insert: Die neu angelegten, bereits der Session hinzugefuegten
            Buchungen in der Reihenfolge der Lieferung.
        skipped_count: Anzahl der Lieferzeilen, die bereits durch den
            vorhandenen Bestand abgedeckt waren.
        warnings: Klartext-Hinweise, insbesondere wenn die Bank weniger
            Buchungen liefert als gespeichert sind. Es wird nie geloescht.
    """

    to_insert: list[Transaction]
    skipped_count: int
    warnings: list[str]

    @property
    def inserted_count(self) -> int:
        """Anzahl der neu eingefuegten Buchungen."""
        return len(self.to_insert)


@dataclass(frozen=True, slots=True)
class SuspectedPair:
    """Ein Verdachtsdubletten-Paar nach F-DUP-03.

    Attributes:
        new_transaction: Die eingehende, noch nicht gespeicherte Buchung aus
            der Lieferung (Import oder Sync).
        existing_transaction: Die bereits gespeicherte Buchung desselben
            Kontos, die den Verdacht ausgeloest hat.
    """

    new_transaction: TransactionData
    existing_transaction: Transaction


def find_suspected_duplicates(
    account_id: int,
    new_transactions: Sequence[TransactionData],
    session: Session,
) -> list[SuspectedPair]:
    """Findet Verdachtsdubletten einer Lieferung gegen den Kontobestand.

    Implementiert F-DUP-03: Ein Paar ist verdaechtig, wenn es zum selben Konto
    gehoert, denselben Betrag in Cent hat, die Buchungstage hoechstens zwei Tage
    auseinanderliegen und sich der normalisierte Text (Gegenpartei oder
    Verwendungszweck) unterscheidet. Ein identischer Text ist keine
    Verdachtsdublette, sondern der F-DUP-02-Fall und wird hier nicht gemeldet.

    Die Funktion ist rein lesend. Sie legt weder Buchungen an noch loescht oder
    veraendert sie, fuehrt nichts automatisch zusammen und erzeugt keine
    :class:`finue.models.duplicate.DuplicateDecision`. Sie liefert nur die
    Kandidatenliste fuer das Import-Protokoll (``suspected_count`` in
    :class:`finue.models.import_run.ImportRun`); die Aufloesung erfolgt spaeter
    durch den Nutzer nach F-DUP-05.

    Bereits gespeicherte Buchungen mit ``merged=True`` werden ignoriert. Wurde
    ein Paar zuvor als ``verworfen`` entschieden, wird es nicht erneut
    vorgeschlagen; die Zuordnung erfolgt ueber den Fingerprint der jeweils
    anderen, bereits gespeicherten Buchung, weil die eingehende Zeile vor dem
    Import noch keine ID besitzt.

    Args:
        account_id: Konto, gegen dessen Bestand geprueft wird.
        new_transactions: Eingehende Lieferzeilen in beliebiger Reihenfolge.
        session: SQLAlchemy-Session fuer die reinen Leseabfragen.

    Returns:
        Alle Verdachts-Paare als :class:`SuspectedPair`, in der Reihenfolge der
        Lieferzeilen und je Zeile nach Buchungstag und ID des Bestands. Eine
        leere Lieferung oder ein Bestand ohne Treffer ergibt ``[]``.
    """
    if not new_transactions:
        return []

    earliest = min(item.booking_date for item in new_transactions) - timedelta(days=_SUSPECT_DAY_WINDOW)
    latest = max(item.booking_date for item in new_transactions) + timedelta(days=_SUSPECT_DAY_WINDOW)
    amounts = {item.amount_cents for item in new_transactions}

    existing_rows = list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.account_id == account_id,
                Transaction.merged.is_(False),
                Transaction.amount_cents.in_(amounts),
                Transaction.booking_date >= earliest,
                Transaction.booking_date <= latest,
            )
            .order_by(Transaction.booking_date, Transaction.id)
        ).all()
    )
    if not existing_rows:
        return []

    dismissed_fingerprints = _load_dismissed_counterparts(
        account_id=account_id,
        existing_ids={row.id for row in existing_rows},
        session=session,
    )

    pairs: list[SuspectedPair] = []
    for item in new_transactions:
        fingerprint = _fingerprint_for(account_id, item)
        for existing in existing_rows:
            if not _is_suspected_pair(item, existing):
                continue
            dismissed = dismissed_fingerprints.get(existing.id)
            if dismissed is not None and fingerprint in dismissed:
                continue
            pairs.append(SuspectedPair(new_transaction=item, existing_transaction=existing))
    return pairs


def reconcile_multiset(
    account_id: int,
    incoming: list[TransactionData],
    session: Session,
    *,
    include_pending_in_existing: bool = True,
) -> ReconcileResult:
    """Gleicht eine Lieferung als Multimenge gegen den Kontobestand ab.

    Implementiert F-DUP-02: Fuer jeden Fingerprint ``f`` gilt
    ``n_neu`` = Anzahl in ``incoming`` und ``n_db`` = Anzahl der bereits
    gespeicherten Buchungen desselben Kontos mit diesem Fingerprint. Neu
    eingefuegt werden ``max(0, n_neu - n_db)`` Buchungen mit fortlaufendem
    ``occurrence_index``. Der Index startet bei 0, wenn zu einem Fingerprint
    noch keine Buchung existiert, und setzt sonst hinter dem hoechsten
    vorhandenen Index fort. Der Index wird immer ueber alle gespeicherten
    Zeilen bestimmt, auch ueber ausgeschlossene Vormerkungen, damit der
    Unique-Constraint ``(account_id, fingerprint, occurrence_index)`` haelt.

    Bereits als ``merged`` markierte Buchungen zaehlen bei ``n_db`` mit. Ihr
    Fingerprint und Index bleiben nach F-DUP-05 belegt, damit ein spaeterer
    Sync oder Import dieselbe Buchung nicht erneut anlegt.

    ``include_pending_in_existing=False`` schliesst vorgemerkte Buchungen aus
    ``n_db`` aus (F-DUP-04). Der Sync setzt das, weil er die Vormerkungen
    derselben Lieferung vorher bereits angelegt hat und sie eine gebuchte Zeile
    mit gleichem Fingerprint nicht verdecken duerfen. Der Datei-Import nutzt
    den Standard ``True``, damit eine Datei mit Vormerkungen beim erneuten
    Import weiterhin 0 neue Buchungen erzeugt (F-DUP-02).

    Die Funktion fuegt neue Buchungen der Session hinzu und ruft ``flush()``
    auf, damit der Unique-Constraint ``(account_id, fingerprint,
    occurrence_index)`` sofort greift. Sie committet nicht und loescht nie
    etwas: Liefert die Bank weniger Buchungen als gespeichert sind, bleibt der
    Bestand unveraendert und :attr:`ReconcileResult.warnings` enthaelt einen
    Klartext-Hinweis.

    Args:
        account_id: Konto, gegen dessen Bestand abgeglichen wird.
        incoming: Gelieferte Buchungen in beliebiger Reihenfolge.
        session: SQLAlchemy-Session fuer Abfrage und Einfuegung.
        include_pending_in_existing: ``True`` (Standard) zaehlt Vormerkungen
            als Bestand mit; ``False`` schliesst sie aus ``n_db`` aus
            (F-DUP-04, Sync-Pfad).

    Returns:
        :class:`ReconcileResult` mit den neu angelegten Buchungen, der Anzahl
        bereits abgedeckter Lieferzeilen und etwaigen Warnungen.
    """
    fingerprints = [_fingerprint_for(account_id, item) for item in incoming]
    if not fingerprints:
        return ReconcileResult(to_insert=[], skipped_count=0, warnings=[])

    db_counts, next_indices = _load_existing_state(
        account_id,
        set(fingerprints),
        session,
        include_pending_in_existing=include_pending_in_existing,
    )

    new_counts = Counter(fingerprints)
    warnings: list[str] = []
    for fingerprint in dict.fromkeys(fingerprints):
        new_count = new_counts[fingerprint]
        existing_count = db_counts.get(fingerprint, 0)
        if new_count < existing_count:
            warnings.append(
                f"Fuer Fingerprint {fingerprint[:12]}: Bank lieferte {new_count} Buchung(en), "
                f"gespeichert sind {existing_count}. Es wurde nichts geloescht."
            )

    processed: Counter[str] = Counter()
    to_insert: list[Transaction] = []
    skipped_count = 0

    for item, fingerprint in zip(incoming, fingerprints, strict=True):
        ordinal = processed[fingerprint]
        processed[fingerprint] += 1

        # Die ersten n_db Vorkommen sind durch den Bestand abgedeckt.
        if ordinal < db_counts.get(fingerprint, 0):
            skipped_count += 1
            continue

        occurrence_index = next_indices.get(fingerprint, 0)
        next_indices[fingerprint] = occurrence_index + 1
        to_insert.append(_build_transaction(account_id, item, fingerprint, occurrence_index))

    if to_insert:
        session.add_all(to_insert)
        session.flush()

    return ReconcileResult(to_insert=to_insert, skipped_count=skipped_count, warnings=warnings)


def _fingerprint_for(account_id: int, item: TransactionData) -> str:
    """Berechnet den F-DUP-01-Fingerprint einer eingehenden Buchung."""
    return compute_fingerprint(
        account_id=account_id,
        booking_date=item.booking_date,
        value_date=item.value_date,
        amount_cents=item.amount_cents,
        currency=item.currency,
        counterparty=item.counterparty,
        counterparty_iban=item.counterparty_iban,
        purpose=item.purpose,
    )


def _text_key(counterparty: str | None, purpose: str | None) -> tuple[str, str]:
    """Liefert den normalisierten Text-Schluessel aus Gegenpartei und Zweck."""
    return (normalize_field(counterparty), normalize_field(purpose))


def _is_suspected_pair(item: TransactionData, existing: Transaction) -> bool:
    """Prueft die F-DUP-03-Kriterien Betrag, Tagesfenster und abweichender Text."""
    return (
        item.amount_cents == existing.amount_cents
        and abs((item.booking_date - existing.booking_date).days) <= _SUSPECT_DAY_WINDOW
        and _text_key(item.counterparty, item.purpose) != _text_key(existing.counterparty, existing.purpose)
    )


def _load_dismissed_counterparts(
    account_id: int,
    existing_ids: set[int],
    session: Session,
) -> dict[int, frozenset[str]]:
    """Liest die Fingerprints der als ``verworfen`` entschiedenen Gegenstuecke.

    F-DUP-05: Ein verworfenes Paar wird nicht erneut vorgeschlagen. Die
    eingehende Zeile hat vor dem Import noch keine ID, daher wird ihr
    F-DUP-01-Fingerprint mit dem Fingerprint der jeweils anderen, bereits
    gespeicherten Buchung verglichen. Es werden nur Gegenstuecke desselben
    Kontos beruecksichtigt.
    """
    if not existing_ids:
        return {}

    decisions = session.execute(
        select(DuplicateDecision.transaction_id, DuplicateDecision.other_transaction_id).where(
            DuplicateDecision.decision == "verworfen",
            or_(
                DuplicateDecision.transaction_id.in_(existing_ids),
                DuplicateDecision.other_transaction_id.in_(existing_ids),
            ),
        )
    ).all()

    counterpart_ids: dict[int, set[int]] = {}
    for transaction_id, other_id in decisions:
        if transaction_id in existing_ids and other_id is not None:
            counterpart_ids.setdefault(transaction_id, set()).add(other_id)
        if other_id is not None and other_id in existing_ids:
            counterpart_ids.setdefault(other_id, set()).add(transaction_id)

    all_counterpart_ids = {counterpart_id for ids in counterpart_ids.values() for counterpart_id in ids}
    if not all_counterpart_ids:
        return {}

    fingerprints = {
        row_id: fingerprint
        for row_id, fingerprint in session.execute(
            select(Transaction.id, Transaction.fingerprint).where(
                Transaction.id.in_(all_counterpart_ids),
                Transaction.account_id == account_id,
            )
        ).all()
    }

    return {
        existing_id: frozenset(
            fingerprints[counterpart_id]
            for counterpart_id in counterpart_ids[existing_id]
            if counterpart_id in fingerprints
        )
        for existing_id in counterpart_ids
    }


def _load_existing_state(
    account_id: int,
    fingerprints: set[str],
    session: Session,
    *,
    include_pending_in_existing: bool = True,
) -> tuple[dict[str, int], dict[str, int]]:
    """Liest Bestand und naechsten freien Index je Fingerprint aus der DB.

    ``merged`` zaehlt absichtlich mit (F-DUP-05). Der naechste Index ist das
    Maximum der vorhandenen Indizes plus eins; ohne Bestand ist er 0. Er wird
    immer ueber alle Zeilen bestimmt, damit eine ausgeschlossene Vormerkung
    ihren Index nicht an eine neue Buchung vergibt.

    ``include_pending_in_existing=False`` zaehlt nur gebuchte Buchungen fuer
    ``n_db`` (F-DUP-04). Der Index bleibt davon unberuehrt.
    """
    if not fingerprints:
        return {}, {}

    count_statement = (
        select(Transaction.fingerprint, func.count(Transaction.id))
        .where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .group_by(Transaction.fingerprint)
    )
    if not include_pending_in_existing:
        count_statement = count_statement.where(Transaction.status == _BOOKED_STATUS)

    db_counts: dict[str, int] = {
        fingerprint: int(count) for fingerprint, count in session.execute(count_statement).all()
    }

    index_rows = session.execute(
        select(Transaction.fingerprint, func.max(Transaction.occurrence_index))
        .where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .group_by(Transaction.fingerprint)
    ).all()

    next_indices: dict[str, int] = {
        fingerprint: (max_index if max_index is not None else -1) + 1 for fingerprint, max_index in index_rows
    }
    return db_counts, next_indices


def _build_transaction(
    account_id: int,
    item: TransactionData,
    fingerprint: str,
    occurrence_index: int,
) -> Transaction:
    """Erzeugt eine neue ORM-Buchung aus einer Lieferzeile."""
    return Transaction(
        account_id=account_id,
        status=item.status,
        booking_date=item.booking_date,
        value_date=item.value_date,
        amount_cents=item.amount_cents,
        currency=item.currency,
        counterparty=item.counterparty,
        counterparty_iban=item.counterparty_iban,
        purpose=item.purpose,
        search_text=item.search_text,
        note=item.note,
        checked=item.checked,
        checked_at=item.checked_at,
        checked_origin=item.checked_origin,
        reconciled=item.reconciled,
        reconciled_at=item.reconciled_at,
        reconciled_origin=item.reconciled_origin,
        origin=item.origin,
        external_ref=item.external_ref,
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
        merged=False,
    )
