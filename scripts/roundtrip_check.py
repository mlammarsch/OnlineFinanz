#!/usr/bin/env python3
"""Erweitertes Roundtrip-Pruefprogramm des Datei-Imports und -Exports.

Laeuft im Projekt-Interpreter (``uv run python scripts/roundtrip_check.py``),
weil ``scripts/validate.py`` selbst nur die Standardbibliothek nutzen darf.
Referenzierte Anforderungen:

- **F-EXP-04** verlustfreier JSON-Export (Kopf, Konten ohne Zugangsdaten,
  vollstaendige Buchungen samt Kennzeichen, Zeitpunkt und Herkunft).
- **F-EXP-06** JSON- und CSV-Re-Import ergeben 0 neue Buchungen; der eigene
  CSV-Export wird mit der Importvorlage "Eigener CSV-Export" gelesen.
- **F-EXP-07** jede Schema-Version unter ``docs/schemas/`` ist mit ihrer
  Testdatei importierbar; eine neuere Version wird mit klarer Meldung abgelehnt.
- **F-ABS-06** die Kennzeichen geprueft/abgestimmt sind nicht Teil des
  Fingerprints und bleiben nach Re-Import und Fake-Sync erhalten.
- **F-DUP-04** zusammengefuehrte Buchungen sind vom Export ausgeschlossen.

Es werden ausschliesslich erfundene Testdaten verwendet (D8): keine echten
Kontodaten, keine Zugangsdaten und keine Bank-/Netz-Zugriffe. Alle Schreib-
vorgaenge laufen in temporaeren, isolierten SQLite-Dateien.

Ausgabe bei Erfolg:
``ROUNDTRIP_OK fixture=<n> mehrkonto_reimport=<n> mehrkonto_frisch=<n>
csv=<n> schema=<n>``. Exit 0 nur bei vollstaendigem Erfolg, sonst Exit 1 mit
``ROUNDTRIP_FEHLER:``.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

# Der Projektcode liegt in backend/; beim direkten CLI-Aufruf fehlt es im Pfad.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import finue.models  # noqa: E402,F401  # registriert alle Tabellen an Base.metadata
from finue.db.base import Base  # noqa: E402
from finue.db.engine import create_db_engine, make_session_factory  # noqa: E402
from finue.models.account import Account, AccountGroup  # noqa: E402
from finue.models.transaction import Transaction  # noqa: E402
from finue.services.duplicate import (  # noqa: E402
    TransactionData as DuplicateTransactionData,
    compute_fingerprint,
    reconcile_multiset,
)
from finue.services.export import (  # noqa: E402
    CsvExportOptions,
    ExportFormat,
    ExportRequest,
    ExportScope,
    run_export,
    strip_export_guard,
)
from finue.services.export_schema import SUPPORTED_EXPORT_VERSION  # noqa: E402
from finue.services.import_detect import CsvOptions, load_rows  # noqa: E402
from finue.services.import_json import amount_string_to_cents, parse_own_json  # noqa: E402
from finue.services.import_run import execute_import  # noqa: E402
from finue.services.import_templates import (  # noqa: E402
    OWN_CSV_TEMPLATE_NAME,
    list_templates,
    seed_default_templates,
)
from finue.services.search import SearchPeriod  # noqa: E402
from finue.services.search_text import build_search_text  # noqa: E402

#: Fester Bezugszeitpunkt fuer reproduzierbare Exporte (F-EXP-02).
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 28)
MAX_ROWS = 10_000

_SCHEMA_FILE_RE = re.compile(r"export-v(\d+)\.schema\.json")


def main() -> int:
    """Fuehrt alle Roundtrip-Bloecke aus und meldet das Gesamtergebnis."""
    fuenf_fehler: dict[str, list[str]] = {}
    zaehler: dict[str, int] = {}

    with tempfile.TemporaryDirectory(prefix="finue-roundtrip-") as tmp:
        tmp_dir = Path(tmp)
        zaehler["fixture"], fuenf_fehler["fixture"] = _check_fixture_roundtrip(tmp_dir)
        mehrkonto, fuenf_fehler["mehrkonto"] = _check_multi_account_json(tmp_dir)
        zaehler.update({f"mehrkonto_{k}": v for k, v in mehrkonto.items()})
        zaehler["csv"], fuenf_fehler["csv"] = _check_csv_template_roundtrip(tmp_dir)
        zaehler["schema"], fuenf_fehler["schema"] = _check_schema_versions(tmp_dir)

    fehler = [f"{block}: {msg}" for block, liste in fuenf_fehler.items() for msg in liste]
    if fehler:
        print("ROUNDTRIP_FEHLER: " + "; ".join(fehler))
        return 1

    print(
        "ROUNDTRIP_OK "
        f"fixture={zaehler['fixture']} "
        f"mehrkonto_reimport={zaehler['mehrkonto_reimport']} "
        f"mehrkonto_frisch={zaehler['mehrkonto_frisch']} "
        f"csv={zaehler['csv']} "
        f"schema={zaehler['schema']}"
    )
    return 0


# --------------------------------------------------------------- Hilfsfunktionen


def _new_session(tmp_dir: Path, name: str) -> tuple[Engine, Session]:
    """Erzeugt eine isolierte SQLite-Datei mit dem aktuellen Schema."""
    engine = create_db_engine("sqlite:///" + (tmp_dir / name).as_posix())
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)()


def _norm_dt(value: object) -> str:
    """Normalisiert einen Zeitstempel (String oder datetime) auf ISO-UTC oder ``""``."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        moment = datetime.fromisoformat(text)
    elif isinstance(value, datetime):
        moment = value
    else:
        return ""
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _text(value: object) -> str:
    """Normalisiert optionale Textwerte fuer den Feldvergleich auf einen String."""
    return value if isinstance(value, str) else ""


def _canonical_row(
    *,
    account_id: int,
    status: str,
    booking_date: date,
    value_date: date | None,
    counterparty: object,
    counterparty_iban: object,
    purpose: object,
    note: object,
    checked: bool,
    checked_at: object,
    checked_origin: object,
    reconciled: bool,
    reconciled_at: object,
    reconciled_origin: object,
    origin: str,
    external_ref: object,
    amount_cents: int,
    currency: str,
) -> tuple[object, ...]:
    """Bildet die vergleichbare Feldprojektion einer Buchung (F-EXP-04)."""
    return (
        account_id,
        status,
        booking_date.isoformat(),
        value_date.isoformat() if value_date is not None else "",
        _text(counterparty),
        _text(counterparty_iban),
        _text(purpose),
        _text(note),
        bool(checked),
        _norm_dt(checked_at),
        _text(checked_origin),
        bool(reconciled),
        _norm_dt(reconciled_at),
        _text(reconciled_origin),
        origin,
        _text(external_ref),
        amount_cents,
        currency,
    )


def _doc_row_key(transaction: dict[str, Any]) -> tuple[object, ...]:
    """Feldprojektion einer Buchung aus dem JSON-Exportdokument."""
    value_date = transaction.get("value_date")
    return _canonical_row(
        account_id=int(transaction["account_id"]),
        status=str(transaction["status"]),
        booking_date=date.fromisoformat(str(transaction["booking_date"])),
        value_date=date.fromisoformat(str(value_date)) if value_date else None,
        counterparty=transaction.get("counterparty"),
        counterparty_iban=transaction.get("counterparty_iban"),
        purpose=transaction.get("purpose"),
        note=transaction.get("note"),
        checked=bool(transaction["checked"]),
        checked_at=transaction.get("checked_at"),
        checked_origin=transaction.get("checked_origin"),
        reconciled=bool(transaction["reconciled"]),
        reconciled_at=transaction.get("reconciled_at"),
        reconciled_origin=transaction.get("reconciled_origin"),
        origin=str(transaction["origin"]),
        external_ref=transaction.get("external_ref"),
        amount_cents=amount_string_to_cents(str(transaction["amount"])),
        currency=str(transaction["currency"]),
    )


def _db_row_key(transaction: Transaction) -> tuple[object, ...]:
    """Feldprojektion einer gespeicherten Buchung."""
    return _canonical_row(
        account_id=transaction.account_id,
        status=transaction.status,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        counterparty=transaction.counterparty,
        counterparty_iban=transaction.counterparty_iban,
        purpose=transaction.purpose,
        note=transaction.note,
        checked=transaction.checked,
        checked_at=transaction.checked_at,
        checked_origin=transaction.checked_origin,
        reconciled=transaction.reconciled,
        reconciled_at=transaction.reconciled_at,
        reconciled_origin=transaction.reconciled_origin,
        origin=transaction.origin,
        external_ref=transaction.external_ref,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
    )


def _snapshot(session: Session) -> list[tuple[object, ...]]:
    """Feldprojektionen aller Buchungen (inklusive zusammengefuehrter)."""
    rows = session.scalars(select(Transaction).order_by(Transaction.id)).all()
    return sorted(_db_row_key(row) for row in rows)


def _compare_multiset(expected: list[tuple[object, ...]], actual: list[tuple[object, ...]], label: str) -> list[str]:
    """Vergleicht zwei Feldprojektions-Multimengen und meldet Abweichungen."""
    if sorted(expected) == sorted(actual):
        return []
    missing = [row for row in expected if row not in actual]
    extra = [row for row in actual if row not in expected]
    return [f"{label}: {len(missing)} fehlende und {len(extra)} zusaetzliche Buchung(en) im Feldvergleich"]


def _add_transactions(session: Session, specs: list[dict[str, Any]]) -> None:
    """Legt Buchungen mit deterministischem Fingerprint/Index an (F-DUP-01/02)."""
    counters: dict[tuple[int, str], int] = {}
    for spec in specs:
        fields = dict(spec)
        fingerprint = compute_fingerprint(
            account_id=fields["account_id"],
            booking_date=fields["booking_date"],
            value_date=fields.get("value_date"),
            amount_cents=fields["amount_cents"],
            currency=fields.get("currency", "EUR"),
            counterparty=fields.get("counterparty"),
            counterparty_iban=fields.get("counterparty_iban"),
            purpose=fields.get("purpose"),
        )
        key = (fields["account_id"], fingerprint)
        index = counters.get(key, -1) + 1
        counters[key] = index
        session.add(
            Transaction(
                account_id=fields["account_id"],
                status=fields.get("status", "gebucht"),
                booking_date=fields["booking_date"],
                value_date=fields.get("value_date"),
                counterparty=fields.get("counterparty"),
                counterparty_iban=fields.get("counterparty_iban"),
                purpose=fields.get("purpose"),
                note=fields.get("note"),
                checked=fields.get("checked", False),
                checked_at=fields.get("checked_at"),
                checked_origin=fields.get("checked_origin"),
                reconciled=fields.get("reconciled", False),
                reconciled_at=fields.get("reconciled_at"),
                reconciled_origin=fields.get("reconciled_origin"),
                origin=fields.get("origin", "csv"),
                external_ref=fields.get("external_ref"),
                fingerprint=fingerprint,
                occurrence_index=index,
                merged=fields.get("merged", False),
                amount_cents=fields["amount_cents"],
                currency=fields.get("currency", "EUR"),
                search_text=build_search_text(
                    counterparty=fields.get("counterparty"),
                    purpose=fields.get("purpose"),
                    note=fields.get("note"),
                    counterparty_iban=fields.get("counterparty_iban"),
                    external_ref=fields.get("external_ref"),
                ),
            )
        )
    session.commit()


def _export_json(session: Session, request: ExportRequest) -> dict[str, Any]:
    """Fuehrt einen JSON-Export aus und liefert das geparste Dokument (F-EXP-04)."""
    result = run_export(session, request, now=NOW, today=TODAY, max_rows=MAX_ROWS)
    document = json.loads(result.content)
    assert isinstance(document, dict)
    return document


def _subset_document(document: dict[str, Any], account_id: int) -> dict[str, Any]:
    """Bildet ein Teildokument mit den Buchungen genau eines Kontos."""
    return {
        **document,
        "transactions": [tx for tx in document["transactions"] if tx["account_id"] == account_id],
    }


# ------------------------------------------------------------- Block 0: Fixture


def _check_fixture_roundtrip(tmp_dir: Path) -> tuple[int, list[str]]:
    """Bisheriger Import-Roundtrip der JSON-v1-Fixture (F-EXP-04/06, F-IMP-04)."""
    fixture = ROOT / "tests" / "fixtures" / "export_v1_sample.json"
    if not fixture.is_file():
        return 0, [f"Fixture fehlt: {fixture}"]

    document = json.loads(fixture.read_text(encoding="utf-8"))
    expected = len(document.get("transactions", []))
    if expected < 1:
        return 0, ["Fixture enthaelt keine Buchungen"]

    engine, session = _new_session(tmp_dir, "fixture.db")
    try:
        session.add(Account(id=1, name="Roundtrip-Pruefung", currency="EUR", source="datei"))
        session.commit()

        first = execute_import(1, document, None, "json", session)
        total_first = session.scalar(select(func.count()).select_from(Transaction)) or 0
        second = execute_import(1, document, None, "json", session)
        total_second = session.scalar(select(func.count()).select_from(Transaction)) or 0
    finally:
        session.close()
        engine.dispose()

    fehler: list[str] = []
    if first.status != "erfolgreich":
        fehler.append(f"erster Lauf Status {first.status!r}")
    if first.new_count != expected:
        fehler.append(f"erster Lauf new_count={first.new_count}, erwartet {expected}")
    if total_first != expected:
        fehler.append(f"Bestand nach erstem Lauf {total_first}, erwartet {expected}")
    if second.status != "erfolgreich":
        fehler.append(f"zweiter Lauf Status {second.status!r}")
    if second.new_count != 0:
        fehler.append(f"zweiter Lauf new_count={second.new_count}, erwartet 0")
    if total_second != total_first:
        fehler.append(f"Bestand nach zweitem Lauf {total_second}, erwartet {total_first}")
    return total_second, fehler


# ------------------------------------------------- Block 1: Mehrkonto-JSON


def _seed_multi_account_base(session: Session) -> None:
    """Bestand: 3 Konten, eine Gruppe, Notizen, Kennzeichen/Referenzen, ein Merge-Paar."""
    session.add(AccountGroup(id=1, name="Haushalt", sort_order=0))
    session.add_all(
        [
            Account(
                id=1,
                name="Girokonto",
                iban="DE89370400440532013000",
                currency="EUR",
                source="datei",
                account_type="giro",
                group_id=1,
            ),
            Account(
                id=2,
                name="Tagesgeld",
                iban=None,
                currency="EUR",
                source="datei",
                account_type="sparen",
                group_id=1,
            ),
            Account(
                id=3,
                name="Kreditkarte",
                iban="DE02120300000000202051",
                currency="EUR",
                source="datei",
                account_type="kreditkarte",
                group_id=None,
            ),
        ]
    )
    session.flush()
    _add_transactions(
        session,
        [
            # Konto 1: Notiz, beide Kennzeichen mit Herkunft, externe Referenz.
            {
                "account_id": 1,
                "booking_date": date(2026, 1, 15),
                "value_date": date(2026, 1, 16),
                "counterparty": "Beispiel Markt GmbH",
                "counterparty_iban": "DE02120300000000202051",
                "purpose": "Einkauf",
                "note": "Wocheneinkauf",
                "checked": True,
                "checked_at": datetime(2026, 1, 17, 8, 0, tzinfo=timezone.utc),
                "checked_origin": "ui",
                "reconciled": True,
                "reconciled_at": datetime(2026, 1, 18, 9, 30, tzinfo=timezone.utc),
                "reconciled_origin": "import",
                "origin": "fints",
                "external_ref": "HAUSHALT-2026-0001",
                "amount_cents": -4883,
            },
            # Konto 1: nur geprueft, Herkunft mcp.
            {
                "account_id": 1,
                "booking_date": date(2026, 2, 3),
                "value_date": date(2026, 2, 4),
                "counterparty": "Stadtwerke Beispielstadt",
                "purpose": "Abschlag Strom Maerz",
                "checked": True,
                "checked_at": datetime(2026, 2, 5, 7, 15, tzinfo=timezone.utc),
                "checked_origin": "mcp",
                "origin": "fints",
                "amount_cents": -8900,
            },
            # Konto 1: Nicht-zusammengefuehrte Seite eines Paares (F-DUP-05).
            {
                "account_id": 1,
                "booking_date": date(2026, 4, 10),
                "counterparty": "Doppelt GmbH",
                "purpose": "Merge-Probe",
                "origin": "csv",
                "amount_cents": -1111,
            },
            # Konto 1: zusammengefuehrte Seite (merged, F-DUP-04) -- wird ausgeschlossen.
            {
                "account_id": 1,
                "booking_date": date(2026, 4, 10),
                "counterparty": "Doppelt GmbH",
                "purpose": "Merge-Probe",
                "origin": "csv",
                "amount_cents": -1111,
                "merged": True,
            },
            # Konto 2: Notiz und abgestimmt mit Herkunft ui, externe Referenz.
            {
                "account_id": 2,
                "booking_date": date(2026, 3, 1),
                "counterparty": "Sparplan",
                "purpose": "Monatliche Ruecklage",
                "note": "Ruecklage fuer Urlaub",
                "reconciled": True,
                "reconciled_at": datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
                "reconciled_origin": "ui",
                "origin": "csv",
                "external_ref": "ACTUAL-2026-0042",
                "amount_cents": 50000,
            },
            # Konto 3: Vormerkung mit Notiz.
            {
                "account_id": 3,
                "status": "vorgemerkt",
                "booking_date": date(2026, 11, 24),
                "counterparty": "Online Shop Beispiel",
                "counterparty_iban": "AT611904300234573201",
                "purpose": "Bestellung 4711",
                "note": "Geschenk",
                "origin": "json",
                "amount_cents": -12999,
            },
        ],
    )


def _fake_sync(session: Session, account_id: int, document: dict[str, Any]) -> int:
    """Simuliert einen Sync ohne Bankzugriff (F-ABS-06): Multimengen-Abgleich."""
    incoming = [
        DuplicateTransactionData(
            booking_date=date.fromisoformat(str(tx["booking_date"])),
            value_date=date.fromisoformat(str(tx["value_date"])) if tx["value_date"] else None,
            amount_cents=amount_string_to_cents(str(tx["amount"])),
            currency=str(tx["currency"]),
            counterparty=tx["counterparty"],
            counterparty_iban=tx["counterparty_iban"],
            purpose=tx["purpose"],
            origin=str(tx["origin"]),
            status=str(tx["status"]),
        )
        for tx in document["transactions"]
        if tx["account_id"] == account_id
    ]
    result = reconcile_multiset(account_id, incoming, session)
    session.rollback()
    return result.inserted_count


def _check_multi_account_json(tmp_dir: Path) -> tuple[dict[str, int], list[str]]:
    """Mehrkonto-JSON-Roundtrip mit Feldvergleich und Fake-Sync (F-EXP-04/06, F-ABS-06).

    Returns:
        Zaehler (Re-Import und frische DB) sowie die Fehlerliste.
    """
    fehler: list[str] = []
    engine, session = _new_session(tmp_dir, "multi.db")
    zaehler = {"reimport": 0, "frisch": 0}
    try:
        _seed_multi_account_base(session)
        before = _snapshot(session)

        document = _export_json(
            session,
            ExportRequest(format=ExportFormat.JSON, scope=ExportScope.ALL, period=SearchPeriod.ALL),
        )
        exported = document["transactions"]
        exported_keys = sorted(_doc_row_key(tx) for tx in exported)
        exported_accounts = {account["id"] for account in document["accounts"]}
        if len(exported) != 5:
            fehler.append(f"JSON-Export umfasst {len(exported)} statt 5 Buchungen (Merge-Ausschluss F-DUP-04)")
        if exported_accounts != {1, 2, 3}:
            fehler.append(f"JSON-Export nennt Konten {sorted(exported_accounts)} statt 1, 2, 3")

        # Umfang Gruppe: nur Konten der Gruppe 1 (Konto 3 bleibt aussen vor).
        group_document = _export_json(
            session,
            ExportRequest(
                format=ExportFormat.JSON,
                scope=ExportScope.GROUP,
                group_id=1,
                period=SearchPeriod.ALL,
            ),
        )
        group_account_ids = {account["id"] for account in group_document["accounts"]}
        if group_account_ids != {1, 2}:
            fehler.append(f"Gruppen-Export nennt Konten {sorted(group_account_ids)} statt 1, 2")
        if sorted(_doc_row_key(tx) for tx in group_document["transactions"]) != [
            key for key in exported_keys if key[0] in {1, 2}
        ]:
            fehler.append("Gruppen-Export enthaelt nicht genau die Buchungen der Gruppe")

        # Re-Import in dieselbe DB: je Konto 0 neu, unveraenderter Stand.
        for account in document["accounts"]:
            subset = _subset_document(document, account["id"])
            run = execute_import(account["id"], subset, None, "json", session)
            if run.new_count != 0:
                fehler.append(f"Re-Import Konto {account['id']}: new_count={run.new_count}, erwartet 0")
        after = _snapshot(session)
        if before != after:
            fehler.append("Re-Import hat den Bestand veraendert (Feldvergleich vor/nach)")

        # Fake-Sync (ohne Bankzugriff): nichts neu, Kennzeichen bleiben (F-ABS-06).
        if _fake_sync(session, 1, document) != 0:
            fehler.append("Fake-Sync hat Buchungen angelegt, erwartet 0")
        marker = session.scalars(
            select(Transaction).where(Transaction.external_ref == "HAUSHALT-2026-0001")
        ).one_or_none()
        if marker is None:
            fehler.append("Kennzeichen-Buchung nach Re-Import/Sync nicht gefunden")
        elif not (marker.checked and marker.reconciled and marker.checked_origin == "ui"):
            fehler.append("Kennzeichen nach Re-Import/Sync nicht erhalten (F-ABS-06)")

        zaehler["reimport"] = len(exported)

        # Import in eine frische DB: gleicher Stand (Feldvergleich).
        fresh_engine, fresh = _new_session(tmp_dir, "frisch.db")
        try:
            for account in document["accounts"]:
                fresh.add(
                    Account(
                        id=account["id"],
                        name=account["name"],
                        iban=account["iban"],
                        currency=account["currency"],
                        source="datei",
                        account_type=account["account_type"],
                    )
                )
            fresh.commit()
            for account in document["accounts"]:
                subset = _subset_document(document, account["id"])
                run = execute_import(account["id"], subset, None, "json", fresh)
                if run.status not in ("erfolgreich", "teilweise"):
                    fehler.append(f"Frische DB Konto {account['id']}: Status {run.status!r}")
            fresh_total = fresh.scalar(select(func.count()).select_from(Transaction)) or 0
            if fresh_total != len(exported):
                fehler.append(f"Frische DB hat {fresh_total} statt {len(exported)} Buchungen")
            fehler.extend(_compare_multiset(exported_keys, _snapshot(fresh), "Frische DB"))
            zaehler["frisch"] = fresh_total
        finally:
            fresh.close()
            fresh_engine.dispose()
    finally:
        session.close()
        engine.dispose()
    return zaehler, fehler


# --------------------------------------------------- Block 2: CSV mit Vorlage


def _check_csv_template_roundtrip(tmp_dir: Path) -> tuple[int, list[str]]:
    """CSV-Export mit Standardprofil -> Import mit Vorlage = 0 neu (F-EXP-06)."""
    engine, session = _new_session(tmp_dir, "csv.db")
    fehler: list[str] = []
    skipped = 0
    try:
        session.add(Account(id=1, name="Girokonto", currency="EUR", source="datei"))
        session.flush()
        _add_transactions(
            session,
            [
                {
                    "account_id": 1,
                    "booking_date": date(2026, 1, 15),
                    "counterparty": "=SUM(A1:A2)",
                    "counterparty_iban": "DE02120300000000202051",
                    "purpose": "Einkauf",
                    "amount_cents": -1000,
                },
                {
                    "account_id": 1,
                    "booking_date": date(2026, 1, 16),
                    "counterparty": "Beispiel Markt GmbH",
                    "purpose": "Einkauf",
                    "amount_cents": -4883,
                },
            ],
        )

        seed_default_templates(session)
        template = {item.name: item for item in list_templates(session)}[OWN_CSV_TEMPLATE_NAME]
        result = run_export(
            session,
            ExportRequest(
                format=ExportFormat.CSV,
                scope=ExportScope.ACCOUNT,
                account_id=1,
                period=SearchPeriod.ALL,
                csv=CsvExportOptions(),
            ),
            now=NOW,
            today=TODAY,
            max_rows=MAX_ROWS,
        )
        rows = load_rows(
            result.content.encode(result.encoding),
            kind="csv",
            options=CsvOptions(
                encoding=template.encoding,
                delimiter=template.delimiter,
                decimal_separator=template.decimal_separator,
                date_format=template.date_format,
            ),
        )
        rows = strip_export_guard(cast("list[dict[str, Any]]", rows))
        run = execute_import(1, rows, template.mapping, "csv", session)
        skipped = run.skipped_count
        if run.new_count != 0:
            fehler.append(f"CSV-Re-Import new_count={run.new_count}, erwartet 0")
        if run.skipped_count != 2:
            fehler.append(f"CSV-Re-Import skipped_count={run.skipped_count}, erwartet 2")
    finally:
        session.close()
        engine.dispose()
    return skipped, fehler


# ------------------------------------------------ Block 3: Schema-Versionen


def _import_document_into_fresh(tmp_dir: Path, document: dict[str, Any], name: str) -> list[str]:
    """Importiert ein Dokument in eine frische DB und prueft die Buchungszahl."""
    fehler: list[str] = []
    engine, session = _new_session(tmp_dir, name)
    try:
        for account in document["accounts"]:
            session.add(
                Account(
                    id=account["id"],
                    name=account["name"],
                    iban=account["iban"],
                    currency=account["currency"],
                    source="datei",
                    account_type=account["account_type"],
                )
            )
        session.commit()
        new_total = 0
        for account in document["accounts"]:
            subset = _subset_document(document, account["id"])
            run = execute_import(account["id"], subset, None, "json", session)
            if run.status not in ("erfolgreich", "teilweise"):
                fehler.append(f"{name} Konto {account['id']}: Status {run.status!r}")
            new_total += run.new_count
        if new_total != len(document["transactions"]):
            fehler.append(f"{name}: {new_total} importiert, erwartet {len(document['transactions'])}")
    finally:
        session.close()
        engine.dispose()
    return fehler


def _check_schema_versions(tmp_dir: Path) -> tuple[int, list[str]]:
    """Jede Schema-Version importierbar; neuere Version klar abgelehnt (F-EXP-07)."""
    schemas_dir = ROOT / "docs" / "schemas"
    schema_files = sorted(schemas_dir.glob("export-v*.schema.json"))
    if not schema_files:
        return 0, ["keine Schema-Datei unter docs/schemas/ gefunden"]

    fehler: list[str] = []
    geprueft = 0
    example_for_reject: dict[str, Any] | None = None
    for schema_file in schema_files:
        match = _SCHEMA_FILE_RE.fullmatch(schema_file.name)
        if match is None:
            fehler.append(f"Schema-Dateiname nicht erkannt: {schema_file.name}")
            continue
        version = int(match.group(1))
        example = schemas_dir / f"export-v{version}.example.json"
        if not example.is_file():
            fehler.append(f"Testdatei fehlt: {example.name}")
            continue
        document = json.loads(example.read_text(encoding="utf-8"))
        if document.get("version") != version:
            fehler.append(f"{example.name}: version {document.get('version')} passt nicht zum Dateinamen")
            continue
        if version <= SUPPORTED_EXPORT_VERSION:
            fehler.extend(_import_document_into_fresh(tmp_dir, document, f"schema-v{version}.db"))
            geprueft += 1
        else:
            # Eine neue, noch nicht unterstuetzte Version muss importseitig abgelehnt werden.
            fehler.extend(_expect_rejected(document, version, example.name))
        if example_for_reject is None:
            example_for_reject = document

    # Synthetischer neuerer Fall, auch wenn (noch) keine hoehere Schema-Datei existiert.
    if example_for_reject is not None:
        bumped = {**example_for_reject, "version": SUPPORTED_EXPORT_VERSION + 1}
        fehler.extend(_expect_rejected(bumped, SUPPORTED_EXPORT_VERSION + 1, "synthetische neuere Version"))
    return geprueft, fehler


def _expect_rejected(document: dict[str, Any], version: int, label: str) -> list[str]:
    """Erwartet, dass der Import eine neuere Formatversion mit Klartext ablehnt."""
    try:
        parse_own_json(document)
    except ValueError as exc:
        if str(version) in str(exc):
            return []
        return [f"{label}: Ablehnung nennt Version {version} nicht (Meldung: {exc})"]
    return [f"{label}: Version {version} wurde nicht abgelehnt (F-EXP-07)"]


if __name__ == "__main__":
    raise SystemExit(main())
