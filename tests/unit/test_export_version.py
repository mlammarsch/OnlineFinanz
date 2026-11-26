"""Tests fuer die Versionskonvertierung des JSON-Exportformats (F-EXP-07).

Deckt die Pflicht aus F-EXP-07 ab: jede Formatversion hat eine Testdatei, die
bei jedem Testlauf importiert wird, eine fruehere Version wird schrittweise auf
die aktuelle konvertiert, und eine neuere Version als die der App wird mit
klarer Meldung abgelehnt. Die Beispieldatei ist zugleich ein gueltiges
Dokument nach F-EXP-04.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from finue.services import export_version
from finue.services.export_schema import parse_export_document
from finue.services.export_version import (
    CURRENT_EXPORT_VERSION,
    ImportFormatVersionError,
    register_converter,
    upgrade_to_current,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PATH = REPO_ROOT / "tests" / "fixtures" / "export_v1_sample.json"


@pytest.fixture()
def sample_v1() -> dict[str, Any]:
    """Laedt die v1-Testdatei bei jedem Test frisch von der Platte."""
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def test_beispiel_v1_ist_gueltiges_exportdokument(sample_v1: dict[str, Any]) -> None:
    """F-EXP-04/F-EXP-07: Die Testdatei ist ein vollstaendiges v1-Dokument."""
    document = parse_export_document(sample_v1)
    assert document.version == CURRENT_EXPORT_VERSION == 1
    assert len(document.accounts) >= 2
    assert len(document.transactions) >= 5
    assert any(transaction.note for transaction in document.transactions)
    assert any(transaction.checked for transaction in document.transactions)
    assert any(transaction.reconciled for transaction in document.transactions)
    assert any(transaction.external_ref for transaction in document.transactions)


def test_v1_datei_bleibt_bei_identitaet_unveraendert(sample_v1: dict[str, Any]) -> None:
    """F-EXP-07: Bei aktueller Version ist die Konvertierung die Identitaet."""
    original = deepcopy(sample_v1)
    result = upgrade_to_current(sample_v1)
    assert result == original
    assert result["version"] == CURRENT_EXPORT_VERSION
    assert sample_v1 == original


def test_hoehere_fantasieversion_wird_abgelehnt(sample_v1: dict[str, Any]) -> None:
    """F-EXP-07: Eine neuere Dateiversion als die App wird klar abgelehnt."""
    payload = {**sample_v1, "version": 99}
    with pytest.raises(ImportFormatVersionError) as excinfo:
        upgrade_to_current(payload)
    message = str(excinfo.value)
    assert "99" in message
    assert "neuer" in message
    assert str(CURRENT_EXPORT_VERSION) in message


def test_fehlende_version_wird_abgelehnt(sample_v1: dict[str, Any]) -> None:
    """F-EXP-07: Ohne Versionsfeld ist die Datei nicht importierbar."""
    payload = dict(sample_v1)
    del payload["version"]
    with pytest.raises(ImportFormatVersionError, match="'version' fehlt"):
        upgrade_to_current(payload)


@pytest.mark.parametrize("value", ["1", 1.0, True, None, [1]])
def test_ungueltiger_versionstyp_wird_abgelehnt(sample_v1: dict[str, Any], value: object) -> None:
    """F-EXP-07: Die Version muss eine ganze Zahl sein."""
    payload = {**sample_v1, "version": value}
    with pytest.raises(ImportFormatVersionError, match="ganze Zahl"):
        upgrade_to_current(payload)


@pytest.mark.parametrize("value", [0, -1])
def test_version_kleiner_eins_wird_abgelehnt(sample_v1: dict[str, Any], value: int) -> None:
    """F-EXP-07: Versionen beginnen bei 1."""
    payload = {**sample_v1, "version": value}
    with pytest.raises(ImportFormatVersionError, match="beginnen bei 1"):
        upgrade_to_current(payload)


def test_nicht_objekt_wird_abgelehnt() -> None:
    """F-EXP-07: Der Import erwartet ein JSON-Objekt mit Versionsfeld."""
    with pytest.raises(ImportFormatVersionError, match="JSON-Objekt"):
        upgrade_to_current(cast("dict[str, Any]", []))


def test_fehlender_konverter_wird_abgelehnt(sample_v1: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """F-EXP-07: Eine aeltere Version ohne registrierten Konverter ist ein klarer Fehler."""
    monkeypatch.setattr(export_version, "_CONVERTERS", {})
    monkeypatch.setattr(export_version, "CURRENT_EXPORT_VERSION", 2)
    with pytest.raises(ImportFormatVersionError, match="kein Konverter"):
        upgrade_to_current(sample_v1)


def test_konverterkette_wird_schrittweise_angewandt(sample_v1: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """F-EXP-07: Konverter werden als Kette v1 -> v2 -> v3 angewandt."""
    monkeypatch.setattr(export_version, "_CONVERTERS", {})
    monkeypatch.setattr(export_version, "CURRENT_EXPORT_VERSION", 3)

    calls: list[int] = []

    def to_v2(document: dict[str, Any]) -> dict[str, Any]:
        calls.append(2)
        upgraded = dict(document)
        upgraded["added_in_v2"] = True
        return upgraded

    def to_v3(document: dict[str, Any]) -> dict[str, Any]:
        calls.append(3)
        upgraded = dict(document)
        upgraded["added_in_v3"] = upgraded.pop("added_in_v2")
        return upgraded

    register_converter(1, 2, to_v2)
    register_converter(2, 3, to_v3)

    payload = deepcopy(sample_v1)
    result = upgrade_to_current(payload)

    assert result["version"] == 3
    assert result["added_in_v3"] is True
    assert "added_in_v2" not in result
    assert calls == [2, 3]
    assert payload["version"] == 1
    assert "added_in_v2" not in payload


@pytest.mark.parametrize(("from_version", "to_version"), [(1, 3), (2, 1), (0, 1)])
def test_register_converter_lehnt_ungueltige_schritte_ab(from_version: int, to_version: int) -> None:
    """F-EXP-07: Die Registry erlaubt nur Einzelschritte ab Version 1."""
    with pytest.raises(ValueError):
        register_converter(from_version, to_version, lambda document: document)


def test_register_converter_lehnt_nichtganzzahlige_versionen_ab() -> None:
    """F-EXP-07: Versionen sind ganze Zahlen."""
    with pytest.raises(TypeError):
        register_converter(cast("int", "1"), 2, lambda document: document)
