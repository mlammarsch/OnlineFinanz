"""Durchfuehrung eines Datei-Imports mit ImportRun-Protokoll (F-IMP-06, F-SYN-07).

Der Service verbindet die vorhandenen Bausteine:

- :func:`finue.services.import_preview.build_preview` klassifiziert jede Zeile
  gegen den aktuellen Kontobestand (F-DUP-02, F-DUP-03, F-IMP-06).
- :func:`finue.services.duplicate.reconcile_multiset` legt ausschliesslich die
  noch nicht vorhandenen Buchungen an (F-DUP-02).
- :class:`finue.models.import_run.ImportRun` protokolliert den Lauf mit
  Zeitpunkt, Konto, Status, Zaehlern und Klartext (F-SYN-07, F-IMP-06).
- :class:`finue.models.import_run.ImportRunError` protokolliert Zeilenfehler
  ohne Abbruch des restlichen Imports.

Der Import ist idempotent: Ein wiederholter Lauf derselben Daten legt keine
neue Buchung an und meldet ``new_count == 0``.

F-IMP-07: Neue Buchungen uebernehmen Notiz, geprueft/abgestimmt samt
Zeitpunkten und externe Referenz immer aus der Datei. Bereits vorhandene
Buchungen desselben Fingerprints bleiben im Standard unveraendert
(``restore_flags_for_existing=False``, F-ABS-06). Nur im Wiederherstellungsfall
(``restore_flags_for_existing=True``) werden diese Werte auch auf die
vorhandenen Buchungen uebertragen. Der Fingerprint selbst enthaelt diese
Felder nicht (F-DUP-01, F-ABS-06).

Zeilenfehler beim Parsing oder Mapping werden je Zeile protokolliert; die
uebrigen Zeilen werden weiter importiert. Dokument- oder Laufzeitfehler
brechen den Lauf ab.

Der abschliessende Status folgt F-SYN-07: ``erfolgreich`` ohne Zeilenfehler,
``teilweise`` bei Zeilenfehlern mit mindestens einer verarbeiteten Zeile und
``fehler``, wenn alle Zeilen fehlerhaft waren oder ein Dokument- bzw.
Laufzeitfehler auftrat. ``abgebrochen`` und ``zeitueberschreitung`` entstehen
im FinTS-Sync, nicht in diesem Service; ``laeuft`` und ``wartet_auf_tan`` sind
technische Zwischenzustaende.

Transaktionssicherheit: Alle Schreibvorgaenge eines Laufs (ImportRun,
Buchungen, ImportRunError) werden in genau einem Commit gespeichert. Tritt
waehrend der Verarbeitung eine unerwartete Ausnahme auf, wird die Transaktion
vollstaendig zurueckgerollt und anschliessend ein eigener fehlerhafter
ImportRun mit Klartext committed. Ein halb geschriebener Zustand bleibt damit
nicht zurueck.

N-SEC-02: Meldungen enthalten nur Klartext aus dem Import (z. B. Zeilennummer
und Feldname), niemals PIN, TAN, Schluessel, Tokens oder Zugangsdaten. Bank-
zugriffe finden in diesem Service nicht statt (N-DB-01: kein rohes SQL).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.import_run import ImportRun, ImportRunError
from finue.models.transaction import Transaction
from finue.services.duplicate import (
    TransactionData as DuplicateTransactionData,
    reconcile_multiset,
)
from finue.services.import_json import (
    ImportFormatError,
    TransactionData as OwnJsonTransactionData,
    detect_own_format,
    parse_own_json,
)
from finue.services.import_preview import (
    STATUS_EXISTING,
    ImportPreview,
    ParsedTransaction,
    build_preview,
)
from finue.services.mapping import (
    MappingError,
    TransactionData as MappedTransactionData,
    apply_mapping,
)
from finue.services.search_text import build_search_text, refresh_search_text
from finue.timeutil import utc_now

__all__ = ["execute_import"]

#: Schreibweisen fuer das eigene JSON-Format ohne Mapping (F-IMP-04).
_OWN_JSON_KINDS = frozenset({"own_json", "own-json", "eigenes_json", "eigenes-json"})

#: Maximale Laenge einer Klartextmeldung im Protokoll.
_MESSAGE_LIMIT = 500

#: Fehlercodes der ImportRunError-Zeilen (Spalte ``code``, max. 20 Zeichen).
_CODE_MAPPING = "mapping_error"
_CODE_ROW_INVALID = "row_invalid"
_CODE_RUN_FAILED = "run_failed"


@dataclass(frozen=True, slots=True)
class _RowIssue:
    """Ein zeilenbezogener Fehler waehrend der Importvorbereitung."""

    row_no: int | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class _PreparedInput:
    """Fuer die Vorschau aufbereitete Eingabe samt Zeilenfehlern.

    Attributes:
        data: Rohdaten fuer :func:`build_preview` (gute Zeilen bzw. Dokument
            ohne fehlerhafte Transaktionen).
        mapping: Mapping fuer CSV/fremdes JSON, beim eigenen JSON ``None``.
        kind: Import-Art fuer :func:`build_preview`.
        origin: Herkunft der neu anzulegenden Buchungen (``csv`` oder ``json``).
        issues: Zeilenfehler, die protokolliert, aber nicht importiert werden.
    """

    data: object
    mapping: Mapping[str, Any] | None
    kind: str | None
    origin: str
    issues: list[_RowIssue]


def execute_import(
    account_id: int,
    rows_or_data: object,
    mapping_or_none: Mapping[str, Any] | None,
    kind: str | None,
    session: Session,
    *,
    restore_flags_for_existing: bool = False,
) -> ImportRun:
    """Fuehrt einen CSV-/JSON-Import aus und protokolliert ihn (F-IMP-06).

    Args:
        account_id: Zielkonto. Die Buchungen werden ausschliesslich diesem
            Konto zugeordnet; Fingerprints werden dagegen neu berechnet.
        rows_or_data: Bei CSV/fremdem JSON eine Iterable von Zeilenobjekten,
            beim eigenen JSON das geparste Wurzelobjekt.
        mapping_or_none: Mapping fuer CSV/fremdes JSON (F-IMP-03); beim eigenen
            JSON ``None``.
        kind: ``"csv"``, ``"json"`` oder eine Schreibweise des eigenen JSON-
            Formats (``"own_json"``, ``"eigenes_json"``, ...).
        session: SQLAlchemy-Session. Der Service committet den Lauf selbst.
        restore_flags_for_existing: F-IMP-07-Wiederherstellung. Bei ``False``
            (Standard) erhalten nur neue Buchungen Notiz, geprueft/abgestimmt
            samt Zeitpunkten und externe Referenz; vorhandene Buchungen
            desselben Fingerprints bleiben unveraendert (F-ABS-06). Bei
            ``True`` werden die Dateiwerte zusaetzlich auf die vorhandenen
            Buchungen uebertragen.

    Returns:
        Der persistierte :class:`ImportRun`. Sein Status ist ``erfolgreich``
        ohne Zeilenfehler, ``teilweise`` bei fehlerhaften Zeilen, die den
        uebrigen Import nicht verhindern, und ``fehler``, wenn alle Zeilen
        fehlerhaft waren oder ein Dokument- bzw. Laufzeitfehler auftrat.

    Raises:
        ValueError: Bei unbekannter Import-Art (``kind``). In diesem Fall wird
            kein Lauf angelegt, weil das ein Programmierfehler ist.
    """
    run_kind = _run_kind(kind)
    run = ImportRun(
        account_id=account_id,
        kind=run_kind,
        status="laeuft",
        message="Import laeuft.",
    )

    try:
        session.add(run)
        session.flush()

        prepared = _prepare_input(rows_or_data, mapping_or_none, kind)
        preview = build_preview(
            account_id,
            prepared.data,
            prepared.mapping,
            prepared.kind,
            session,
        )

        # F-IMP-07: Vor dem Anlegen neuer Buchungen die Wiederherstellung auf
        # den vorhandenen Bestand anwenden. So trifft die Zuordnung per
        # Fingerprint/Vorkommen ausschliesslich bereits gespeicherte Buchungen;
        # neue Buchungen erhalten ihre Dateiwerte beim Einfuegen.
        if restore_flags_for_existing:
            _restore_existing_flags(account_id, preview, session)

        # reconcile_multiset erhaelt alle geparsten Zeilen, damit der
        # Multimengen-Abgleich die gelieferte Anzahl je Fingerprint kennt
        # (F-DUP-02). Geschrieben werden ausschliesslich die von ihm
        # ermittelten neuen Buchungen (``to_insert``).
        incoming = [_to_duplicate_data(row.parsed_data, prepared.origin) for row in preview.rows]
        reconcile_multiset(account_id, incoming, session)

        _finish_successful_run(run, preview, prepared.issues, session)
        return run
    except Exception as exc:  # noqa: BLE001 - Laufzeitfehler werden protokolliert, nicht verschluckt
        session.rollback()
        return _record_failed_run(session, account_id, run_kind, exc)


def _run_kind(kind: str | None) -> str:
    """Bildet die Import-Art auf den ``ImportRun.kind``-Wertebereich ab."""
    if kind == "csv":
        return "csv"
    if kind is None or kind == "json" or kind in _OWN_JSON_KINDS:
        return "json"
    raise ValueError(f"Unbekannte Import-Art {kind!r}. Erlaubt: csv, json, own_json, eigenes_json.")


def _prepare_input(
    rows_or_data: object,
    mapping_or_none: Mapping[str, Any] | None,
    kind: str | None,
) -> _PreparedInput:
    """Bereitet die Eingabe zeilenweise tolerant auf.

    Fehlerhafte Zeilen werden als :class:`_RowIssue` gesammelt und aus den
    Vorschaudaten entfernt. Strukturelle Fehler (fehlendes Mapping, ungueltiges
    Dokument) werden als Ausnahme weitergereicht und beenden den Lauf.
    """
    if _is_own_json(rows_or_data, mapping_or_none, kind):
        return _prepare_own_json(rows_or_data)
    return _prepare_mapped_rows(rows_or_data, mapping_or_none, kind)


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


def _prepare_mapped_rows(
    rows_or_data: object,
    mapping_or_none: Mapping[str, Any] | None,
    kind: str | None,
) -> _PreparedInput:
    """Validiert CSV-/fremde JSON-Zeilen einzeln und sammelt Zeilenfehler."""
    if mapping_or_none is None:
        raise ValueError("Fuer CSV oder fremdes JSON ist ein Mapping erforderlich (F-IMP-03).")

    rows = _materialise_rows(rows_or_data)
    base_offset = _row_offset(mapping_or_none)
    origin = "csv" if kind == "csv" else "json"

    good_rows: list[Mapping[str, Any]] = []
    issues: list[_RowIssue] = []

    for index, row in enumerate(rows, start=1):
        row_no = index + base_offset
        if not isinstance(row, Mapping):
            issues.append(
                _RowIssue(
                    row_no=row_no,
                    code=_CODE_ROW_INVALID,
                    message=_short_message(f"Zeile {row_no}: Zeile ist kein Objekt."),
                )
            )
            continue

        normalised_row: Mapping[str, Any] = {str(key): value for key, value in row.items()}
        adjusted_mapping = {**mapping_or_none, "row_offset": row_no - 1}
        try:
            mapped = apply_mapping([normalised_row], adjusted_mapping)
        except MappingError as exc:
            if exc.row_no is None:
                # Struktureller Mapping-Fehler (z. B. unbekanntes Zielfeld):
                # kein Zeilenfehler, sondern ein unbrauchbares Mapping.
                raise
            issues.append(
                _RowIssue(
                    row_no=exc.row_no,
                    code=_CODE_MAPPING,
                    message=_short_message(str(exc)),
                )
            )
            continue

        # Vollstaendig leere Zeilen ueberspringt apply_mapping ohne Daten.
        if mapped:
            good_rows.append(normalised_row)

    return _PreparedInput(
        data=good_rows,
        mapping=mapping_or_none,
        kind=kind,
        origin=origin,
        issues=issues,
    )


def _prepare_own_json(rows_or_data: object) -> _PreparedInput:
    """Validiert das eigene JSON-Dokument und dessen Buchungen einzeln."""
    if not isinstance(rows_or_data, Mapping):
        raise ImportFormatError("JSON-Importdaten muessen ein JSON-Objekt sein.")

    document: dict[str, object] = {str(key): value for key, value in rows_or_data.items()}
    transactions = document.get("transactions")
    if not isinstance(transactions, list):
        raise ImportFormatError("Feld 'transactions' fehlt oder ist keine Liste.")

    # Dokumentkopf (Format, Version, Konten, Filter, Zeitraum) einmal pruefen.
    # Fehler hier sind strukturell und brechen den Lauf ab.
    parse_own_json({**document, "transactions": []})

    good_transactions: list[object] = []
    issues: list[_RowIssue] = []
    for index, transaction in enumerate(transactions, start=1):
        candidate: dict[str, object] = {**document, "transactions": [transaction]}
        try:
            parse_own_json(candidate)
        except (ImportFormatError, ValueError) as exc:
            issues.append(
                _RowIssue(
                    row_no=index,
                    code=_CODE_ROW_INVALID,
                    message=_short_message(f"Zeile {index}: {exc}"),
                )
            )
            continue
        good_transactions.append(transaction)

    return _PreparedInput(
        data={**document, "transactions": good_transactions},
        mapping=None,
        kind="own_json",
        origin="json",
        issues=issues,
    )


def _materialise_rows(value: object) -> list[object]:
    """Materialisiert die Zeilenliste; strukturell falsche Eingaben sind Fehler."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError("Importzeilen muessen eine Liste von Objekten sein.")
    return list(value)


def _row_offset(mapping: Mapping[str, Any]) -> int:
    """Liest den optionalen Zeilenversatz aus dem Mapping (F-IMP-02)."""
    value = mapping.get("row_offset")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _to_duplicate_data(
    parsed: ParsedTransaction,
    fallback_origin: str,
) -> DuplicateTransactionData:
    """Uebertraegt eine Vorschauzeile in die Dubletten-Datenstruktur (F-DUP-02).

    Beim eigenen JSON werden Notiz, Status, Herkunft, externe Referenz und die
    Kennzeichen geprueft/abgestimmt samt Zeitpunkten und Herkunft uebernommen,
    damit neue Buchungen die Dateifelder erhalten (F-IMP-07, F-ABS-01). Beim
    Mapping-Import gilt die Herkunft ``csv`` bzw. ``json``; gemappte Zeilen
    tragen keine Kennzeichen.
    """
    if isinstance(parsed, OwnJsonTransactionData):
        return DuplicateTransactionData(
            booking_date=parsed.booking_date,
            value_date=parsed.value_date,
            amount_cents=parsed.amount_cents,
            currency=parsed.currency,
            counterparty=parsed.counterparty,
            counterparty_iban=parsed.counterparty_iban,
            purpose=parsed.purpose,
            origin=parsed.origin,
            status=parsed.status,
            note=parsed.note,
            external_ref=parsed.external_ref,
            checked=parsed.checked,
            checked_at=parsed.checked_at,
            checked_origin=parsed.checked_origin,
            reconciled=parsed.reconciled,
            reconciled_at=parsed.reconciled_at,
            reconciled_origin=parsed.reconciled_origin,
            search_text=build_search_text(
                counterparty=parsed.counterparty,
                purpose=parsed.purpose,
                note=parsed.note,
                counterparty_iban=parsed.counterparty_iban,
                external_ref=parsed.external_ref,
            ),
        )

    mapped: MappedTransactionData = parsed
    return DuplicateTransactionData(
        booking_date=mapped.booking_date,
        value_date=mapped.value_date,
        amount_cents=mapped.amount_cents,
        currency=mapped.currency,
        counterparty=mapped.counterparty,
        counterparty_iban=mapped.counterparty_iban,
        purpose=mapped.purpose,
        origin=fallback_origin,
        search_text=build_search_text(
            counterparty=mapped.counterparty,
            purpose=mapped.purpose,
            counterparty_iban=mapped.counterparty_iban,
        ),
    )


@dataclass(frozen=True, slots=True)
class _RestoreValues:
    """Dateiwerte fuer die Wiederherstellung vorhandener Buchungen (F-IMP-07).

    Fuehrt auch die Herkunft der Kennzeichen (``checked_origin``/
    ``reconciled_origin``, F-ABS-01), damit ein Re-Import den vollstaendigen
    Stand inklusive der letzten Aenderungsquelle wiederherstellt. Der
    Fingerprint enthaelt diese Felder nicht (F-ABS-06).
    """

    note: str | None
    checked: bool
    checked_at: datetime | None
    checked_origin: str | None
    reconciled: bool
    reconciled_at: datetime | None
    reconciled_origin: str | None
    external_ref: str | None


def _restore_existing_flags(
    account_id: int,
    preview: ImportPreview,
    session: Session,
) -> None:
    """Uebertraegt Dateiwerte auf vorhandene Buchungen (F-IMP-07).

    Zugeordnet wird ueber Fingerprint und Vorkommen: Die Lieferzeilen eines
    Fingerprints werden in Dateireihenfolge den bereits gespeicherten Buchungen
    desselben Kontos in Reihenfolge ihres ``occurrence_index`` zugeordnet. Nur
    Zeilen mit Status ``vorhanden`` treffen den Bestand; neue und
    Verdachtszeilen werden spaeter eingefuegt und erhalten ihre Werte beim
    Anlegen.

    Die Aenderung laeuft in derselben Transaktion wie der uebrige Import.
    ``version_id_col`` (F-CON-01/02) schuetzt vor einem zwischenzeitlich
    geaenderten Stand; ein Konflikt rollt den gesamten Lauf zurueck.
    """
    candidates: list[tuple[str, int, _RestoreValues]] = []
    ordinals: dict[str, int] = {}
    for row in preview.rows:
        ordinal = ordinals.get(row.fingerprint, 0)
        ordinals[row.fingerprint] = ordinal + 1
        if row.status != STATUS_EXISTING:
            continue
        values = _restore_values_from(row.parsed_data)
        if values is not None:
            candidates.append((row.fingerprint, ordinal, values))

    if not candidates:
        return

    fingerprints = {fingerprint for fingerprint, _, _ in candidates}
    stored_rows = session.scalars(
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .order_by(Transaction.fingerprint, Transaction.occurrence_index, Transaction.id)
    ).all()

    by_fingerprint: dict[str, list[Transaction]] = {}
    for transaction in stored_rows:
        by_fingerprint.setdefault(transaction.fingerprint, []).append(transaction)

    for fingerprint, ordinal, values in candidates:
        matches = by_fingerprint.get(fingerprint, [])
        # Defensiv: Weicht der Bestand zwischen Vorschau und Schreibphase ab,
        # wird diese Zeile nicht wiederhergestellt statt falsch zugeordnet.
        if ordinal >= len(matches):
            continue
        _apply_restore_values(matches[ordinal], values)


def _restore_values_from(parsed: ParsedTransaction) -> _RestoreValues | None:
    """Liest die wiederherstellbaren Dateifelder einer Vorschauzeile.

    Nur das eigene JSON-Format traegt Notiz, Kennzeichen und externe Referenz.
    Gemappte CSV-/Fremd-JSON-Zeilen liefern diese Felder nicht und ergeben
    ``None``; ein spaeteres eigenes CSV-Format kann hier ergaenzt werden.
    """
    if isinstance(parsed, OwnJsonTransactionData):
        return _RestoreValues(
            note=parsed.note,
            checked=parsed.checked,
            checked_at=parsed.checked_at,
            checked_origin=parsed.checked_origin,
            reconciled=parsed.reconciled,
            reconciled_at=parsed.reconciled_at,
            reconciled_origin=parsed.reconciled_origin,
            external_ref=parsed.external_ref,
        )
    return None


def _apply_restore_values(transaction: Transaction, values: _RestoreValues) -> None:
    """Setzt abweichende Dateiwerte und laesst gleiche Werte unangetastet.

    Die Schreibvermeidung bei Gleichstand haelt einen wiederholten
    Wiederherstellungs-Import ohne Aenderung von einer unnoetigen
    Versionserhoehung fern. Notiz und externe Referenz sind Teil der
    Suchspalte (F-SUC-01, F-ABS-05); aendert sich eines von beiden, wird
    ``search_text`` neu gebaut.
    """
    search_relevant_changed = False
    if transaction.note != values.note:
        transaction.note = values.note
        search_relevant_changed = True
    if transaction.checked != values.checked:
        transaction.checked = values.checked
    if transaction.checked_at != values.checked_at:
        # UtcDateTime ist ein TypeDecorator; setattr haelt mypy mit der ORM-Deklaration zufrieden.
        setattr(transaction, "checked_at", values.checked_at)
    if transaction.checked_origin != values.checked_origin:
        transaction.checked_origin = values.checked_origin
    if transaction.reconciled != values.reconciled:
        transaction.reconciled = values.reconciled
    if transaction.reconciled_at != values.reconciled_at:
        setattr(transaction, "reconciled_at", values.reconciled_at)
    if transaction.reconciled_origin != values.reconciled_origin:
        transaction.reconciled_origin = values.reconciled_origin
    if transaction.external_ref != values.external_ref:
        transaction.external_ref = values.external_ref
        search_relevant_changed = True
    if search_relevant_changed:
        refresh_search_text(transaction)


def _finish_successful_run(
    run: ImportRun,
    preview: ImportPreview,
    issues: list[_RowIssue],
    session: Session,
) -> None:
    """Setzt Zaehler, Status und Meldung und committed den gesamten Lauf.

    Die Zaehler folgen den Vorschaukategorien (F-IMP-06): ``new_count`` zaehlt
    nur ``neu``, ``suspected_count`` die Verdachtsdubletten. Verdachtsdubletten
    werden nach F-DUP-03 ebenfalls importiert, aber getrennt gezaehlt.
    """
    run.new_count = preview.summary.neu
    run.skipped_count = preview.summary.vorhanden
    run.suspected_count = preview.summary.verdachtsdublette
    run.failed_count = len(issues)
    # UtcDateTime ist ein TypeDecorator; setattr haelt mypy mit der ORM-Deklaration zufrieden.
    setattr(run, "finished_at", utc_now())

    if issues and not preview.rows:
        # Alle Zeilen waren fehlerhaft: der Lauf hat nichts importiert.
        run.status = "fehler"
        run.message = _short_message(
            f"Import fehlerhaft: alle {run.failed_count} Zeile(n) waren fehlerhaft. Details stehen im Fehlerprotokoll."
        )
    elif issues:
        # Mindestens eine Zeile wurde verarbeitet, andere waren fehlerhaft.
        run.status = "teilweise"
        run.message = _success_message(run, partial=True)
    else:
        run.status = "erfolgreich"
        run.message = _success_message(run, partial=False)

    for issue in issues:
        run.errors.append(
            ImportRunError(
                row_no=issue.row_no,
                code=issue.code,
                message=issue.message,
            )
        )

    session.commit()


def _record_failed_run(
    session: Session,
    account_id: int,
    run_kind: str,
    exc: Exception,
) -> ImportRun:
    """Rollt den Lauf zurueck und protokolliert einen fehlerhaften Lauf.

    Der urspruengliche Lauf und alle bereits geschriebenen Buchungen wurden
    durch ``session.rollback()`` entfernt. Der Fehlerlauf ist die einzige
    Persistenz und haelt den Klartext ohne Geheimnisse fest (N-SEC-02).
    """
    message = _short_message(f"Import fehlerhaft: {exc}")
    failed = ImportRun(
        account_id=account_id,
        kind=run_kind,
        status="fehler",
        finished_at=utc_now(),
        failed_count=1,
        message=message,
    )
    failed.errors.append(
        ImportRunError(
            row_no=None,
            code=_CODE_RUN_FAILED,
            message=message,
        )
    )
    session.add(failed)
    session.commit()
    return failed


def _success_message(run: ImportRun, *, partial: bool) -> str:
    """Formuliert den Klartext-Status eines erfolgreichen oder teilweisen Laufs."""
    prefix = "Import teilweise abgeschlossen" if partial else "Import abgeschlossen"
    message = (
        f"{prefix}: {run.new_count} neu, {run.skipped_count} bereits vorhanden, "
        f"{run.suspected_count} Verdachtsdublette(n), {run.failed_count} fehlerhafte Zeile(n)."
    )
    if partial:
        message += " Fehlerhafte Zeilen wurden protokolliert, die uebrigen Zeilen importiert."
    return _short_message(message)


def _short_message(text: str, limit: int = _MESSAGE_LIMIT) -> str:
    """Kuerzt eine Klartextmeldung auf die Protokollgroesse."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."
