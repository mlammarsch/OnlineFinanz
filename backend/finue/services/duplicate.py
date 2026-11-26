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
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finue.models.transaction import Transaction

# Eindeutiger Trenner zwischen den Feldern. Das Unit Separator (U+001F) kommt
# in Banktexten nicht vor und verhindert, dass Feldgrenzen durch den Inhalt
# verschoben werden.
_FIELD_SEPARATOR = "\x1f"

_WHITESPACE_RE = re.compile(r"\s+")

__all__ = [
    "ReconcileResult",
    "TransactionData",
    "compute_fingerprint",
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


def reconcile_multiset(
    account_id: int,
    incoming: list[TransactionData],
    session: Session,
) -> ReconcileResult:
    """Gleicht eine Lieferung als Multimenge gegen den Kontobestand ab.

    Implementiert F-DUP-02: Fuer jeden Fingerprint ``f`` gilt
    ``n_neu`` = Anzahl in ``incoming`` und ``n_db`` = Anzahl der bereits
    gespeicherten Buchungen desselben Kontos mit diesem Fingerprint. Neu
    eingefuegt werden ``max(0, n_neu - n_db)`` Buchungen mit fortlaufendem
    ``occurrence_index``. Der Index startet bei 0, wenn zu einem Fingerprint
    noch keine Buchung existiert, und setzt sonst hinter dem hoechsten
    vorhandenen Index fort.

    Bereits als ``merged`` markierte Buchungen zaehlen bei ``n_db`` mit. Ihr
    Fingerprint und Index bleiben nach F-DUP-05 belegt, damit ein spaeterer
    Sync oder Import dieselbe Buchung nicht erneut anlegt.

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

    Returns:
        :class:`ReconcileResult` mit den neu angelegten Buchungen, der Anzahl
        bereits abgedeckter Lieferzeilen und etwaigen Warnungen.
    """
    fingerprints = [_fingerprint_for(account_id, item) for item in incoming]
    if not fingerprints:
        return ReconcileResult(to_insert=[], skipped_count=0, warnings=[])

    db_counts, next_indices = _load_existing_state(account_id, set(fingerprints), session)

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


def _load_existing_state(
    account_id: int,
    fingerprints: set[str],
    session: Session,
) -> tuple[dict[str, int], dict[str, int]]:
    """Liest Bestand und naechsten freien Index je Fingerprint aus der DB.

    ``merged`` zaehlt absichtlich mit (F-DUP-05). Der naechste Index ist das
    Maximum der vorhandenen Indizes plus eins; ohne Bestand ist er 0.
    """
    if not fingerprints:
        return {}, {}

    rows = session.execute(
        select(
            Transaction.fingerprint,
            func.count(Transaction.id),
            func.max(Transaction.occurrence_index),
        )
        .where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .group_by(Transaction.fingerprint)
    ).all()

    db_counts: dict[str, int] = {}
    next_indices: dict[str, int] = {}
    for fingerprint, count, max_index in rows:
        db_counts[fingerprint] = count
        next_indices[fingerprint] = (max_index if max_index is not None else -1) + 1
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
        origin=item.origin,
        external_ref=item.external_ref,
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
        merged=False,
    )
