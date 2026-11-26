"""Vorschau eines Datei-Imports ohne Datenbank-Schreibzugriff (F-IMP-06).

Die Vorschau klassifiziert jede Importzeile gegen den aktuellen Kontobestand:

- ``neu``: Der Fingerprint ist im Bestand noch nicht in ausreichender Anzahl
  vorhanden (F-DUP-02, Multimengen-Regel ``max(0, n_neu - n_db)``).
- ``vorhanden``: Die Zeile ist durch bereits gespeicherte Buchungen desselben
  Kontos abgedeckt.
- ``verdachtsdublette``: Gleicher Betrag, Buchungstag +/- 2 Tage und
  abweichender normalisierter Text (F-DUP-03).

CSV und fremdes JSON laufen ueber :func:`finue.services.mapping.apply_mapping`
(F-IMP-03), das eigene JSON-Format ueber
:func:`finue.services.import_json.parse_own_json` ohne Mapping (F-IMP-04).

Die Funktion ist rein lesend. Sie verwendet die Klassifikationslogik aus
:mod:`finue.services.duplicate`, ruft aber bewusst nicht
:func:`finue.services.duplicate.reconcile_multiset` auf, weil dieses neue
Buchungen anlegt. Stattdessen wird die Bestandszaehlung je Fingerprint als
reine Leseabfrage wiederholt. Es entstehen weder Buchungen noch Import-Laeufe,
Dublettenentscheidungen oder andere Schreibvorgaenge.

Statusreihenfolge: Eine durch den Bestand abgedeckte Zeile ist ``vorhanden``,
auch wenn ein anderer Bestandseintrag sie zusaetzlich als Verdachtsdublette
qualifizieren wuerde. Nur nicht abgedeckte Zeilen koennen
``verdachtsdublette`` sein.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finue.models.transaction import Transaction
from finue.services.duplicate import (
    TransactionData as DuplicateTransactionData,
    compute_fingerprint,
    find_suspected_duplicates,
)
from finue.services.import_json import (
    ImportFormatError,
    TransactionData as OwnJsonTransactionData,
    detect_own_format,
    parse_own_json,
)
from finue.services.mapping import (
    TransactionData as MappedTransactionData,
    apply_mapping,
)

__all__ = [
    "STATUS_EXISTING",
    "STATUS_NEW",
    "STATUS_SUSPECTED",
    "ImportPreview",
    "PreviewRow",
    "PreviewStatus",
    "PreviewSummary",
    "build_preview",
]

STATUS_NEW: Literal["neu"] = "neu"
STATUS_EXISTING: Literal["vorhanden"] = "vorhanden"
STATUS_SUSPECTED: Literal["verdachtsdublette"] = "verdachtsdublette"

PreviewStatus = Literal["neu", "vorhanden", "verdachtsdublette"]

#: Ergebnis eines Vorgaengerschritts. Beide Typen tragen die F-DUP-01-Felder;
#: das eigene JSON zusaetzlich Notiz, Herkunft und externe Referenz.
ParsedTransaction = MappedTransactionData | OwnJsonTransactionData

_OWN_JSON_KINDS = frozenset({"own_json", "own-json", "eigenes_json", "eigenes-json"})
_ALLOWED_KINDS = frozenset({None, "csv", "json"}) | _OWN_JSON_KINDS


@dataclass(frozen=True, slots=True)
class PreviewRow:
    """Eine Zeile der Import-Vorschau (F-IMP-06).

    Attributes:
        row_no: 1-basierte Zeilennummer in der Quelldatei. Bei CSV/fremdem JSON
            wird ein ``row_offset`` aus dem Mapping beruecksichtigt; vollstaendig
            leere Zeilen ueberspringt :func:`apply_mapping` und zaehlen daher
            nicht mit.
        parsed_data: Das gemappte bzw. geparste Vorgaengerergebnis. Es bleibt
            unveraendert, damit der spaetere Import die Quellfelder (Notiz,
            Kennzeichen, externe Referenz) uebernehmen kann (F-IMP-07).
        status: ``neu``, ``vorhanden`` oder ``verdachtsdublette``.
        fingerprint: F-DUP-01-Fingerprint, berechnet gegen das Zielkonto.
    """

    row_no: int
    parsed_data: ParsedTransaction
    status: PreviewStatus
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PreviewSummary:
    """Zusammenfassung der Vorschau (F-IMP-06)."""

    neu: int
    vorhanden: int
    verdachtsdublette: int

    @property
    def new_count(self) -> int:
        """Anzahl der Zeilen mit Status ``neu``."""
        return self.neu

    @property
    def existing_count(self) -> int:
        """Anzahl der Zeilen mit Status ``vorhanden``."""
        return self.vorhanden

    @property
    def suspected_count(self) -> int:
        """Anzahl der Zeilen mit Status ``verdachtsdublette``."""
        return self.verdachtsdublette


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """Ergebnis einer Import-Vorschau (F-IMP-06).

    Attributes:
        account_id: Zielkonto, gegen dessen Bestand klassifiziert wurde.
        rows: Alle Vorschauzeilen in Quelldatei-Reihenfolge.
        summary: Anzahl je Status.
    """

    account_id: int
    rows: list[PreviewRow]
    summary: PreviewSummary

    @property
    def new_count(self) -> int:
        """Anzahl der Zeilen mit Status ``neu``."""
        return self.summary.neu

    @property
    def existing_count(self) -> int:
        """Anzahl der Zeilen mit Status ``vorhanden``."""
        return self.summary.vorhanden

    @property
    def suspected_count(self) -> int:
        """Anzahl der Zeilen mit Status ``verdachtsdublette``."""
        return self.summary.verdachtsdublette


@dataclass(frozen=True, slots=True)
class _SourceRow:
    """Interner Zwischenschritt: Quellzeile und Dubletten-Datenstruktur."""

    row_no: int
    parsed_data: ParsedTransaction
    duplicate: DuplicateTransactionData


def build_preview(
    account_id: int,
    rows_or_data: object,
    mapping_or_none: Mapping[str, Any] | None,
    kind: str | None,
    session: Session,
) -> ImportPreview:
    """Erzeugt eine rein lesende Import-Vorschau (F-IMP-06).

    Args:
        account_id: Zielkonto fuer Fingerprint und Bestandsabgleich.
        rows_or_data: Bei CSV/fremdem JSON eine Iterable von Zeilenobjekten, bei
            eigenem JSON das geparste Wurzelobjekt
            (``{"format": "finanzuebersicht", ...}``).
        mapping_or_none: Mapping fuer CSV/fremdes JSON (F-IMP-03). Beim eigenen
            JSON muss ``None`` uebergeben werden, weil kein Mapping noetig ist
            (F-IMP-04).
        kind: ``"csv"``, ``"json"`` oder eine der eigenen JSON-Schreibweisen
            (``"own_json"``, ``"eigenes_json"``, ...). Ohne ``mapping`` wird ein
            JSON-Objekt mit eigenem Kopf automatisch als eigenes Format erkannt.
        session: SQLAlchemy-Session fuer reine Leseabfragen.

    Returns:
        :class:`ImportPreview` mit Zeilen und Zusammenfassung.

    Raises:
        ValueError: Bei unbekanntem ``kind``, fehlendem Mapping fuer CSV/fremdes
            JSON oder ungueltiger Zeilenliste.
        finue.services.mapping.MappingError: Bei fehlerhaften Mapping-Daten.
        finue.services.import_json.ImportFormatError: Bei fehlerhaftem eigenem
            JSON.
    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"Unbekannte Import-Art {kind!r}. Erlaubt: csv, json, own_json, eigenes_json.")

    sources = _prepare_sources(account_id, rows_or_data, mapping_or_none, kind)
    if not sources:
        return ImportPreview(account_id=account_id, rows=[], summary=PreviewSummary(0, 0, 0))

    duplicate_rows = [source.duplicate for source in sources]
    fingerprints = [_fingerprint_for(account_id, row) for row in duplicate_rows]
    existing_counts = _load_existing_counts(account_id, set(fingerprints), session)
    suspected_positions = _suspected_positions(account_id, duplicate_rows, session)

    processed: Counter[str] = Counter()
    preview_rows: list[PreviewRow] = []
    for position, (source, fingerprint) in enumerate(zip(sources, fingerprints, strict=True)):
        ordinal = processed[fingerprint]
        processed[fingerprint] += 1

        status: PreviewStatus
        if ordinal < existing_counts.get(fingerprint, 0):
            # F-DUP-02: Die ersten n_db Vorkommen sind durch den Bestand gedeckt.
            status = STATUS_EXISTING
        elif position in suspected_positions:
            status = STATUS_SUSPECTED
        else:
            status = STATUS_NEW

        preview_rows.append(
            PreviewRow(
                row_no=source.row_no,
                parsed_data=source.parsed_data,
                status=status,
                fingerprint=fingerprint,
            )
        )

    summary = PreviewSummary(
        neu=sum(1 for row in preview_rows if row.status == STATUS_NEW),
        vorhanden=sum(1 for row in preview_rows if row.status == STATUS_EXISTING),
        verdachtsdublette=sum(1 for row in preview_rows if row.status == STATUS_SUSPECTED),
    )
    return ImportPreview(account_id=account_id, rows=preview_rows, summary=summary)


def _prepare_sources(
    account_id: int,
    rows_or_data: object,
    mapping_or_none: Mapping[str, Any] | None,
    kind: str | None,
) -> list[_SourceRow]:
    """Waehlt den Vorgaenger-Parser und bereitet die Zeilen auf."""
    if _is_own_json(rows_or_data, mapping_or_none, kind):
        document = _as_document(rows_or_data)
        parsed = parse_own_json(document)
        return [
            _SourceRow(
                row_no=index,
                parsed_data=item,
                duplicate=_duplicate_from_own_json(item),
            )
            for index, item in enumerate(parsed, start=1)
        ]

    if mapping_or_none is None:
        raise ValueError("Fuer CSV oder fremdes JSON ist ein Mapping erforderlich (F-IMP-03).")

    rows = _as_rows(rows_or_data)
    mapped = apply_mapping(rows, mapping_or_none)
    origin = "csv" if kind == "csv" else "json"
    offset = _row_offset(mapping_or_none)
    return [
        _SourceRow(
            row_no=index + offset,
            parsed_data=item,
            duplicate=_duplicate_from_mapped(item, origin),
        )
        for index, item in enumerate(mapped, start=1)
    ]


def _is_own_json(
    rows_or_data: object,
    mapping_or_none: Mapping[str, Any] | None,
    kind: str | None,
) -> bool:
    """True, wenn das eigene JSON-Format ohne Mapping gelesen werden muss."""
    if kind in _OWN_JSON_KINDS:
        return True
    if kind == "csv" or mapping_or_none is not None:
        return False
    return detect_own_format(rows_or_data)


def _as_document(value: object) -> Mapping[str, object]:
    """Prueft die Wurzel des eigenen JSON-Dokuments."""
    if not isinstance(value, Mapping):
        raise ImportFormatError("JSON-Importdaten muessen ein JSON-Objekt sein.")
    return cast(Mapping[str, object], value)


def _as_rows(value: object) -> list[Mapping[str, Any]]:
    """Prueft und materialisiert Zeilen fuer :func:`apply_mapping`."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError("Importzeilen muessen eine Liste von Objekten sein.")
    rows: list[Mapping[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError("Importzeilen muessen Objekte mit Spalten bzw. Schluesseln sein.")
        rows.append({str(key): item for key, item in row.items()})
    return rows


def _row_offset(mapping_or_none: Mapping[str, Any]) -> int:
    """Liest den optionalen Zeilenversatz aus dem Mapping (F-IMP-02)."""
    value = mapping_or_none.get("row_offset")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _duplicate_from_mapped(item: MappedTransactionData, origin: str) -> DuplicateTransactionData:
    """Uebertraegt gemappte Buchungsdaten in die Dubletten-Datenstruktur."""
    return DuplicateTransactionData(
        booking_date=item.booking_date,
        value_date=item.value_date,
        amount_cents=item.amount_cents,
        currency=item.currency,
        counterparty=item.counterparty,
        counterparty_iban=item.counterparty_iban,
        purpose=item.purpose,
        origin=origin,
    )


def _duplicate_from_own_json(item: OwnJsonTransactionData) -> DuplicateTransactionData:
    """Uebertraegt eigenes JSON in die Dubletten-Datenstruktur.

    ``account_id`` und ``fingerprint`` aus der Datei werden bewusst nicht
    uebernommen. Der Fingerprint wird gegen das Zielkonto neu berechnet, damit
    die Vorschau genau das Konto prueft, in das importiert werden soll.
    """
    return DuplicateTransactionData(
        booking_date=item.booking_date,
        value_date=item.value_date,
        amount_cents=item.amount_cents,
        currency=item.currency,
        counterparty=item.counterparty,
        counterparty_iban=item.counterparty_iban,
        purpose=item.purpose,
        origin=item.origin,
        status=item.status,
        note=item.note,
        external_ref=item.external_ref,
    )


def _fingerprint_for(account_id: int, item: DuplicateTransactionData) -> str:
    """Berechnet den F-DUP-01-Fingerprint gegen das Zielkonto."""
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


def _load_existing_counts(
    account_id: int,
    fingerprints: set[str],
    session: Session,
) -> dict[str, int]:
    """Liest die Bestandsanzahl je Fingerprint rein lesend (F-DUP-02).

    ``merged`` zaehlt mit, weil zusammengefuehrte Buchungen ihren Fingerprint
    und Index behalten (F-DUP-05); ein Re-Import darf sie nicht erneut anlegen.
    """
    if not fingerprints:
        return {}

    rows = session.execute(
        select(Transaction.fingerprint, func.count(Transaction.id))
        .where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .group_by(Transaction.fingerprint)
    ).all()
    return {fingerprint: int(count) for fingerprint, count in rows}


def _suspected_positions(
    account_id: int,
    duplicate_rows: list[DuplicateTransactionData],
    session: Session,
) -> set[int]:
    """Liefert die Positionen der Zeilen, die F-DUP-03-Verdachtsdubletten sind."""
    if not duplicate_rows:
        return set()

    pairs = find_suspected_duplicates(account_id, duplicate_rows, session)
    if not pairs:
        return set()

    position_by_identity = {id(row): position for position, row in enumerate(duplicate_rows)}
    return {
        position_by_identity[id(pair.new_transaction)]
        for pair in pairs
        if id(pair.new_transaction) in position_by_identity
    }
