"""Read-only-Guard fuer FinTS: Positiv- und Negativfaelle.

Belegt, dass ``scripts/check_fints_readonly.py`` schreibende python-fints-Namen
per AST und Text findet, lesende Aufrufe, Kommentare und Docstrings nicht
meldet und die Namensliste aus dem in T-060 gepinnten python-fints 5.0.1
abgeleitet ist. Die Beispieldateien liegen ausschliesslich in ``tmp_path``,
es gibt keinen Netzzugriff und keine echten Kontodaten (D8).

Anforderungen: D2 (schreibende FinTS-Funktionen nie aufrufen), F-AUF-05
(Auftraege nur lesend anzeigen), F-AUF-07 (lesende Erweiterung), Review-
Checkliste "Auftraege nur lesend: kein Aufruf schreibender FinTS-
Geschaeftsvorfaelle".
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_fints_readonly.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_guard() -> object:
    """Laedt das Guard-Skript als Modul (scripts/ ist kein Paket)."""
    spec = importlib.util.spec_from_file_location("check_fints_readonly", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Vor exec_module registrieren, sonst findet dataclass das Modul nicht
    # (Python 3.12: sys.modules muss den Modulnamen bereits kennen).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _write_source(tmp_path: Path, relative: str, content: str) -> Path:
    """Schreibt eine Beispieldatei unter tmp_path und liefert ihren Pfad."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


POSITIVE_FAELLE = [
    pytest.param("client.sepa_transfer(account, pain)", "sepa_transfer", id="aufruf-sepa-transfer"),
    pytest.param(
        "client.simple_sepa_transfer(account, iban, bic, name, betrag)",
        "simple_sepa_transfer",
        id="aufruf-simple-sepa-transfer",
    ),
    pytest.param("client.sepa_debit(account, pain)", "sepa_debit", id="aufruf-sepa-debit"),
    pytest.param("client.simple_sepa_debit(account, pain)", "simple_sepa_debit", id="aufruf-simple-sepa-debit"),
    pytest.param("from fints.client import sepa_transfer", "sepa_transfer", id="import-client-methode"),
    pytest.param("from fints.segments.transfer import HKCCS1", "HKCCS1", id="import-ueberweisungssegment"),
    pytest.param("from fints.segments.debit import HKDSE2", "HKDSE2", id="import-lastschriftsegment"),
    pytest.param("segment = HKIPZ1(account=account)", "HKIPZ1", id="konstruktion-segment"),
    pytest.param("methode = getattr(client, 'sepa_transfer')", "sepa_transfer", id="getattr-namens-string"),
    pytest.param(
        "def sende(client, account, pain):\n    return client.sepa_debit(account, pain)\n",
        "sepa_debit",
        id="aufruf-in-funktion",
    ),
]

NEGATIVE_FAELLE = [
    pytest.param("client.get_balance(account)", id="lesend-saldo"),
    pytest.param("client.get_transactions_xml(account, start, ende)", id="lesend-camt"),
    pytest.param("client.get_transactions(account, include_pending=True)", id="lesend-mt940"),
    pytest.param("client.get_scheduled_debits(account)", id="lesend-lastschriftbestand"),
    pytest.param("from fints.segments.debit import HKDBS1", id="lesend-bestandssegment"),
    pytest.param("segment = HKDMB1(account=account)", id="lesend-bestandssegment-konstruktion"),
    pytest.param(
        "# client.sepa_transfer(account, pain) wird bewusst nie aufgerufen\nname = 'friedlich'\n",
        id="kommentar-mention",
    ),
    pytest.param(
        '"""Kein Aufruf von sepa_transfer oder HKCCS1 in diesem Modul."""\n',
        id="docstring-prosa",
    ),
]


@pytest.mark.parametrize("code, erwartet", POSITIVE_FAELLE)
def test_ast_und_text_finden_schreibende_namen(tmp_path: Path, code: str, erwartet: str) -> None:
    """Positive Faelle: jeder schreibende Name erzeugt eine Fundstelle."""
    ziel = _write_source(tmp_path, "backend/finue/sync.py", code)
    findings = guard.scan_file(ziel)
    assert findings, f"kein Befund fuer {code!r}"
    assert erwartet in {finding.name for finding in findings}
    assert all(finding.kind != "Lesefehler" for finding in findings)


@pytest.mark.parametrize("code", NEGATIVE_FAELLE)
def test_lesende_aufrufe_und_texte_bleiben_sauber(tmp_path: Path, code: str) -> None:
    """Negative Faelle: lesende Aufrufe, Kommentare und Docstrings sind erlaubt."""
    ziel = _write_source(tmp_path, "backend/finue/lesend.py", code)
    assert guard.scan_file(ziel) == []


def test_textpruefung_findet_namen_ohne_parsebaren_ast(tmp_path: Path) -> None:
    """Nicht parsebarer Code wird per Text-/Zeilenpruefung erkannt."""
    kaputt = "wert = 'nicht beendet\nclient.sepa_transfer(account, pain)\n"
    ziel = _write_source(tmp_path, "backend/finue/kaputt.py", kaputt)

    findings = guard.scan_file(ziel)

    assert findings, "Textpruefung hat den schreibenden Aufruf nicht gefunden"
    assert {finding.name for finding in findings} == {"sepa_transfer"}
    assert {finding.line for finding in findings} == {2}


def test_cli_exit_1_mit_fundstelle(tmp_path: Path) -> None:
    """CLI endet bei einem Fund mit Exit 1 und nennt Datei, Zeile und Name."""
    _write_source(tmp_path, "backend/finue/sync.py", "client.sepa_transfer(account, pain)\n")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "backend")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert proc.returncode == 1
    assert "FINTS_READONLY_FEHLER" in proc.stdout
    assert "sepa_transfer" in proc.stdout
    assert "sync.py:1" in proc.stdout


def test_cli_exit_0_auf_sauberem_beispiel(tmp_path: Path) -> None:
    """CLI endet ohne Fund mit Exit 0 und einer OK-Zeile."""
    _write_source(
        tmp_path,
        "backend/finue/lesend.py",
        "client.get_balance(account)\nclient.get_transactions_xml(account, start, ende)\n",
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "backend")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FINTS_READONLY_OK" in proc.stdout


def test_echtes_backend_ist_sauber() -> None:
    """Der Guard laeuft auf dem echten backend/ ohne Fund durch."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("FINTS_READONLY_OK")


def test_guard_ist_in_stage_m3_registriert() -> None:
    """validate.py fuehrt den Guard in der M3-Stufe auf (ohne T-083-Integration)."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate.py"), "--list", "m3"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FinTS Read-only-Guard" in proc.stdout


def test_client_namensliste_stammt_aus_installierter_version() -> None:
    """Jede schreibende oeffentliche Client-Methode von 5.0.1 ist verboten."""
    from fints.client import FinTS3Client

    schreibend = {
        name
        for name in dir(FinTS3Client)
        if not name.startswith("_") and not name.startswith("get_") and ("transfer" in name or "debit" in name)
    }

    assert schreibend, "keine schreibenden Client-Methoden gefunden - Bibliotheksversion pruefen"
    assert schreibend <= guard.FORBIDDEN_CLIENT_METHODS


def test_dod_namen_sind_vollstaendig_enthalten() -> None:
    """Die vier in der Task-Definition genannten Client-Methoden sind verboten."""
    erwartet = {"sepa_transfer", "simple_sepa_transfer", "sepa_debit", "simple_sepa_debit"}
    assert erwartet <= guard.FORBIDDEN_CLIENT_METHODS


def test_segmentnamensliste_stammt_aus_ueberweisung_und_lastschrift() -> None:
    """Verbotene Segmente sind exakt die schreibenden Klassen der Bibliothek."""
    import fints.segments.debit as debit
    import fints.segments.transfer as transfer
    from fints.segments.base import FinTS3Segment

    def schreibende_klassen(module: object) -> set[str]:
        ergebnis: set[str] = set()
        for name, obj in vars(module).items():
            # Nur Command-Segmente (HK...) senden Auftraege; HI... sind
            # Antwort-/Parametersegmente und damit nicht schreibend.
            if not name.startswith("HK"):
                continue
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            if not issubclass(obj, FinTS3Segment):
                continue
            doc = (inspect.getdoc(obj) or "").lower()
            # "Einreichung"/"einreichen" und "Ueberweisung" bezeichnen das
            # Absenden eines Auftrags; "anfordern" (Bestand) bleibt aussen vor.
            if "berweisung" in doc or "einreich" in doc:
                ergebnis.add(name)
        return ergebnis

    erwartet = schreibende_klassen(transfer) | schreibende_klassen(debit)

    assert erwartet, "keine schreibenden Segmente gefunden - Bibliotheksversion pruefen"
    assert erwartet == set(guard.FORBIDDEN_SEGMENT_CLASSES)


def test_lesende_bestandssegmente_sind_erlaubt() -> None:
    """HKDBS1/2 und HKDMB1 fordern nur Bestaende an und bleiben sauber."""
    assert set(guard.ALLOWED_READONLY_SEGMENT_CLASSES) == {"HKDBS1", "HKDBS2", "HKDMB1"}
    assert guard.ALLOWED_READONLY_SEGMENT_CLASSES.isdisjoint(guard.FORBIDDEN_NAMES)


def test_namensliste_ist_an_gepinnte_version_gebunden() -> None:
    """pyproject und installierte Version entsprechen der Ableitungsversion."""
    daten = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    abhaengigkeiten = daten["project"]["dependencies"]

    assert f"fints=={guard.PINNED_FINTS_VERSION}" in abhaengigkeiten
    assert importlib.metadata.version("fints") == guard.PINNED_FINTS_VERSION
