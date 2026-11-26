"""Persistenz eines FinTS-Syncs je Konto in genau einer Transaktion.

Der Service setzt die Persistenzstufe des lesenden Syncs um (F-SYN-03,
F-SYN-07, F-SYN-10, F-CON-05) und verbindet die vorhandenen Bausteine:

- :func:`finue.services.duplicate.reconcile_multiset` legt ausschliesslich die
  noch nicht vorhandenen gebuchten Buchungen an (F-DUP-02).
- :func:`finue.services.duplicate.find_suspected_duplicates` erkennt
  Verdachtsdubletten gegen den gebuchten Bestand (F-DUP-03). Sie werden wie
  beim Datei-Import importiert, aber separat gezaehlt.
- :func:`finue.services.pending_sync.replace_pending` ersetzt die
  vorgemerkten Buchungen komplett (F-DUP-04). Das geschieht bewusst vor dem
  Multimengen-Abgleich, damit alte Vormerkungen nicht mitzaehlen. Der Abgleich
  schliesst zusaetzlich die in diesem Lauf neu angelegten Vormerkungen aus dem
  Bestand aus, damit eine gebuchte Zeile mit gleichem Fingerprint nicht
  uebersprungen wird.
- :func:`finue.services.pending_sync.transfer_flags` uebertraegt Notiz und
  Pruef-Flag einer ersetzten Vormerkung auf die genau eine passende, in diesem
  Lauf neu gebuchte Buchung; mehrdeutige Faelle landen als ``VORMERKUNG``-Zeile
  im Protokoll (F-DUP-04).

Abbildung der Anforderungen:

- F-SYN-03: Saldo (``Account.balance_cents``, ``Account.balance_at``),
  gebuchte und vorgemerkte Umsaetze werden uebernommen.
  :func:`compute_fetch_range` liefert den Abrufzeitraum ab dem letzten
  erfolgreichen Sync minus :data:`FETCH_OVERLAP_DAYS` Tage; beim ersten Sync
  ``None`` (so weit die Bank liefert).
- F-SYN-07: Jeder Aufruf schreibt genau einen :class:`ImportRun` mit
  ``kind='sync'``, ``job_id``, Zaehlern (neu/unveraendert/Verdacht/Fehler),
  Status, Beginn und Ende sowie Klartext. Die abschliessenden Statuswerte
  ``erfolgreich``/``fehler``/``abgebrochen``/``zeitueberschreitung`` werden
  unterstuetzt; ``teilweise`` entsteht auf Batch-Ebene in der Orchestrierung.
- F-SYN-10: Die Schreibvorgaenge eines Kontos laufen in genau einem Commit.
  Tritt vor dem Commit eine Exception auf, wird zurueckgerollt: von diesem
  Konto bleibt nichts gespeichert, bereits abgeschlossene Konten bleiben
  erhalten. :func:`persist_sync_failure` speichert nur die Protokollzeile des
  fehlgeschlagenen Kontos, nie Kontodaten.
- F-CON-05: Bestehende Buchungen werden nur gelesen. Ein Sync aendert nie
  Notiz, geprueft, abgestimmt oder externe Referenz; die einzige Ausnahme ist
  die Uebertragung von einer ersetzten Vormerkung auf eine in diesem Lauf neu
  gebuchte Buchung (F-DUP-04).
- F-SYN-05: :func:`persist_sync_failure` loest den Bankcode ueber das
  Fehler-Mapping in Klartext und Handlungsempfehlung auf.
- N-SEC-02: Meldungen enthalten nur Zaehler, Status und Klartext aus dem
  Fehler-Mapping. PIN, TAN, Schluessel, Tokens und Dialogdaten werden weder
  gespeichert noch ausgegeben. Der Service kennt keine Dialogdaten.
- N-DB-01: Ausschliesslich SQLAlchemy-Ausdruecke, kein rohes SQL.

Es finden keine Bankzugriffe statt. Der Aufrufer uebergibt die bereits lesend
abgerufenen Daten als :class:`~finue.services.fints_types.SyncAccountData`.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Final, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.log_filter import mask_text
from finue.models.account import Account
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.transaction import Transaction
from finue.services.bank_errors import resolve_error
from finue.services.duplicate import (
    TransactionData,
    compute_fingerprint,
    find_suspected_duplicates,
    reconcile_multiset,
)
from finue.services.fints_types import (
    BalanceResult,
    FinTSBankError,
    FetchedTransaction,
    SyncAccountData,
    SyncCancelledError,
    TanTimeoutError,
)
from finue.services.pending_sync import replace_pending, transfer_flags
from finue.timeutil import utc_now

__all__ = [
    "FETCH_OVERLAP_DAYS",
    "FailureStatus",
    "compute_fetch_range",
    "persist_account_sync",
    "persist_sync_failure",
    "persist_waiting_for_tan",
]

#: F-SYN-03: Ueberlappung des Abrufzeitraums ab dem letzten erfolgreichen Sync.
FETCH_OVERLAP_DAYS: Final[int] = 14

#: Abschliessende Fehlerstatus eines Kontos nach F-SYN-07. ``erfolgreich``
#: schreibt :func:`persist_account_sync`, ``teilweise`` die Batch-Orchestrierung.
FailureStatus = Literal["fehler", "abgebrochen", "zeitueberschreitung"]

#: Maximale Laenge einer Klartextmeldung im Protokoll.
_MESSAGE_LIMIT: Final[int] = 500

#: Fehlercodes der Protokollzeilen bei Abbruch, Timeout und unbekanntem Fehler.
_CODE_CANCELLED: Final[str] = "ABGEBROCHEN"
_CODE_TIMEOUT: Final[str] = "ZEITUEBERSCHREITUNG"
_CODE_UNKNOWN: Final[str] = "SYNC_FEHLER"
_CODE_MAX_LENGTH: Final[int] = 20

_BOOKED: Final[str] = "gebucht"
_PENDING: Final[str] = "vorgemerkt"


def persist_account_sync(
    session: Session,
    account_id: int,
    data: SyncAccountData,
    job_id: str | None,
    *,
    waiting_run: ImportRun | None = None,
) -> ImportRun:
    """Uebernimmt die Daten eines Kontos in genau einer Transaktion (F-SYN-10).

    Der Aufruf schreibt Saldo, gebuchte und vorgemerkte Umsaetze und genau
    einen :class:`ImportRun` mit ``kind='sync'`` und ``status='erfolgreich'``.
    Alle Schreibvorgaenge werden in einem Commit gespeichert. Tritt vorher
    eine Exception auf, wird die Transaktion zurueckgerollt: von diesem Konto
    bleibt nichts gespeichert, bereits abgeschlossene Konten bleiben erhalten.

    Die Reihenfolge folgt der Vorgabe aus :mod:`finue.services.pending_sync`:
    Die Vormerkungen werden ersetzt, bevor der Multimengen-Abgleich laeuft,
    damit alte Vormerkungen nicht mitzaehlen (F-DUP-04). Die neu angelegten
    Vormerkungen zaehlen im Abgleich ebenfalls nicht als Bestand, damit eine
    gebuchte Zeile mit gleichem Fingerprint angelegt wird. Anschliessend werden
    die gebuchten Umsaetze abgeglichen (F-DUP-02), Verdachtsdubletten
    gezaehlt (F-DUP-03) und Notiz/Pruef-Flag ersetzter Vormerkungen auf die
    neuen Buchungen uebertragen.

    Args:
        session: SQLAlchemy-Session. Der Service committet selbst und darf
            keine fremden, noch nicht gespeicherten Aenderungen enthalten.
        account_id: Konto, dem die Daten zugeordnet werden.
        data: Lesend abgerufene Daten des Kontos. Auftraege werden hier noch
            nicht gespeichert (T-079).
        job_id: Gemeinsame Kennung eines Batch-Laufs (F-SYN-07); ``None`` beim
            Einzel-Sync.
        waiting_run: Optionaler, bereits gespeicherter ImportRun mit Status
            ``wartet_auf_tan``. Er wird nach der TAN-Freigabe fuer denselben
            Lauf weiterverwendet, damit je Konto genau eine Protokollzeile
            entsteht (F-SYN-04, F-SYN-07).

    Returns:
        Der persistierte :class:`ImportRun` mit Status ``erfolgreich``.

    Raises:
        LookupError: Wenn das Konto nicht existiert.
        Exception: Jeder Fehler beim Schreiben wird nach dem Rollback
            unveraendert weitergereicht.
    """
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Konto {account_id} nicht gefunden.")

    booked = [_to_transaction_data(row) for row in data.transactions if row.status == _BOOKED]
    pending = [_to_transaction_data(row) for row in data.transactions if row.status == _PENDING]

    run = _start_run(session, account_id, job_id, waiting_run)

    try:
        _apply_balance(account, data.balance)
        # F-DUP-04: Vormerkungen zuerst ersetzen. Die in diesem Lauf neu
        # angelegten Vormerkungen zaehlen anschliessend nicht als Bestand des
        # gebuchten Multimengen-Abgleichs; sonst wuerde eine gebuchte Zeile mit
        # gleichem Fingerprint uebersprungen.
        replace_pending(session, account_id, pending)
        suspected_count = _count_suspected_new_booked(account_id, booked, session)
        reconciled = reconcile_multiset(
            account_id,
            booked,
            session,
            include_pending_in_existing=False,
        )
        transfer_flags(session, account_id, reconciled.to_insert, run=run)

        run.new_count = reconciled.inserted_count
        run.skipped_count = reconciled.skipped_count
        run.suspected_count = suspected_count
        run.failed_count = 0
        run.status = "erfolgreich"
        # UtcDateTime ist ein TypeDecorator; setattr haelt mypy mit der
        # ORM-Deklaration zufrieden (gleiches Muster wie import_run.py).
        setattr(run, "finished_at", utc_now())
        run.message = _success_message(run)
        session.commit()
        return run
    except Exception:
        session.rollback()
        raise


def persist_sync_failure(
    session: Session,
    account_id: int,
    job_id: str | None,
    error: Exception,
    *,
    blz: str | None = None,
    waiting_run: ImportRun | None = None,
) -> ImportRun:
    """Protokolliert einen fehlgeschlagenen Kontolauf ohne Kontodaten (F-SYN-07).

    Es wird ausschliessich die Protokollzeile gespeichert. Buchungen,
    Vormerkungen und Saldo des Kontos bleiben unveraendert (F-SYN-10). Der
    Status folgt dem Fehlertyp:

    - :class:`~finue.services.fints_types.FinTSBankError` (inklusive falscher
      PIN und gesperrtem Zugang) -> ``fehler``,
    - :class:`~finue.services.fints_types.SyncCancelledError` ->
      ``abgebrochen``,
    - :class:`~finue.services.fints_types.TanTimeoutError` ->
      ``zeitueberschreitung``,
    - jede andere Exception -> ``fehler`` mit festem Klartext.

    Bei Bankfehlern wird der Code ueber :func:`finue.services.bank_errors.resolve_error`
    in Klartext und Handlungsempfehlung aufgeloest (F-SYN-05). Unbekannte
    Exceptions geben nur den Typnamen aus, nie die Meldung, damit keine
    Geheimnisse aus Fehlertexten in das Protokoll gelangen (N-SEC-02).

    Args:
        session: SQLAlchemy-Session. Der Service committet selbst.
        account_id: Betroffenes Konto.
        job_id: Gemeinsame Kennung eines Batch-Laufs; ``None`` beim Einzel-Sync.
        error: Aufgetretener Fehler. Es werden nur bekannte, geheimnisfreie
            Attribute ausgewertet.
        blz: Optionale Bankleitzahl fuer das Fehler-Mapping. Ohne Angabe wird
            sie aus dem Bankzugang des Kontos gelesen.
        waiting_run: Optionaler, bereits gespeicherter ImportRun mit Status
            ``wartet_auf_tan``. Er wird fuer denselben Lauf aktualisiert,
            damit je Konto genau eine Protokollzeile entsteht (F-SYN-07).

    Returns:
        Der persistierte :class:`ImportRun` mit Fehlerstatus.

    Raises:
        LookupError: Wenn das Konto nicht existiert.
    """
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Konto {account_id} nicht gefunden.")

    resolved_blz = blz if blz is not None else _account_blz(account)
    status, code, message = _failure_outcome(session, error, resolved_blz)

    if waiting_run is not None:
        run = _reusable_run(session, waiting_run, account_id)
        run.status = status
        setattr(run, "finished_at", utc_now())
        run.failed_count = 1
        run.message = message
        run.errors.append(ImportRunError(row_no=None, code=code, message=message))
        session.add(run)
        session.commit()
        return run

    run = ImportRun(
        account_id=account_id,
        job_id=job_id,
        kind="sync",
        status=status,
        # UtcDateTime ist ein TypeDecorator; der Konstruktor akzeptiert das
        # aware datetime wie in finue.services.import_run.
        finished_at=utc_now(),
        failed_count=1,
        message=message,
    )
    run.errors.append(ImportRunError(row_no=None, code=code, message=message))
    session.add(run)
    session.commit()
    return run


def persist_waiting_for_tan(
    session: Session,
    account_id: int,
    job_id: str | None,
) -> ImportRun:
    """Legt den Protokolllauf eines wartenden TAN-Dialogs an (F-SYN-04/07).

    Je Konto und Lauf existiert hoechstens eine Zeile im Status
    ``wartet_auf_tan``. Existiert bereits eine offene Zeile, wird sie
    aktualisiert. Die Zeile enthaelt nur festen Klartext und keine TAN- oder
    Dialogdaten (N-SEC-02/03).

    Args:
        session: SQLAlchemy-Session. Die Funktion committet selbst.
        account_id: Konto, dessen Abruf auf die Freigabe wartet.
        job_id: Gemeinsame Kennung eines Batch-Laufs; ``None`` beim Einzel-Sync.

    Returns:
        Der gespeicherte :class:`ImportRun` mit Status ``wartet_auf_tan``.
    """

    statement = select(ImportRun).where(
        ImportRun.account_id == account_id,
        ImportRun.kind == "sync",
        ImportRun.status.in_(("laeuft", "wartet_auf_tan")),
    )
    if job_id is None:
        statement = statement.where(ImportRun.job_id.is_(None))
    else:
        statement = statement.where(ImportRun.job_id == job_id)
    run = session.scalars(statement.order_by(ImportRun.id.desc()).limit(1)).first()
    message = "Sync wartet auf TAN-Freigabe."
    if run is None:
        run = ImportRun(
            account_id=account_id,
            job_id=job_id,
            kind="sync",
            status="wartet_auf_tan",
            message=message,
        )
    else:
        run.status = "wartet_auf_tan"
        run.message = message
        setattr(run, "finished_at", None)
    session.add(run)
    session.commit()
    return run


def compute_fetch_range(
    session: Session,
    account_id: int,
    *,
    today: date | None = None,
) -> tuple[date, date] | None:
    """Liefert den Abrufzeitraum des naechsten Syncs (F-SYN-03).

    Der Beginn liegt :data:`FETCH_OVERLAP_DAYS` Tage vor dem Ende des letzten
    erfolgreichen Syncs dieses Kontos. Beim ersten Sync gibt es keinen
    erfolgreichen Lauf; dann wird ``None`` geliefert, damit die Bank so weit
    liefert, wie sie kann.

    Nur Laeufe mit ``kind='sync'`` und ``status='erfolgreich'`` zaehlen.
    Fehlgeschlagene, abgebrochene und Datei-Import-Laeufe werden ignoriert.

    Args:
        session: SQLAlchemy-Session (nur lesend).
        account_id: Konto, dessen letzter erfolgreicher Sync gesucht wird.
        today: Optionales Enddatum fuer Tests. Standard ist das aktuelle
            UTC-Datum.

    Returns:
        ``(start, end)`` als Datumstupel oder ``None`` beim ersten Sync.
    """
    latest = session.scalars(
        select(ImportRun)
        .where(
            ImportRun.account_id == account_id,
            ImportRun.kind == "sync",
            ImportRun.status == "erfolgreich",
        )
        .order_by(ImportRun.finished_at.desc(), ImportRun.started_at.desc())
        .limit(1)
    ).all()
    if not latest:
        return None

    last_run = latest[0]
    last_finished = cast("datetime | None", last_run.finished_at)
    last_date = last_finished.date() if last_finished is not None else last_run.started_at.date()
    end = today if today is not None else utc_now().date()
    return (last_date - timedelta(days=FETCH_OVERLAP_DAYS), end)


def _start_run(
    session: Session,
    account_id: int,
    job_id: str | None,
    waiting_run: ImportRun | None,
) -> ImportRun:
    """Legt den Lauf an oder setzt einen wartenden Lauf auf ``laeuft``."""

    if waiting_run is not None:
        run = _reusable_run(session, waiting_run, account_id)
        run.status = "laeuft"
        run.message = "Sync laeuft."
        session.add(run)
        session.flush()
        return run

    run = ImportRun(
        account_id=account_id,
        job_id=job_id,
        kind="sync",
        status="laeuft",
        message="Sync laeuft.",
    )
    session.add(run)
    session.flush()
    return run


def _reusable_run(session: Session, run: ImportRun, account_id: int) -> ImportRun:
    """Prueft einen wiederzuverwendenden ImportRun aus derselben Session."""

    stored = session.get(ImportRun, run.id)
    if stored is None or stored.account_id != account_id:
        raise ValueError("Wartender ImportRun gehoert nicht zum Konto.")
    if stored.status not in {"laeuft", "wartet_auf_tan"}:
        raise ValueError("ImportRun ist nicht mehr offen.")
    return stored


def _to_transaction_data(row: FetchedTransaction) -> TransactionData:
    """Ueberfuehrt eine abgerufene Buchung in die Persistenz-Datenstruktur."""
    return TransactionData(
        booking_date=row.booking_date,
        amount_cents=row.amount_cents,
        currency=row.currency,
        origin="fints",
        value_date=row.value_date,
        counterparty=row.counterparty,
        counterparty_iban=row.counterparty_iban,
        purpose=row.purpose,
        status=row.status,
        external_ref=row.raw_reference,
    )


def _apply_balance(account: Account, balance: BalanceResult | None) -> None:
    """Schreibt den Saldo des Kontos, ohne einen fehlenden Saldo zu loeschen.

    ``balance_at`` haelt den Zeitpunkt der Uebernahme in UTC fest; die Bank
    liefert hoechstens ein Datum (``BalanceResult.as_of``), das im Modell keine
    eigene Spalte hat. Ein Sync ohne Saldo laesst den bisherigen Stand stehen.
    """
    if balance is None:
        return
    account.balance_cents = balance.amount_cents
    # UtcDateTime ist ein TypeDecorator; setattr haelt mypy zufrieden.
    setattr(account, "balance_at", utc_now())


def _count_suspected_new_booked(
    account_id: int,
    booked: list[TransactionData],
    session: Session,
) -> int:
    """Zaehlt Verdachtsdubletten unter den neu einzufuegenden Buchungen.

    Die Reihenfolge folgt der Import-Vorschau: Eine durch den Bestand
    abgedeckte Zeile (F-DUP-02) ist ``unveraendert`` und wird nicht als
    Verdacht gezaehlt, auch wenn sie zusaetzlich einem Bestandseintrag
    aehnelt. Nur neu einzufuegende Zeilen koennen eine Verdachtsdublette sein
    (F-DUP-03).
    """
    if not booked:
        return 0

    new_positions = _new_booked_positions(account_id, booked, session)
    if not new_positions:
        return 0

    pairs = find_suspected_duplicates(account_id, booked, session)
    position_by_identity = {id(row): position for position, row in enumerate(booked)}
    suspected_positions = {
        position_by_identity[id(pair.new_transaction)]
        for pair in pairs
        # Vormerkungen nehmen am Verdachtsabgleich nicht teil (F-DUP-04).
        if pair.existing_transaction.status == _BOOKED and id(pair.new_transaction) in position_by_identity
    }
    return len(suspected_positions & new_positions)


def _new_booked_positions(
    account_id: int,
    booked: list[TransactionData],
    session: Session,
) -> set[int]:
    """Bestimmt die Positionen, die der Multimengen-Abgleich neu einfuegt.

    Die Rechnung entspricht :func:`finue.services.duplicate.reconcile_multiset`
    im Sync-Pfad: Vorgemerkte Buchungen zaehlen nicht als Bestand (F-DUP-04).
    Die ersten ``n_db`` Vorkommen je Fingerprint sind durch die gebuchten
    Buchungen des Kontos abgedeckt, damit die Zaehlung exakt zum spaeteren
    Abgleich passt.
    """
    fingerprints = [_fingerprint_for(account_id, row) for row in booked]
    existing_counts = _load_existing_counts(account_id, set(fingerprints), session)

    processed: Counter[str] = Counter()
    new_positions: set[int] = set()
    for position, fingerprint in enumerate(fingerprints):
        ordinal = processed[fingerprint]
        processed[fingerprint] += 1
        if ordinal >= existing_counts.get(fingerprint, 0):
            new_positions.add(position)
    return new_positions


def _load_existing_counts(
    account_id: int,
    fingerprints: set[str],
    session: Session,
) -> dict[str, int]:
    """Liest die Anzahl gespeicherter gebuchter Buchungen je Fingerprint.

    Vorgemerkte Buchungen zaehlen nicht mit (F-DUP-04); nur so entspricht die
    Zaehlung dem Multimengen-Abgleich des Syncs, der
    ``include_pending_in_existing=False`` setzt.
    """
    if not fingerprints:
        return {}

    rows = session.execute(
        select(Transaction.fingerprint, Transaction.id).where(
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
            Transaction.status == _BOOKED,
        )
    ).all()
    counts: Counter[str] = Counter()
    for fingerprint, _transaction_id in rows:
        counts[fingerprint] += 1
    return dict(counts)


def _fingerprint_for(account_id: int, row: TransactionData) -> str:
    """Berechnet den F-DUP-01-Fingerprint einer eingehenden Buchung."""
    return compute_fingerprint(
        account_id=account_id,
        booking_date=row.booking_date,
        value_date=row.value_date,
        amount_cents=row.amount_cents,
        currency=row.currency,
        counterparty=row.counterparty,
        counterparty_iban=row.counterparty_iban,
        purpose=row.purpose,
    )


def _account_blz(account: Account) -> str | None:
    """Liest die Bankleitzahl des Kontos fuer das Fehler-Mapping."""
    access = account.bank_access
    if access is None:
        return None
    return access.bank.blz


def _failure_outcome(
    session: Session,
    error: Exception,
    blz: str | None,
) -> tuple[FailureStatus, str, str]:
    """Bildet einen Fehler auf Status, Protokollcode und Klartext ab.

    Nur die bekannten, geheimnisfreien Attribute der Domain-Fehler werden
    ausgewertet. Bei einem Bankfehler liefert
    :func:`finue.services.bank_errors.resolve_error` den Klartext und die
    Handlungsempfehlung (F-SYN-05).
    """
    if isinstance(error, TanTimeoutError):
        return (
            "zeitueberschreitung",
            _CODE_TIMEOUT,
            _short_message("Sync-Zeitueberschreitung: Die TAN-Freigabe ist nicht rechtzeitig erfolgt."),
        )
    if isinstance(error, SyncCancelledError):
        return (
            "abgebrochen",
            _CODE_CANCELLED,
            _short_message("Sync abgebrochen: Der Sync wurde durch den Nutzer abgebrochen."),
        )
    if isinstance(error, FinTSBankError):
        resolved = resolve_error(session, error.code, blz, error.original_text)
        # Auch eigene Mapping-Texte koennen Eingaben spiegeln; vor der
        # Protokollierung laeuft der Klartext durch den Geheimnis-Filter
        # (N-SEC-02).
        message = _short_message(mask_text(f"{resolved.message} {resolved.recommendation}"))
        return ("fehler", _checked_code(error.code), message)

    # Unbekannte Exceptions: Der Typname ist hilfreich, die Meldung koennte
    # Geheimnisse enthalten und wird deshalb nicht uebernommen (N-SEC-02).
    return (
        "fehler",
        _CODE_UNKNOWN,
        _short_message(f"Sync fehlgeschlagen ({type(error).__name__})."),
    )


def _checked_code(code: str) -> str:
    """Kuerzt einen Protokollcode auf die Spaltenbreite von 20 Zeichen."""
    return code[:_CODE_MAX_LENGTH]


def _success_message(run: ImportRun) -> str:
    """Formuliert den geheimnisfreien Klartext eines erfolgreichen Laufs."""
    return _short_message(
        f"Sync abgeschlossen: {run.new_count} neu, {run.skipped_count} unveraendert, "
        f"{run.suspected_count} Verdachtsdublette(n), {run.failed_count} Fehler."
    )


def _short_message(text: str, limit: int = _MESSAGE_LIMIT) -> str:
    """Kuerzt eine Klartextmeldung auf die Protokollgroesse."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."
