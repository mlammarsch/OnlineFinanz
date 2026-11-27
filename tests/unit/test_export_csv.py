"""Tests fuer den CSV-Export, die Optionen und den Roundtrip (T-098).

Referenziert F-EXP-03 (CSV-Optionen, Spalten, Zusatzspalten, Kodierung,
Formel-Schutz, Standardprofil) und F-EXP-06 (CSV-Export mit passender Vorlage
ergibt 0 neue Buchungen). Abgedeckt:

- Jede Option: Trennzeichen (``;``/``,``/Tabulator), Dezimalformat
  (deutsch/englisch), Datumsformat (deutsch/ISO), Spaltenauswahl und
  -reihenfolge, Zusatzspalten Notiz/geprueft/abgestimmt/externe Referenz.
- Kodierungen byteweise: UTF-8, UTF-8 mit BOM, Windows-1252 mit dokumentierter
  Regel fuer nicht darstellbare Zeichen.
- Formel-Injection: Entschaerfung fuehrender Formelzeichen in Freitextzellen;
  Betrag und Datum bleiben unveraendert; die Ruecknahme beim Import haelt den
  Fingerprint intakt.
- Roundtrip F-EXP-06: CSV-Export mit Standardprofil ueber die Importvorlage
  "Eigener CSV-Export" ergibt 0 neue Buchungen (auch ueber die REST-Importkette).
- Zusammengefuehrte Buchungen sind ausgeschlossen (F-DUP-04).

Alle Konten und Buchungen sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten, keine Bankzugriffe und keine externen Schreibaktionen.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.export import (
    CSV_COLUMN_LABELS,
    DEFAULT_CSV_COLUMNS,
    CsvExportOptions,
    ExportFormat,
    ExportRequest,
    ExportScope,
    ExportValidationError,
    build_csv,
    build_filename,
    encode_content,
    merge_csv_options,
    run_export,
    strip_export_guard,
    validate_csv_columns,
    validate_csv_options,
)
from finue.services.import_detect import CsvOptions, load_rows
from finue.services.import_run import execute_import
from finue.services.import_templates import OWN_CSV_TEMPLATE_NAME, list_templates, seed_default_templates
from finue.services.search import SearchPeriod
from finue.services.search_text import build_search_text

NOW = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 1, 20)

_occurrence_counts: dict[tuple[int, str], int] = {}


def _session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def _add_account(client: TestClient, name: str = "Konto", **overrides: object) -> int:
    fields: dict[str, object] = {"name": name, "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    with _session(client) as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


def _to_fingerprint(account_id: int, fields: dict[str, Any]) -> str:
    return compute_fingerprint(
        account_id=account_id,
        booking_date=fields.get("booking_date", date(2026, 5, 1)),
        value_date=fields.get("value_date"),
        amount_cents=fields.get("amount_cents", -1000),
        currency=fields.get("currency", "EUR"),
        counterparty=fields.get("counterparty"),
        counterparty_iban=fields.get("counterparty_iban"),
        purpose=fields.get("purpose"),
    )


def _add_transaction(client: TestClient, account_id: int, **overrides: object) -> int:
    fields: dict[str, Any] = {
        "account_id": account_id,
        "booking_date": date(2026, 1, 15),
        "amount_cents": -1000,
        "currency": "EUR",
        "origin": "csv",
        "status": "gebucht",
    }
    fields.update(overrides)
    if "fingerprint" not in fields:
        fields["fingerprint"] = _to_fingerprint(account_id, fields)
    if "occurrence_index" not in overrides:
        key = (account_id, str(fields["fingerprint"]))
        _occurrence_counts[key] = _occurrence_counts.get(key, -1) + 1
        fields["occurrence_index"] = _occurrence_counts[key]
    fields["search_text"] = build_search_text(
        counterparty=fields.get("counterparty"),
        purpose=fields.get("purpose"),
        note=fields.get("note"),
        counterparty_iban=fields.get("counterparty_iban"),
        external_ref=fields.get("external_ref"),
    )
    with _session(client) as session:
        transaction = Transaction(**fields)
        session.add(transaction)
        session.commit()
        return transaction.id


def _memory_session() -> Session:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def _run(session: Session, request: ExportRequest) -> Any:
    return run_export(session, request, now=NOW, today=TODAY, max_rows=1000)


def _csv_request(**overrides: object) -> ExportRequest:
    fields: dict[str, Any] = {
        "format": ExportFormat.CSV,
        "scope": ExportScope.ACCOUNT,
        "account_id": 1,
        "period": SearchPeriod.ALL,
    }
    fields.update(overrides)
    return ExportRequest(**fields)


def _parse(content: str, delimiter: str = ";") -> list[list[str]]:
    return list(csv.reader(io.StringIO(content), delimiter=delimiter))


# ------------------------------------------------------------------ F-EXP-03


def test_csv_standardprofil_spalten_und_kopfzeile(client: TestClient) -> None:
    """F-EXP-03: Standardprofil exportiert die Spalten in fester Reihenfolge."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(
        client,
        account_id,
        booking_date=date(2026, 1, 15),
        amount_cents=-4883,
        counterparty="Beispiel Markt GmbH",
        counterparty_iban="DE02120300000000202051",
        purpose="Einkauf",
        note="Wocheneinkauf",
        checked=True,
        checked_at=NOW,
        checked_origin="ui",
        reconciled=True,
        reconciled_at=NOW,
        reconciled_origin="import",
        external_ref="ACTUAL-2026-0001",
    )

    with _session(client) as session:
        result = _run(session, _csv_request(account_id=account_id))

    rows = _parse(result.content)
    assert result.media_type == "text/csv"
    assert result.filename == "girokonto_2026-01-15_2026-01-15.csv"
    assert rows[0] == [CSV_COLUMN_LABELS[column] for column in DEFAULT_CSV_COLUMNS]
    assert len(rows) == 2
    body = dict(zip(rows[0], rows[1], strict=True))
    assert body["Buchungstag"] == "15.01.2026"
    assert body["Betrag"] == "-48,83"
    assert body["Waehrung"] == "EUR"
    assert body["Geprueft"] == "ja"
    assert body["Abgestimmt"] == "ja"
    assert body["Notiz"] == "Wocheneinkauf"
    assert body["Externe Referenz"] == "ACTUAL-2026-0001"


def test_csv_trennzeichen_varianten(client: TestClient) -> None:
    """F-EXP-03: Semikolon, Komma und Tabulator als Trennzeichen."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="Markt")

    for delimiter in (";", ",", "\t"):
        with _session(client) as session:
            options = CsvExportOptions(delimiter=delimiter, columns=("counterparty", "amount"))
            result = _run(
                session,
                _csv_request(account_id=account_id, csv=options),
            )
        rows = _parse(result.content, delimiter)
        assert rows[0] == ["Gegenpartei", "Betrag"]
        assert rows[1][0] == "Markt"


def test_csv_dezimal_und_datumsformat_optionen(client: TestClient) -> None:
    """F-EXP-03: Dezimalformat deutsch/englisch und Datumsformat deutsch/ISO."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, booking_date=date(2026, 1, 15), amount_cents=-4883)

    with _session(client) as session:
        german = _run(
            session,
            _csv_request(account_id=account_id, csv=CsvExportOptions(columns=("booking_date", "amount"))),
        )
    assert _parse(german.content)[1] == ["15.01.2026", "-48,83"]

    with _session(client) as session:
        english = _run(
            session,
            _csv_request(
                account_id=account_id,
                csv=CsvExportOptions(
                    decimal_separator="point",
                    date_format="JJJJ-MM-TT",
                    columns=("booking_date", "amount"),
                ),
            ),
        )
    assert _parse(english.content)[1] == ["2026-01-15", "-48.83"]


def test_csv_spaltenauswahl_und_reihenfolge(client: TestClient) -> None:
    """F-EXP-03: Spaltenauswahl und -reihenfolge werden beachtet."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="Markt", amount_cents=-100)

    with _session(client) as session:
        result = _run(
            session,
            _csv_request(account_id=account_id, csv=CsvExportOptions(columns=("amount", "booking_date"))),
        )

    rows = _parse(result.content)
    assert rows[0] == ["Betrag", "Buchungstag"]
    assert rows[1] == ["-1,00", "15.01.2026"]


def test_csv_gruppenansicht_hat_kontospalte(client: TestClient) -> None:
    """F-EXP-03: In der Gruppenansicht wird die Spalte Konto vorangestellt."""

    with _session(client) as session:
        group = AccountGroup(name="Haushalt")
        session.add(group)
        session.flush()
        account = Account(name="Girokonto", currency="EUR", source="datei", group_id=group.id)
        session.add(account)
        session.commit()
        group_id = group.id
        account_id = account.id

    _add_transaction(client, account_id, counterparty="Markt")
    with _session(client) as session:
        result = _run(
            session,
            _csv_request(
                account_id=None,
                scope=ExportScope.GROUP,
                group_id=group_id,
                csv=CsvExportOptions(columns=("counterparty",)),
            ),
        )

    rows = _parse(result.content)
    assert rows[0] == ["Konto", "Gegenpartei"]
    assert rows[1] == ["Girokonto", "Markt"]


def test_csv_kodierung_byteweise() -> None:
    """F-EXP-03: UTF-8, UTF-8 mit BOM und Windows-1252 byteweise."""

    text = "Notiz mit Sonderzeichen: \u20ac und \u6f22"

    assert encode_content(text, "utf-8") == text.encode("utf-8")
    assert not encode_content(text, "utf-8").startswith(b"\xef\xbb\xbf")

    with_bom = encode_content(text, "utf-8-sig")
    assert with_bom.startswith(b"\xef\xbb\xbf")
    assert with_bom.decode("utf-8-sig") == text

    cp1252 = encode_content(text, "cp1252")
    # Euro ist in cp1252 enthalten, das CJK-Zeichen nicht: dokumentierte Regel
    # ersetzt nicht darstellbare Zeichen mit "?".
    assert b"?" in cp1252
    assert cp1252.decode("cp1252").startswith("Notiz mit Sonderzeichen: \u20ac und ?")


def test_csv_kodierung_ueber_api_setzt_bom(client: TestClient) -> None:
    """F-EXP-03: ``encoding='utf-8-sig'`` liefert die BOM in den Antwortbytes."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="Markt")

    response = client.post(
        "/api/export",
        json={
            "format": "csv",
            "scope": "account",
            "account_id": account_id,
            "period": "all",
            "csv": {"encoding": "utf-8-sig"},
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-disposition"].endswith('.csv"')


def test_csv_optionen_validierung() -> None:
    """F-EXP-03: Unbekannte Optionen, Spalten und Dopplungen werden abgelehnt."""

    options = validate_csv_options(None)
    assert options.delimiter == ";"
    assert options.decimal_separator == "comma"
    assert options.columns == DEFAULT_CSV_COLUMNS

    for bad in ({"delimiter": "|"}, {"decimal_separator": "dot"}, {"unbekannt": 1}, {"guard_formulas": "ja"}):
        try:
            validate_csv_options(bad)
        except ExportValidationError:
            pass
        else:  # pragma: no cover - Fehlerpfad muss greifen
            raise AssertionError(f"Option {bad!r} wurde nicht abgelehnt.")

    for bad_columns in ([], ["unbekannt"], ["amount", "amount"], "amount"):
        try:
            validate_csv_columns(bad_columns)
        except ExportValidationError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"Spalten {bad_columns!r} wurden nicht abgelehnt.")


def test_csv_merge_options_ueberschreibt_einzeln() -> None:
    """F-EXP-03: Anfragewerte ueberschreiben einzelne Profiloptionen."""

    base = CsvExportOptions(delimiter=";", columns=("amount", "counterparty"))
    merged = merge_csv_options(base, {"delimiter": ","})
    assert merged.delimiter == ","
    assert merged.columns == ("amount", "counterparty")
    assert merged.decimal_separator == "comma"


# --------------------------------------------------------- Formel-Injection


def test_csv_formel_injection_schutz_und_ruecknahme() -> None:
    """F-EXP-03/F-EXP-06: Textzellen werden entschaerft, Betrag/Datum nicht."""

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = make_session_factory(engine)()
    with session:
        session.add(Account(id=1, name="Girokonto", currency="EUR", source="datei"))
        session.commit()
        session.add(
            Transaction(
                account_id=1,
                status="gebucht",
                booking_date=date(2026, 1, 15),
                amount_cents=-1000,
                currency="EUR",
                counterparty="=SUM(A1:A2)",
                purpose="@cmd",
                note="-2+3",
                origin="csv",
                fingerprint=_to_fingerprint(1, {"counterparty": "=SUM(A1:A2)", "purpose": "@cmd"}),
                occurrence_index=0,
                search_text="",
            )
        )
        session.commit()
        result = _run(session, _csv_request(account_id=1))

    body = dict(zip(_parse(result.content)[0], _parse(result.content)[1], strict=True))
    assert body["Gegenpartei"] == "'=SUM(A1:A2)"
    assert body["Verwendungszweck"] == "'@cmd"
    assert body["Notiz"] == "'-2+3"
    # Negative Betraege behalten ihr Vorzeichen (kein Apostroph).
    assert body["Betrag"] == "-10,00"

    rows = load_rows(
        result.content.encode("utf-8"),
        kind="csv",
        options=CsvOptions(encoding="utf-8", delimiter=";", decimal_separator="comma", date_format="TT.MM.JJJJ"),
    )
    cleaned = strip_export_guard(rows)
    assert cleaned[0]["Gegenpartei"] == "=SUM(A1:A2)"
    assert cleaned[0]["Verwendungszweck"] == "@cmd"
    assert cleaned[0]["Notiz"] == "-2+3"


# ------------------------------------------------------------------ F-EXP-06


def _own_csv_template(session: Session) -> Any:
    """Liefert die geseedete Vorlage "Eigener CSV-Export"."""

    seed_default_templates(session)
    templates = {template.name: template for template in list_templates(session)}
    return templates[OWN_CSV_TEMPLATE_NAME]


def test_csv_roundtrip_service_ergibt_null_neue() -> None:
    """F-EXP-06: CSV-Export mit Standardprofil -> Import mit Vorlage = 0 neue."""

    source = _memory_session()
    with source:
        source.add(Account(id=1, name="Girokonto", currency="EUR", source="datei"))
        source.commit()
        for counterparty, amount, day in (
            ("=SUM(A1:A2)", -1000, date(2026, 1, 15)),
            ("Beispiel Markt GmbH", -4883, date(2026, 1, 16)),
        ):
            source.add(
                Transaction(
                    account_id=1,
                    status="gebucht",
                    booking_date=day,
                    amount_cents=amount,
                    currency="EUR",
                    counterparty=counterparty,
                    purpose="Einkauf",
                    counterparty_iban="DE02120300000000202051",
                    origin="csv",
                    fingerprint=_to_fingerprint(
                        1,
                        {
                            "booking_date": day,
                            "amount_cents": amount,
                            "counterparty": counterparty,
                            "purpose": "Einkauf",
                            "counterparty_iban": "DE02120300000000202051",
                        },
                    ),
                    occurrence_index=0,
                    search_text="",
                )
            )
        source.commit()
        exported = _run(source, _csv_request(account_id=1))

    template = _own_csv_template(source)
    rows = load_rows(
        exported.content.encode("utf-8"),
        kind="csv",
        options=CsvOptions(
            encoding=template.encoding,
            delimiter=template.delimiter,
            decimal_separator=template.decimal_separator,
            date_format=template.date_format,
        ),
    )
    rows = strip_export_guard(rows)

    # Re-Import in denselben Bestand: Fingerprint-Abgleich erkennt beide Zeilen
    # als vorhanden (F-DUP-01, F-EXP-06).
    with source:
        run = execute_import(1, rows, template.mapping, "csv", source)

    assert run.new_count == 0
    assert run.skipped_count == 2


def test_csv_roundtrip_ueber_rest_importkette(client: TestClient) -> None:
    """F-EXP-06: Standardprofil-CSV ueber den Import-Assistenten = 0 neue Buchungen."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="=SUM(A1:A2)", amount_cents=-1000, purpose="Einkauf")
    _add_transaction(client, account_id, counterparty="Beispiel Markt GmbH", amount_cents=-4883)

    with _session(client) as session:
        result = _run(session, _csv_request(account_id=account_id))
        template = _own_csv_template(session)
        template_id = template.id

    detected = client.post("/api/import/detect", content=encode_content(result.content, result.encoding))
    assert detected.status_code == 200
    token = detected.json()["token"]
    assert detected.json()["kind"] == "csv"

    executed = client.post(
        "/api/import/execute",
        json={"token": token, "account_id": account_id, "template_id": template_id},
    )
    assert executed.status_code == 200
    body = executed.json()
    assert body["new_count"] == 0
    assert body["skipped_count"] == 2


def test_csv_merged_buchung_ist_ausgeschlossen(client: TestClient) -> None:
    """F-DUP-04: Zusammengefuehrte Buchungen fehlen im CSV-Export."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="Treffer")
    _add_transaction(client, account_id, counterparty="Treffer", merged=True)

    with _session(client) as session:
        result = _run(session, _csv_request(account_id=account_id))

    assert len(_parse(result.content)) == 2


def test_build_filename_csv_endung() -> None:
    """F-EXP-03: Die CSV-Endung ist ``.csv``."""

    name = build_filename("Girokonto Müller", date(2026, 1, 1), date(2026, 11, 25), ExportFormat.CSV)
    assert name == "girokonto-mueller_2026-01-01_2026-11-25.csv"


def test_build_csv_direkt_ist_rfc_4180() -> None:
    """F-EXP-03: Zellen mit Trennzeichen/Anfuehrungszeichen werden gequotet."""

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = make_session_factory(engine)()
    with session:
        account = Account(name="Girokonto", currency="EUR", source="datei")
        session.add(account)
        session.commit()
        transaction = Transaction(
            account_id=account.id,
            status="gebucht",
            booking_date=date(2026, 1, 15),
            amount_cents=-1000,
            currency="EUR",
            counterparty='Markt "Nord"; GmbH',
            fingerprint="x" * 64,
            occurrence_index=0,
            search_text="",
        )
        content = build_csv(
            transactions=[transaction],
            accounts_by_id={account.id: account},
            options=CsvExportOptions(columns=("counterparty", "amount")),
            group_view=False,
        )
    assert _parse(content)[1][0] == 'Markt "Nord"; GmbH'
