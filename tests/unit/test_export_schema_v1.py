"""Tests fuer das JSON-Exportformat v1.

Referenziert F-EXP-04 (Kopf, Konten ohne Zugangsdaten, vollstaendige Buchungen,
Betraege als Dezimal-String mit Punkt, Datum ISO, verlustfrei), F-EXP-07
(Versionierung, neuere Version ablehnen) und F-ABS-01 (Pruef- und
Abstimmungskennzeichen mit Zeitpunkt und Herkunft) sowie N-SEC-01/N-SEC-02
(keine Zugangsdaten im Export).

Die Strukturpruefung des JSON-Schemas kommt ohne die externe Bibliothek
``jsonschema`` aus, weil sie nicht in ``pyproject.toml`` steht. Sie prueft
Pflichtfelder, Typen, `$ref`-Ziele und die exportierten Feldmengen.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from finue.services.export_schema import (
    EXPORT_FORMAT_NAME,
    SUPPORTED_EXPORT_VERSION,
    ExportAccountV1,
    ExportDocumentV1,
    ExportFormatError,
    ExportTransactionV1,
    UnsupportedExportVersionError,
    ensure_supported_version,
    parse_export_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "export-v1.schema.json"
EXAMPLE_PATH = REPO_ROOT / "docs" / "schemas" / "export-v1.example.json"

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
ALLOWED_JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
FORBIDDEN_CREDENTIAL_FIELDS = {
    "bank_access",
    "bank_access_id",
    "login_name",
    "password",
    "pin",
    "pin_encrypted",
    "tan_method",
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    """Laedt das mitgelieferte JSON-Schema."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def example_payload() -> dict[str, Any]:
    """Laedt die schema-valide Beispieldatei."""
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _iter_nodes(node: Any):
    """Liefert alle Objekte eines verschachtelten JSON-Dokuments."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_nodes(value)


def _resolve_ref(root: dict[str, Any], ref: str) -> Any:
    """Loest einen lokalen JSON-Pointer der Form ``#/$defs/name`` auf."""
    assert ref.startswith("#/"), f"Nur lokale $refs erlaubt: {ref}"
    current: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        assert isinstance(current, dict) and part in current, f"$ref-Ziel fehlt: {ref}"
        current = current[part]
    return current


def _replace(payload: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    """Erzeugt eine tiefe Kopie und ersetzt den Wert am angegebenen Pfad."""
    data = deepcopy(payload)
    cursor: Any = data
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return data


def test_schema_ist_draft_2020_12_und_strukturell_gueltig(schema: dict[str, Any]) -> None:
    """F-EXP-04: Das Schema ist Draft 2020-12 und strukturell vollstaendig."""
    assert schema["$schema"] == DRAFT_2020_12
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "format",
        "version",
        "created_at",
        "filter",
        "period",
        "accounts",
        "transactions",
    }
    assert set(schema["$defs"]) >= {"filter", "period", "account", "transaction"}

    for node in _iter_nodes(schema):
        if "$ref" in node:
            _resolve_ref(schema, node["$ref"])
        raw_type = node.get("type")
        if raw_type is not None:
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            assert set(types) <= ALLOWED_JSON_TYPES, f"Unbekannter Typ: {raw_type}"
        if "required" in node:
            properties = node.get("properties", {})
            for field in node["required"]:
                assert field in properties, f"Pflichtfeld ohne Definition: {field}"


def test_schema_bildet_kopf_und_version_ab(schema: dict[str, Any]) -> None:
    """F-EXP-04/F-EXP-07: Formatname und Version sind fest, Zeitstempel ist date-time."""
    properties = schema["properties"]
    assert properties["format"]["const"] == "finanzuebersicht"
    assert properties["version"]["const"] == 1
    assert properties["created_at"]["type"] == "string"
    assert properties["created_at"]["format"] == "date-time"


def test_schema_enthaelt_keine_zugangsdaten(schema: dict[str, Any]) -> None:
    """N-SEC-01/N-SEC-02: Kontostammdaten enthalten keine Zugangsdaten."""
    account_properties = set(schema["$defs"]["account"]["properties"])
    assert account_properties == {"id", "name", "iban", "currency", "account_type"}
    assert not account_properties & FORBIDDEN_CREDENTIAL_FIELDS
    assert schema["$defs"]["account"]["additionalProperties"] is False


def test_schema_bildet_buchungsfelder_und_amount_ab(schema: dict[str, Any]) -> None:
    """F-EXP-04: Buchungen enthalten Status, Kennzeichen, Referenz, Betrag und Datum."""
    transaction = schema["$defs"]["transaction"]
    properties = transaction["properties"]
    expected = {
        "id",
        "account_id",
        "status",
        "booking_date",
        "value_date",
        "counterparty",
        "counterparty_iban",
        "purpose",
        "note",
        "checked",
        "checked_at",
        "checked_origin",
        "reconciled",
        "reconciled_at",
        "reconciled_origin",
        "origin",
        "external_ref",
        "fingerprint",
        "occurrence_index",
        "merged",
        "amount",
        "currency",
    }
    assert expected <= set(properties)
    assert properties["amount"]["type"] == "string"
    assert properties["amount"]["pattern"].endswith(r"\d{2}$")
    assert r"\." in properties["amount"]["pattern"]
    assert properties["booking_date"]["format"] == "date"
    assert properties["checked_origin"]["enum"] == ["ui", "mcp", "import", None]
    assert properties["reconciled_origin"]["enum"] == ["ui", "mcp", "import", None]


def test_beispieldatei_validiert_gegen_pydantic_modell(example_payload: dict[str, Any]) -> None:
    """F-EXP-04: Die Beispieldatei ist schema-valide und wird korrekt gelesen."""
    document = ExportDocumentV1.model_validate(example_payload)
    assert document.format == EXPORT_FORMAT_NAME
    assert document.version == SUPPORTED_EXPORT_VERSION
    assert [account.id for account in document.accounts] == [1, 2]
    assert [transaction.id for transaction in document.transactions] == [101, 102]

    first = document.transactions[0]
    assert first.amount == "-48.83"
    assert first.booking_date == date(2026, 1, 15)
    assert first.checked is True
    assert first.checked_origin == "ui"
    assert first.checked_at is not None
    assert first.checked_at.tzinfo is not None
    assert first.reconciled is True
    assert first.reconciled_origin == "import"

    second = document.transactions[1]
    assert second.checked is False
    assert second.checked_at is None
    assert second.reconciled is False
    assert second.reconciled_at is None


def test_parse_export_document_liefert_gleiches_modell(example_payload: dict[str, Any]) -> None:
    """F-EXP-04: Der oeffentliche Einstieg prueft Version und validiert v1."""
    assert parse_export_document(example_payload) == ExportDocumentV1.model_validate(example_payload)


def test_modell_serialisiert_iso_und_dezimal_strings(example_payload: dict[str, Any]) -> None:
    """F-EXP-04: Serialisierung behaelt Punkt-Dezimalbetraege und ISO-Daten."""
    document = ExportDocumentV1.model_validate(example_payload)
    serialized = document.model_dump(mode="json")
    transaction = serialized["transactions"][0]
    assert transaction["amount"] == "-48.83"
    assert transaction["booking_date"] == "2026-01-15"
    assert isinstance(transaction["checked_at"], str)
    assert transaction["currency"] == "EUR"
    ExportDocumentV1.model_validate(serialized)


def test_pydantic_schema_hat_dieselben_kernconstraints() -> None:
    """F-EXP-04: Pydantic-Modell und Schema stimmen bei den Kernconstraints ueberein."""
    root = ExportDocumentV1.model_json_schema()
    assert root["additionalProperties"] is False
    assert root["properties"]["format"]["const"] == "finanzuebersicht"
    assert root["properties"]["version"]["const"] == 1

    transaction = ExportTransactionV1.model_json_schema()
    assert transaction["additionalProperties"] is False
    assert transaction["properties"]["amount"]["type"] == "string"
    assert "pattern" in transaction["properties"]["amount"]
    assert transaction["properties"]["booking_date"]["format"] == "date"


def test_modelle_enthalten_keine_zugangsdaten() -> None:
    """N-SEC-01/N-SEC-02: Die Modelle exportieren keine Zugangsdaten."""
    assert set(ExportAccountV1.model_fields) == {"id", "name", "iban", "currency", "account_type"}
    assert not FORBIDDEN_CREDENTIAL_FIELDS & set(ExportDocumentV1.model_fields)
    assert not FORBIDDEN_CREDENTIAL_FIELDS & set(ExportAccountV1.model_fields)


@pytest.mark.parametrize(
    "path,value",
    [
        (("version",), "1"),
        (("version",), 2),
        (("created_at",), "2026-11-26T10:41:13"),
        (("filter", "scope"), "unknown"),
        (("filter", "account_ids"), [1, 1]),
        (("period", "start"), "2026-1-1"),
        (("period", "start"), "2026-02-30"),
        (("period", "start"), "2026-12-31"),
        (("transactions", 0, "amount"), "12,34"),
        (("transactions", 0, "amount"), 12.34),
        (("transactions", 0, "amount"), "1234"),
        (("transactions", 0, "amount"), "12.345"),
        (("transactions", 0, "booking_date"), "15.01.2026"),
        (("transactions", 0, "booking_date"), "20260115"),
        (("transactions", 0, "currency"), "eur"),
        (("transactions", 0, "status"), "offen"),
        (("transactions", 0, "fingerprint"), "x" * 64),
        (("transactions", 0, "occurrence_index"), 0),
        (("transactions", 0, "id"), 0),
        (("transactions", 0, "checked_at"), None),
        (("transactions", 1, "checked_at"), "2026-11-26T10:41:13Z"),
        (("transactions", 1, "checked_origin"), "ui"),
        (("transactions", 0, "reconciled_at"), None),
        (("transactions", 0, "account_id"), 999),
        (("accounts", 0, "name"), ""),
        (("accounts", 0, "pin"), "1234"),
        (("accounts", 0, "currency"), "eur"),
        (("accounts", 1, "id"), 1),
        (("transactions", 1, "id"), 101),
    ],
)
def test_ungueltige_dokumente_werden_abgelehnt(
    example_payload: dict[str, Any], path: tuple[Any, ...], value: Any
) -> None:
    """F-EXP-04: Fehlerhafte Werte und inkonsistente Kennzeichen werden abgelehnt."""
    with pytest.raises(ValidationError):
        ExportDocumentV1.model_validate(_replace(example_payload, path, value))


@pytest.mark.parametrize("value", ["1", 1.0, True, None])
def test_version_muss_ganzzahl_sein(value: object) -> None:
    """F-EXP-07: Eine String- oder Boolean-Version ist kein gueltiges Format."""
    with pytest.raises(ExportFormatError):
        ensure_supported_version(value)


def test_version_null_ist_ungueltig() -> None:
    """F-EXP-07: Versionen beginnen bei 1."""
    with pytest.raises(ExportFormatError, match="mindestens 1"):
        ensure_supported_version(0)


def test_unterstuetzte_version_wird_zurueckgegeben() -> None:
    """F-EXP-07: Die aktuelle Version ist zulaessig."""
    assert ensure_supported_version(SUPPORTED_EXPORT_VERSION) == SUPPORTED_EXPORT_VERSION


def test_neuere_version_wird_mit_klarer_meldung_abgelehnt(example_payload: dict[str, Any]) -> None:
    """F-EXP-07: Eine neuere Dateiversion wird vor der Schemavalidierung abgelehnt."""
    payload = _replace(example_payload, ("version",), SUPPORTED_EXPORT_VERSION + 1)
    with pytest.raises(UnsupportedExportVersionError) as excinfo:
        parse_export_document(payload)
    message = str(excinfo.value)
    assert f"Version {SUPPORTED_EXPORT_VERSION + 1}" in message
    assert f"Version {SUPPORTED_EXPORT_VERSION}" in message
    assert "neuer" in message


def test_fehlende_oder_falsche_wurzel_wird_abgelehnt() -> None:
    """F-EXP-04: Der Import erwartet ein JSON-Objekt mit Versionsfeld."""
    with pytest.raises(ExportFormatError, match="version"):
        parse_export_document({})
    with pytest.raises(ExportFormatError, match="JSON-Objekt"):
        parse_export_document([])  # type: ignore[arg-type]
