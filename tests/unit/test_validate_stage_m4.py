"""M4-Validierungsstufe: Formatcheck, type-ignore-Regel, ausgelagertes Roundtrip-Skript.

Belegt die T-084-Guardrails ohne Fachlogik: ``scripts/validate.py`` registriert
die kumulative Stufe ``m4``, der Lint-Block prueft zusaetzlich das Format, jeder
``# type: ignore[...]`` unter ``backend/`` braucht eine nachgestellte Begruendung,
das Import-Roundtrip-Pruefprogramm liegt in ``scripts/roundtrip_check.py``
(aufrufbar per ``uv run python scripts/roundtrip_check.py``) und ``validate.py``
nutzt weiterhin nur die Standardbibliothek.

Anforderungen: F-EXP-04/F-EXP-06 (verlustfreier Roundtrip ohne neue Buchungen),
F-IMP-04 (eigenes JSON ohne Mapping), N-DB-01 (typisierter DB-Zugriff, deshalb
begruendete ignores), Review-Checkliste AGENTS.md (Validiere nur ueber hx.py,
kein rohes SQL). Es werden keine echten Kontodaten oder Zugangsdaten verwendet.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATE = REPO_ROOT / "scripts" / "validate.py"
ROUNDTRIP = REPO_ROOT / "scripts" / "roundtrip_check.py"


def _load_validate() -> object:
    """Laedt validate.py als Modul (scripts/ ist kein Paket)."""
    spec = importlib.util.spec_from_file_location("validate_m4", VALIDATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate = _load_validate()


def test_stage_m4_ist_registriert() -> None:
    """Stufe m4 ist in IMPLEMENTED eingetragen und kumulativ aufrufbar."""
    assert "m4" in validate.IMPLEMENTED
    proc = subprocess.run(
        [sys.executable, str(VALIDATE), "--list", "m4"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_type_ignore_ohne_begruendung_wird_erkannt() -> None:
    """Unbegruendete ignores werden gemeldet, begruendete nicht."""
    text = (
        "x = 1  # type: ignore[misc]\n"  # Zeile 1: unbegruendet
        "y = 2  # type: ignore[misc]  # Grund: python-fints ohne Stubs\n"  # Zeile 2: ok
        "z = 3  # type: ignore  # d2\n"  # Zeile 3: ok
        "w = 4  # type: ignore\n"  # Zeile 4: unbegruendet
    )
    assert validate.unjustified_type_ignore_lines(text) == [1, 4]


def test_backend_ignores_sind_vollstaendig_begruendet() -> None:
    """Der reale backend/-Baum verletzt die Regel nicht (sonst Lint rot)."""
    ok, msg = validate.check_type_ignore_reasons()
    assert ok, msg


def test_roundtrip_skript_existiert_und_ist_verdrahtet() -> None:
    """Das ausgelagerte Roundtrip-Skript existiert; validate.py ruft es auf."""
    assert ROUNDTRIP.is_file()
    source = VALIDATE.read_text(encoding="utf-8")
    assert "scripts/roundtrip_check.py" in source
    # Das eingebettete String-Literal wurde ersetzt, nicht dupliziert.
    assert "ROUNDTRIP_SCRIPT" not in source


def test_validate_nutzt_nur_standardbibliothek() -> None:
    """validate.py importiert ausschliesslich Module der Standardbibliothek."""
    tree = ast.parse(VALIDATE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    extern = sorted(r for r in roots if r not in sys.stdlib_module_names)
    assert extern == [], f"Nicht-Standardbibliothek in validate.py: {extern}"
