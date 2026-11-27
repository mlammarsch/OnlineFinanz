#!/usr/bin/env python3
"""Erzeugt und vermisst den Performance-Datenbestand mit 100.000 Buchungen.

Referenziert **N-PERF-01** (Suche im Standardzeitraum unter 500 ms bei 100.000
Buchungen, SQLite, Entwicklungsrechner) und **F-BUC-06** (fluessige Buchungsliste
auch bei 100.000 Buchungen). Alles ist rein erfunden (D8): feste Namensliste,
oeffentlich dokumentierte IBAN-Testnummern und ein fester Zufallsseed. Es werden
**keine** echten Kontodaten, Zugangsdaten oder Bankzugriffe verwendet.

Der Generator schreibt ausschliesslich ueber SQLAlchemy-Core-``insert`` in eine
temporaere SQLite-Datei (N-DB-01, kein rohes Daten-SQL). Das Schema kommt aus der
echten Alembic-Migrationskette (N-DB-03), damit gemessene Indizes dem
Produktionsschema entsprechen. Die normalisierte Suchspalte ``search_text`` wird
mit derselben Regel wie im Betrieb gefuellt
(:func:`finue.services.search_text.build_search_text`, F-SUC-06).

Das Modul ist zugleich Bibliothek (fuer ``tests/perf``, pytest-Marker ``perf``)
und CLI::

    uv run python scripts/perf_data.py --db data/perf.db --count 100000
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

# Der Projektcode liegt in backend/; beim direkten CLI-Aufruf fehlt es im Pfad.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import insert  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

import finue.models  # noqa: E402,F401  # registriert ALLE Tabellen an Base.metadata
from finue.db.engine import create_db_engine, make_session_factory  # noqa: E402
from finue.db.migrate import upgrade_to_head  # noqa: E402
from finue.models.account import Account, AccountGroup  # noqa: E402
from finue.models.transaction import Transaction  # noqa: E402
from finue.services.duplicate import compute_fingerprint  # noqa: E402
from finue.services.search_text import build_search_text  # noqa: E402

# ------------------------------------------------------------------- Kennzahlen

#: Fester Seed: der Bestand ist damit bit-genau reproduzierbar.
DEFAULT_SEED: Final = 20241126

#: Zielzahl der Buchungen (DoD N-PERF-01, F-BUC-06).
DEFAULT_COUNT: Final = 100_000

#: Anzahl Konten (DoD: 5 Konten).
ACCOUNT_COUNT: Final = 5

#: Zeitraum der Buchungstage in Jahren (DoD: 3 Jahre).
YEARS: Final = 3

#: Bezugstag (fix): Enddatum der Datumsverteilung und Basis der Zeitraeume.
REFERENCE_DAY: Final = date(2026, 12, 31)

#: Fester Zeitstempel fuer ``created_at``/``updated_at`` (Reproduzierbarkeit).
FIXED_TIMESTAMP: Final = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)

#: Zeitbudget je Messung in Millisekunden (N-PERF-01, F-BUC-06).
BUDGET_MS: Final = 500.0

#: Aufwaermlaeufe vor der Messung und Zahl der Messlaeufe (DoD).
WARMUP_RUNS: Final = 2
MEASURE_RUNS: Final = 5

#: Anteil der Buchungen mit Notiz (DoD: etwa 5 Prozent).
NOTE_RATE: Final = 0.05

#: Anteil der vorgemerkten Buchungen (F-BUC-02: Vormerkungsbereich).
PENDING_RATE: Final = 0.05

#: Betrag der reservierten "Nadel" (F-SUC-02: Betragssuche), etwa 2 Prozent der
#: Zeilen, damit eine Betragssuche im Standardzeitraum verlaesslich trifft.
AMOUNT_NEEDLE_CENTS: Final = -4883

#: Umgebungsschluessel fuer den Messbericht (von scripts/validate.py gelesen).
REPORT_ENV: Final = "FINUE_PERF_REPORT"

# Suchbegriffe, die die Perf-Tests verwenden (F-SUC-01, F-SUC-02).
SEARCH_ONE_TERM: Final = "einkauf"
SEARCH_TWO_TERMS: Final = "stadtwerke abschlag"
AMOUNT_SEARCH: Final = "-48,83"

# ------------------------------------------------------------ Fiktive Stammdaten

_ACCOUNT_NAMES: Final = (
    "Perf Girokonto",
    "Perf Tagesgeld",
    "Perf Kreditkarte",
    "Perf Depot-Verrechnung",
    "Perf Datei-Import",
)

# Oeffentlich dokumentierte IBAN-Testnummern (keine echten Konten).
_IBANS: Final = (
    "DE02120300000000202051",
    "DE89370400440532013000",
    "DE02200505501015871393",
    "DE75512108001245126199",
    "DE91100000000123456789",
)

_GROUP_NAME: Final = "Perf-Gruppe"

# Realistische (Gegenpartei, Verwendungszweck)-Vorlagen. Die Paare "Stadtwerke
# Musterstadt"/"Stromabschlag ... Abschlag" und die Einkaufszwecke liefern die
# Begriffe fuer die Ein- und Zwei-Begriff-Suche.
_TEMPLATES: Final[tuple[tuple[str, str], ...]] = (
    ("Max Mustermann", "Lebensmitteleinkauf Filiale Musterstadt"),
    ("Erika Musterfrau", "Gehalt Monat November Ueberweisung"),
    ("Stadtwerke Musterstadt", "Stromabschlag Musterstadt Abschlag"),
    ("Musterfirma GmbH", "Rechnung Buerobedarf Nummer 4711"),
    ("Beispiel Handel AG", "Onlineshop Bestellung Einkauf Elektronik"),
    ("Test-Energie GmbH", "Jahresabrechnung Strom Nachzahlung"),
    ("Fiktiv Versicherung AG", "Versicherungsbeitrag Hausrat jaehrlich"),
    ("Demo Telekom GmbH", "Mobilfunk Grundgebuehr und Verbindungen"),
    ("Muster Shop", "Onlineshop Bestellung Einkauf Haushaltswaren"),
    ("Beispiel Bahn AG", "Bahnticket Fernverkehr Sitzplatzreservierung"),
    ("Musterbank Musterstadt", "Kontofuehrungsgebuehr Quartal"),
    ("Fiktiv Baeckerei GmbH", "Lebensmitteleinkauf Backwaren Filiale"),
    ("Beispiel Drogerie GmbH", "Einkauf Drogerie Kosmetik"),
    ("Muster Tankstelle", "Tanken Kraftstoff Super E10"),
    ("Fiktiv Fitness GmbH", "Mitgliedsbeitrag Fitnessstudio"),
    ("Beispiel Restaurant GmbH", "Gaststaettenbesuch Bewirtung"),
    ("Muster Apotheke", "Apotheke Medikamente Rezept"),
    ("Fiktiv Baumarkt GmbH", "Einkauf Baumarkt Gartenbedarf"),
    ("Beispiel Verlag GmbH", "Abonnement Fachzeitschrift"),
    ("Muster Streaming GmbH", "Streaming-Abo Monat"),
    ("Fiktiv Lohnbuero", "Gehalt und Zulagen Monat"),
    ("Beispiel Steuerberater AG", "Honorar Steuererklaerung"),
    ("Muster Immobilien GmbH", "Miete Wohnung Nebenkosten"),
    ("Fiktiv Fotostudio", "Fotos Portraet Serie"),
)

_NOTES: Final = (
    "Beleg abgelegt",
    "Rueckfrage offen",
    "mit Beleg",
    "geprueft am Monatsende",
    "Bitte Kategorie pruefen",
    "nach der Ruhestandsfeier",
)


# ------------------------------------------------------------------- Datenklassen


@dataclass(frozen=True, slots=True)
class PerfInfo:
    """Kennzahlen und Schluessel des erzeugten Performance-Bestands."""

    db_url: str
    transaction_count: int
    account_ids: tuple[int, ...]
    group_id: int
    reference_day: date

    @property
    def first_account_id(self) -> int:
        """Erstes Konto (Basis der Kontoliste-Messung)."""
        return self.account_ids[0]

    def as_dict(self) -> dict[str, Any]:
        """Kompakte, serialisierbare Kennzahlen (fuer Berichte)."""
        return {
            "db_url": self.db_url,
            "transaction_count": self.transaction_count,
            "account_ids": list(self.account_ids),
            "group_id": self.group_id,
            "reference_day": self.reference_day.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Measurement:
    """Ergebnis einer Zeitmessung: Median und Maximum in Millisekunden."""

    name: str
    median_ms: float
    max_ms: float
    runs: int

    def within_budget(self, budget_ms: float = BUDGET_MS) -> bool:
        """Prueft das Zeitbudget gegen den Median."""
        return self.median_ms < budget_ms


# ------------------------------------------------------------------- Generator


def _iter_rows(
    account_ids: tuple[int, ...],
    *,
    seed: int,
    count: int,
    reference_day: date,
) -> Iterator[dict[str, Any]]:
    """Erzeugt ``count`` reproduzierbare Buchungszeilen als Core-Insert-Werte.

    Fester Seed -> identische Reihenfolge und Werte bei jedem Lauf. Textlaengen
    entsprechen realistischen Umsaetzen; etwa 5 Prozent tragen eine Notiz, etwa
    5 Prozent sind vorgemerkt, rund 2 Prozent tragen den reservierten Betrag.
    """

    import random

    rng = random.Random(seed)
    span_days = YEARS * 365
    account_index_count = len(account_ids)
    # Vorkommens-Index je (Konto, Fingerprint) als Multiset (F-DUP-02): gleiche
    # Fingerprints koennen vorkommen und werden ueber den Index eindeutig.
    # 0-basiert wie die Produktionslogik (services/duplicate.py, demo.py).
    counters: dict[tuple[int, str], int] = {}

    for index in range(count):
        account_id = account_ids[index % account_index_count]
        booking_date = reference_day - timedelta(days=rng.randrange(0, span_days))
        value_date = booking_date + timedelta(days=rng.randrange(0, 3))

        if index % 50 == 0:
            amount_cents = AMOUNT_NEEDLE_CENTS
        else:
            amount_cents = rng.randint(-250_000, 320_000)

        counterparty, purpose = rng.choice(_TEMPLATES)
        if rng.random() < 0.25:
            counterparty = None
        counterparty_iban = _IBANS[index % len(_IBANS)] if rng.random() < 0.7 else None

        note = _NOTES[rng.randrange(len(_NOTES))] if rng.random() < NOTE_RATE else None
        external_ref = f"perf-{index:07d}"
        search_text = build_search_text(
            counterparty=counterparty,
            purpose=purpose,
            note=note,
            counterparty_iban=counterparty_iban,
            external_ref=external_ref,
        )
        fingerprint = compute_fingerprint(
            account_id,
            booking_date,
            value_date,
            amount_cents,
            "EUR",
            counterparty,
            counterparty_iban,
            purpose,
        )
        counter_key = (account_id, fingerprint)
        occurrence_index = counters.get(counter_key, 0)
        counters[counter_key] = occurrence_index + 1
        checked = index % 7 == 0
        reconciled = index % 11 == 0
        status = "vorgemerkt" if index % 20 == 0 else "gebucht"

        yield {
            "account_id": account_id,
            "status": status,
            "booking_date": booking_date,
            "value_date": value_date,
            "amount_cents": amount_cents,
            "currency": "EUR",
            "counterparty": counterparty,
            "counterparty_iban": counterparty_iban,
            "purpose": purpose,
            "search_text": search_text,
            "note": note,
            "checked": checked,
            "checked_at": FIXED_TIMESTAMP if checked else None,
            "checked_origin": "import" if checked else None,
            "reconciled": reconciled,
            "reconciled_at": FIXED_TIMESTAMP if reconciled else None,
            "reconciled_origin": "import" if reconciled else None,
            "origin": "csv",
            "external_ref": external_ref,
            "fingerprint": fingerprint,
            "occurrence_index": occurrence_index,
            "merged": False,
            "version": 1,
            "created_at": FIXED_TIMESTAMP,
            "updated_at": FIXED_TIMESTAMP,
        }


def _chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Teilt die Zeilen in Insert-Bloecke (Speicher-schonendes Bulk-Insert)."""

    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def generate_perf_database(
    db_url: str,
    *,
    count: int = DEFAULT_COUNT,
    seed: int = DEFAULT_SEED,
    reference_day: date = REFERENCE_DAY,
    batch_size: int = 5_000,
) -> PerfInfo:
    """Legt 5 Konten, eine Gruppe und ``count`` Buchungen in einer SQLite-Datei an.

    Das Schema kommt aus der echten Alembic-Kette (:func:`upgrade_to_head`), die
    Buchungen gehen per SQLAlchemy-Core-``insert`` in Bloecken hinein (N-DB-01).

    Args:
        db_url: SQLAlchemy-URL der Zieldatenbank (temporaere SQLite-Datei).
        count: Zahl der zu erzeugenden Buchungen (Standard 100.000).
        seed: Zufallsseed (Standard fester Wert).
        reference_day: Enddatum der Datumsverteilung.
        batch_size: Zeilen je Insert-Block.

    Returns:
        :class:`PerfInfo` mit Konten, Gruppe und Kennzahlen.
    """

    upgrade_to_head(db_url)
    engine = create_db_engine(db_url)
    factory: sessionmaker[Session] = make_session_factory(engine)
    try:
        with factory() as session:
            group = AccountGroup(name=_GROUP_NAME, sort_order=0)
            session.add(group)
            session.flush()

            accounts = [
                Account(
                    name=_ACCOUNT_NAMES[i % len(_ACCOUNT_NAMES)],
                    iban=_IBANS[i % len(_IBANS)],
                    currency="EUR",
                    source="datei",
                    account_type="Perf-Konto",
                    include_in_total=True,
                    group_id=group.id,
                    opening_balance_cents=0,
                )
                for i in range(ACCOUNT_COUNT)
            ]
            session.add_all(accounts)
            session.flush()
            account_ids = tuple(account.id for account in accounts)
            group_id = group.id
            session.commit()

            table = Transaction.__table__
            for batch in _chunks(
                _iter_rows(account_ids, seed=seed, count=count, reference_day=reference_day),
                batch_size,
            ):
                session.execute(insert(table), batch)
            session.commit()
    finally:
        engine.dispose()

    return PerfInfo(
        db_url=db_url,
        transaction_count=count,
        account_ids=account_ids,
        group_id=group_id,
        reference_day=reference_day,
    )


# ------------------------------------------------------------------- Messungen


def measure_ms(
    name: str,
    call: Any,
    *,
    warmups: int = WARMUP_RUNS,
    runs: int = MEASURE_RUNS,
) -> Measurement:
    """Misst ``call`` nach Aufwaermen und liefert Median und Maximum in Millisekunden.

    Der Median aus :data:`MEASURE_RUNS` Laeufen ist das Budgetkriterium; das
    Maximum wird zusaetzlich protokolliert (DoD N-PERF-01).
    """

    for _ in range(warmups):
        call()
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000.0)
    return Measurement(name=name, median_ms=statistics.median(samples), max_ms=max(samples), runs=runs)


def format_measurement(measurement: Measurement) -> str:
    """Formatiert eine Messung einzeilig (Median/Maximum in Millisekunden)."""

    verdict = "OK" if measurement.within_budget() else "UEBER_BUDGET"
    return (
        f"{measurement.name} median={measurement.median_ms:.1f}ms "
        f"max={measurement.max_ms:.1f}ms ({measurement.runs} Laeufe) {verdict}"
    )


def append_report(measurement: Measurement, report_path: str | os.PathLike[str] | None = None) -> None:
    """Schreibt eine Messung in den Messbericht (:data:`REPORT_ENV`), falls gesetzt."""

    target = report_path if report_path is not None else os.environ.get(REPORT_ENV)
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{measurement.name}\t{measurement.median_ms:.1f}\t{measurement.max_ms:.1f}\n")


# ------------------------------------------------------------------- CLI


def _default_db_path() -> Path:
    """Temporaerer Standardpfad fuer den CLI-Betrieb."""

    tmpdir = Path(tempfile.mkdtemp(prefix="finue-perf-"))
    return tmpdir / "perf.db"


def main(argv: list[str] | None = None) -> int:
    """CLI: erzeugt den Performance-Bestand und meldet die Kennzahlen."""

    parser = argparse.ArgumentParser(description="Performance-Datenbestand mit 100.000 Buchungen erzeugen")
    parser.add_argument("--db", type=Path, default=None, help="Zieldatei (Standard: temporaere Datei)")
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"Zahl der Buchungen (Standard {DEFAULT_COUNT})",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"Zufallsseed (Standard {DEFAULT_SEED})")
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else _default_db_path()
    db_url = "sqlite:///" + db_path.as_posix()

    start = time.perf_counter()
    info = generate_perf_database(db_url, count=args.count, seed=args.seed)
    duration = time.perf_counter() - start
    print(
        f"PERF_DATEN count={info.transaction_count} konten={len(info.account_ids)} "
        f"gruppe={info.group_id} seed={args.seed} dauer_s={duration:.1f} db={db_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
