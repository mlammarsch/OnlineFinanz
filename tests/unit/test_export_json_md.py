"""Tests fuer den Export-Service und ``POST /api/export`` (T-097).

Referenziert F-EXP-01, F-EXP-02, F-EXP-04 und F-EXP-05 sowie die
Regression F-EXP-07 (Import des eigenen JSON erkennt weiterhin die v1).
Abgedeckt:

- Dateiname: ``slug(Konto|Gruppe)_von_bis.ext`` in ASCII, Umlaute transkribiert,
  bei ``alle`` der Zeitraum aus Minimum/Maximum der Daten (F-EXP-02).
- JSON: Schema-Validierung gegen ``docs/schemas/export-v1.schema.json``
  (Feldmengen inkl. Konten ohne Zugangsdaten) und verlustfreier Re-Import
  (F-EXP-04/06-Regression).
- Markdown: Golden-File mit ``|``, Backslash und Zeilenumbruch, deutsches
  Zahlen- und Datumsformat, Gruppenansicht mit Kontospalte (F-EXP-05).
- API: ``Content-Disposition``, Markdown als ``text/markdown`` und als
  ``text/plain`` fuer die Zwischenablage, Auswahlumfang, Grenze (413),
  ungueltiger Umfang (422).
- Zusammengefuehrte Buchungen sind ausgeschlossen (F-DUP-04).

Alle Konten und Buchungen sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten, keine Bankzugriffe und keine externen Schreibaktionen.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.config import Settings
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.main import create_app
from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.duplicate import compute_fingerprint
from finue.services.export import (
    ExportFormat,
    ExportLimitExceeded,
    ExportRequest,
    ExportScope,
    build_filename,
    run_export,
    slugify,
)
from finue.services.export_schema import ExportDocumentV1
from finue.services.import_run import execute_import
from finue.services.search import SearchPeriod, SortDirection, SortField
from finue.services.search_text import build_search_text

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "export-v1.schema.json"

NOW = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 1, 20)

_occurrence_counts: dict[tuple[int, str], int] = {}


def _session(client: TestClient) -> Session:
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _add_account(client: TestClient, name: str = "Konto", **overrides: object) -> int:
    """Legt ein Konto an und liefert seine ID (erfundene Testdaten, D8)."""

    fields: dict[str, object] = {"name": name, "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    with _session(client) as session:
        account = Account(**fields)
        session.add(account)
        session.commit()
        return account.id


def _add_group(client: TestClient, name: str = "Gruppe") -> int:
    with _session(client) as session:
        group = AccountGroup(name=name)
        session.add(group)
        session.commit()
        return group.id


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
    """Legt eine Testbuchung mit gefuellter Suchspalte an (F-SUC-01)."""

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
        # Der Bestand fuehrt den Vorkommen-Index 0-basiert (F-DUP-02, Demo-Import).
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
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def _run(session: Session, request: ExportRequest) -> Any:
    return run_export(session, request, now=NOW, today=TODAY, max_rows=1000)


# ------------------------------------------------------------------ F-EXP-02


def test_slugify_transkribiert_umlaute_und_ascii() -> None:
    """F-EXP-02: Umlaute werden transkribiert, der Rest wird ASCII."""

    assert slugify("Girokonto Müller") == "girokonto-mueller"
    assert slugify("Über-Ärger & Straße") == "ueber-aerger-strasse"
    assert slugify("  ") == "export"


def test_build_filename_ascii_mit_zeitraum() -> None:
    """F-EXP-02: ``slug_von_bis.ext`` in ASCII mit JSON- und Markdown-Endung."""

    name = build_filename("Girokonto Müller", date(2026, 1, 1), date(2026, 11, 25), ExportFormat.JSON)
    assert name == "girokonto-mueller_2026-01-01_2026-11-25.json"
    assert name.isascii()
    markdown = build_filename("Girokonto", date(2026, 1, 1), date(2026, 11, 25), ExportFormat.MARKDOWN)
    assert markdown == "girokonto_2026-01-01_2026-11-25.md"


def test_dateiname_bei_alle_aus_min_max(client: TestClient) -> None:
    """F-EXP-02: Bei Zeitraum ``alle`` stammt der Dateiname-Zeitraum aus den Daten."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, booking_date=date(2025, 3, 2))
    _add_transaction(client, account_id, booking_date=date(2026, 4, 9))

    with _session(client) as session:
        result = _run(
            session,
            ExportRequest(scope=ExportScope.ACCOUNT, account_id=account_id, period=SearchPeriod.ALL),
        )

    assert result.filename == "girokonto_2025-03-02_2026-04-09.json"


# ------------------------------------------------------------------ F-EXP-04


def _schema_properties(section: str) -> set[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return set(schema["$defs"][section]["properties"])


def test_json_export_validiert_gegen_schema(client: TestClient) -> None:
    """F-EXP-04: JSON nach Schema v1, Konten ohne Zugangsdaten."""

    account_id = _add_account(client, "Girokonto", iban="DE89 3704 0044 0532 0130 00")
    _add_transaction(
        client,
        account_id,
        counterparty="Beispiel Markt GmbH",
        purpose="Einkauf",
        amount_cents=-4883,
        checked=True,
        checked_at=NOW,
        checked_origin="ui",
    )

    with _session(client) as session:
        result = _run(session, ExportRequest(scope=ExportScope.ACCOUNT, account_id=account_id, period=SearchPeriod.ALL))

    assert result.media_type == "application/json"
    payload = json.loads(result.content)
    assert payload["format"] == "finanzuebersicht"
    assert payload["version"] == 1

    # Gegen das mitgelieferte Schema: die Feldmengen stimmen exakt ueberein.
    assert set(payload["accounts"][0]) == _schema_properties("account")
    assert set(payload["transactions"][0]) == _schema_properties("transaction")
    assert set(payload["filter"]) == _schema_properties("filter")
    # Keine Zugangsdaten (N-SEC-01/02): nur die Schemafelder des Kontos.
    assert "bank_access_id" not in payload["accounts"][0]
    assert payload["accounts"][0]["iban"] == "DE89370400440532013000"

    # Was das Schema-Modell akzeptiert, ist die maszgebliche Validierung.
    ExportDocumentV1.model_validate(payload)


def _signature(transaction: Transaction) -> tuple[Any, ...]:
    return (
        transaction.fingerprint,
        transaction.occurrence_index,
        transaction.booking_date,
        transaction.value_date,
        transaction.amount_cents,
        transaction.currency,
        transaction.counterparty,
        transaction.counterparty_iban,
        transaction.purpose,
        transaction.note,
        transaction.status,
        transaction.origin,
        transaction.external_ref,
        transaction.checked,
        transaction.checked_at,
        transaction.checked_origin,
        transaction.reconciled,
        transaction.reconciled_at,
        transaction.reconciled_origin,
        transaction.merged,
    )


def test_json_export_reimport_ergibt_denselben_stand() -> None:
    """F-EXP-04/06: Re-Import des JSON-Exports in eine frische DB ergibt denselben Stand."""

    session = _memory_session()
    with session:
        session.add(Account(id=1, name="Girokonto", currency="EUR", source="datei"))
        session.commit()
        session.add(
            Transaction(
                account_id=1,
                status="gebucht",
                booking_date=date(2026, 1, 15),
                value_date=date(2026, 1, 16),
                amount_cents=-4883,
                currency="EUR",
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
                origin="fints",
                external_ref="ACTUAL-2026-0001",
                fingerprint=_to_fingerprint(
                    1,
                    {
                        "booking_date": date(2026, 1, 15),
                        "value_date": date(2026, 1, 16),
                        "amount_cents": -4883,
                        "counterparty": "Beispiel Markt GmbH",
                        "counterparty_iban": "DE02120300000000202051",
                        "purpose": "Einkauf",
                    },
                ),
                occurrence_index=0,
                merged=False,
                search_text=build_search_text(
                    counterparty="Beispiel Markt GmbH",
                    purpose="Einkauf",
                    note="Wocheneinkauf",
                    counterparty_iban="DE02120300000000202051",
                    external_ref="ACTUAL-2026-0001",
                ),
            )
        )
        session.commit()
        before = [row for row in session.scalars(select(Transaction).order_by(Transaction.id)).all()]
        exported = _run(session, ExportRequest(scope=ExportScope.ACCOUNT, account_id=1, period=SearchPeriod.ALL))
        signatures_before = sorted(_signature(row) for row in before)

    payload = json.loads(exported.content)

    target = _memory_session()
    with target:
        target.add(Account(id=1, name="Girokonto", currency="EUR", source="datei"))
        target.commit()
        execute_import(1, payload, None, "own_json", target)
        imported = [row for row in target.scalars(select(Transaction).order_by(Transaction.id)).all()]

    assert sorted(_signature(row) for row in imported) == signatures_before


# ------------------------------------------------------------------ F-EXP-05


def _golden_markdown() -> str:
    return (
        "# Export: Girokonto\n"
        "\n"
        "Zeitraum: 15.01.2026 - 16.01.2026\n"
        "Anzahl: 2\n"
        "Summe: 2.451,17 EUR\n"
        "\n"
        "| Datum | Gegenpartei | Verwendungszweck | Betrag | Notiz |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 15.01.2026 | Beispiel Markt GmbH | Einkauf \\| Sonder<br>Zeile | -48,83 EUR | Wochen\\\\einkauf |\n"
        "| 16.01.2026 | Arbeitgeber | Gehalt | 2.500,00 EUR |  |\n"
        "\n"
        "**Summe: 2.451,17 EUR**\n"
    )


def test_markdown_golden_mit_sonderzeichen(client: TestClient) -> None:
    """F-EXP-05: Golden-File mit ``|``, Backslash, Zeilenumbruch, deutschem Format."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(
        client,
        account_id,
        booking_date=date(2026, 1, 15),
        amount_cents=-4883,
        counterparty="Beispiel Markt GmbH",
        purpose="Einkauf | Sonder\nZeile",
        note="Wochen\\einkauf",
    )
    _add_transaction(
        client,
        account_id,
        booking_date=date(2026, 1, 16),
        amount_cents=250000,
        counterparty="Arbeitgeber",
        purpose="Gehalt",
    )

    with _session(client) as session:
        result = _run(
            session,
            ExportRequest(
                format=ExportFormat.MARKDOWN,
                scope=ExportScope.ACCOUNT,
                account_id=account_id,
                period=SearchPeriod.ALL,
            ),
        )

    assert result.media_type == "text/markdown"
    assert result.filename == "girokonto_2026-01-15_2026-01-16.md"
    assert result.content == _golden_markdown()


def test_markdown_gruppenansicht_hat_kontospalte(client: TestClient) -> None:
    """F-EXP-05: Die Gruppenansicht fuehrt zusaetzlich eine Kontospalte."""

    group_id = _add_group(client, "Haushalt")
    first = _add_account(client, "Girokonto", group_id=group_id)
    second = _add_account(client, "Tagesgeld", group_id=group_id)
    _add_transaction(client, first, counterparty="Markt", amount_cents=-100)
    _add_transaction(client, second, counterparty="Bank", amount_cents=250)

    with _session(client) as session:
        result = _run(
            session,
            ExportRequest(
                format=ExportFormat.MARKDOWN,
                scope=ExportScope.GROUP,
                group_id=group_id,
                period=SearchPeriod.ALL,
                sort=SortField.DATE,
                direction=SortDirection.ASC,
            ),
        )

    assert "| Datum | Konto | Gegenpartei | Verwendungszweck | Betrag | Notiz |" in result.content
    assert "| 15.01.2026 | Girokonto | Markt |  | -1,00 EUR |  |" in result.content
    assert result.filename == "haushalt_2026-01-15_2026-01-15.md"


def test_merged_buchung_ist_ausgeschlossen(client: TestClient) -> None:
    """F-DUP-04: Zusammengefuehrte Buchungen fehlen in JSON und Markdown."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="Treffer")
    _add_transaction(client, account_id, counterparty="Treffer", merged=True)

    with _session(client) as session:
        as_json = _run(
            session, ExportRequest(scope=ExportScope.ACCOUNT, account_id=account_id, period=SearchPeriod.ALL)
        )
        as_md = _run(
            session,
            ExportRequest(
                format=ExportFormat.MARKDOWN,
                scope=ExportScope.ACCOUNT,
                account_id=account_id,
                period=SearchPeriod.ALL,
            ),
        )

    assert len(json.loads(as_json.content)["transactions"]) == 1
    assert "Anzahl: 1" in as_md.content


# ------------------------------------------------------------------ Grenze


def test_grenze_bei_zu_vielen_buchungen(client: TestClient) -> None:
    """F-EXP-01: Mehr Buchungen als erlaubt loesen ExportLimitExceeded aus."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id)
    _add_transaction(client, account_id)

    with _session(client) as session:
        try:
            run_export(
                session,
                ExportRequest(scope=ExportScope.ACCOUNT, account_id=account_id, period=SearchPeriod.ALL),
                now=NOW,
                today=TODAY,
                max_rows=1,
            )
        except ExportLimitExceeded as exc:
            assert "Grenze" in str(exc)
        else:  # pragma: no cover - Fehlerpfad muss greifen
            raise AssertionError("ExportLimitExceeded wurde nicht ausgeloest.")


# ------------------------------------------------------------------ API


def test_api_export_json_mit_content_disposition(client: TestClient) -> None:
    """F-EXP-01/02: ``POST /api/export`` liefert die Datei mit Content-Disposition."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, booking_date=date(2026, 1, 15), counterparty="Markt")

    response = client.post(
        "/api/export",
        json={"format": "json", "scope": "account", "account_id": account_id, "period": "all"},
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="girokonto_2026-01-15_2026-01-15.json"'
    assert response.headers["content-type"].startswith("application/json")
    assert json.loads(response.text)["version"] == 1


def test_api_export_markdown_datei_und_zwischenablage(client: TestClient) -> None:
    """F-EXP-05: Markdown als Datei (text/markdown) und als text/plain fuer die Zwischenablage."""

    account_id = _add_account(client, "Girokonto")
    _add_transaction(client, account_id, counterparty="Markt")

    payload = {"format": "md", "scope": "account", "account_id": account_id, "period": "all"}
    as_file = client.post("/api/export", json=payload)
    assert as_file.status_code == 200
    assert as_file.headers["content-type"].startswith("text/markdown")
    assert "content-disposition" in as_file.headers
    assert as_file.text.startswith("# Export: Girokonto")

    clipboard = client.post("/api/export", params={"download": "false"}, json=payload)
    assert clipboard.status_code == 200
    assert clipboard.headers["content-type"].startswith("text/plain")
    assert "content-disposition" not in clipboard.headers
    assert clipboard.text.startswith("# Export: Girokonto")


def test_api_export_auswahl(client: TestClient) -> None:
    """F-EXP-01: Ein Umfang ``selection`` exportiert nur die gewaehlten IDs."""

    account_id = _add_account(client, "Girokonto")
    first = _add_transaction(client, account_id, counterparty="Erste")
    _add_transaction(client, account_id, counterparty="Zweite")
    third = _add_transaction(client, account_id, counterparty="Dritte")

    response = client.post(
        "/api/export",
        json={"format": "json", "scope": "selection", "transaction_ids": [first, third]},
    )

    assert response.status_code == 200
    exported = json.loads(response.text)["transactions"]
    assert {item["id"] for item in exported} == {first, third}


def test_api_export_grenze_liefert_413(tmp_path: Path) -> None:
    """F-EXP-01: Eine zu grosse Buchungsmenge wird als 413 mit Klartext gemeldet."""

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'grenze.db').as_posix()}",
        data_dir=tmp_path,
        export_max_rows=1,
    )
    with TestClient(create_app(settings)) as client:
        account_id = _add_account(client, "Girokonto")
        _add_transaction(client, account_id)
        _add_transaction(client, account_id)
        response = client.post(
            "/api/export",
            json={"format": "json", "scope": "account", "account_id": account_id, "period": "all"},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.json()["details"]["limit"] == 1


def test_api_export_ungueltiger_umfang_liefert_422(client: TestClient) -> None:
    """F-EXP-01: ``scope=account`` ohne ``account_id`` liefert deutschen 422-Fehler."""

    response = client.post("/api/export", json={"format": "json", "scope": "account"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "account_id" in response.json()["message"]
