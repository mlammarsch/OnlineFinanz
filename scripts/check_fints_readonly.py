#!/usr/bin/env python3
"""Read-only-Guard fuer FinTS: findet schreibende python-fints-Operationen.

Der Guard durchsucht ``backend/`` per AST (Aufrufe, Importe, Attributzugriffe,
exakte Namens-Strings) und zusaetzlich per Text-/Tokenpruefung. Er endet mit
Exit 1 und nennt jede Fundstelle, sobald ein schreibender Name auftaucht.
Damit ist "Auftraege nur lesend" (D2, F-AUF-05, F-AUF-07) maschinell belegt.

Die verbotene Namensliste ist aus python-fints 5.0.1 abgeleitet (T-060):

- Client-Methoden, die Auftraege einreichen: ``sepa_transfer``,
  ``simple_sepa_transfer`` und ``sepa_debit``. ``simple_sepa_debit`` nennt die
  Task-Definition, existiert in 5.0.1 aber nicht (dort nur ``sepa_debit``);
  der Name bleibt defensiv in der Liste.
- Schreibende Command-Segmente: SEPA-Einzel-/Sammelueberweisung
  (HKCCS1, HKIPZ1, HKCCM1, HKIPM1) und das Einreichen von Lastschriften
  (HKDSE1/2, HKDME1/2, HKDSC1, HKDMC1). ``HKDBS1``, ``HKDBS2`` und
  ``HKDMB1`` fordern nur den Bestand terminierter Lastschriften an und sind
  ausdruecklich erlaubt.

Nur Standardbibliothek. Aufruf: ``python scripts/check_fints_readonly.py
[--root PFAD]``.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "backend"

# Version, aus der die Namensliste abgeleitet wurde (T-060, exakt gepinnt).
PINNED_FINTS_VERSION: Final[str] = "5.0.1"

FORBIDDEN_CLIENT_METHODS: Final[frozenset[str]] = frozenset(
    {
        "sepa_transfer",
        "simple_sepa_transfer",
        "sepa_debit",
        # In 5.0.1 nicht vorhanden, aber in der Task-Definition genannt:
        # defensiv mitgefuehrt, damit ein spaeterer Pin-Sprung nicht durchrutscht.
        "simple_sepa_debit",
    }
)

FORBIDDEN_SEGMENT_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "HKCCS1",  # SEPA-Einzelueberweisung
        "HKIPZ1",  # SEPA-instant Einzelueberweisung
        "HKCCM1",  # SEPA-Sammelueberweisung
        "HKIPM1",  # SEPA-instant Sammelueberweisung
        "HKDSE1",  # terminierte SEPA-Einzellastschrift einreichen
        "HKDSE2",
        "HKDME1",  # terminierte SEPA-Sammellastschrift einreichen
        "HKDME2",
        "HKDSC1",  # terminierte SEPA-COR1-Einzellastschrift einreichen
        "HKDMC1",  # terminierte SEPA-COR1-Sammellastschrift einreichen
    }
)

# Lesende Auftragsabfragen, die der Guard bewusst nicht meldet.
ALLOWED_READONLY_SEGMENT_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "HKDBS1",  # Bestand terminierter SEPA-Einzellastschriften anfordern
        "HKDBS2",
        "HKDMB1",  # Bestand terminierter SEPA-Sammellastschriften anfordern
    }
)

FORBIDDEN_NAMES: Final[frozenset[str]] = FORBIDDEN_CLIENT_METHODS | FORBIDDEN_SEGMENT_CLASSES

_SKIP_DIRS: Final[frozenset[str]] = frozenset({"__pycache__", ".venv", "node_modules", ".git"})
_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset({".py", ".pyi"})


@dataclass(frozen=True)
class Finding:
    """Eine Fundstelle im Quelltext."""

    path: Path
    line: int
    column: int
    name: str
    kind: str

    def format(self) -> str:
        """Fundstelle als ``pfad:zeile:spalte: Art 'Name'``."""
        try:
            shown = self.path.resolve().relative_to(ROOT)
        except ValueError:
            shown = self.path
        return f"{shown.as_posix()}:{self.line}:{self.column + 1}: {self.kind} '{self.name}'"


class _AstVisitor(ast.NodeVisitor):
    """Sammelt Aufrufe, Importe, Attributzugriffe und Namens-Strings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[Finding] = []

    def _add(self, node: ast.AST, name: str, kind: str) -> None:
        self.findings.append(
            Finding(
                path=self.path,
                line=getattr(node, "lineno", 0),
                column=getattr(node, "col_offset", 0),
                name=name,
                kind=kind,
            )
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in FORBIDDEN_NAMES:
                self._add(alias, alias.name, "Import")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self._add(node, node.id, "Bezeichner")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_NAMES:
            self._add(node, node.attr, "Attributzugriff")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_NAMES:
            name = func.attr
        elif isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
            name = func.id
        if name is not None:
            self._add(node, name, "Aufruf")
            # Argumente weiter pruefen, das Func-Attribut selbst nicht doppelt.
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in FORBIDDEN_NAMES:
            self._add(node, node.value, "Namens-String")
        self.generic_visit(node)


def _scan_text(path: Path, text: str) -> list[Finding]:
    """Textpruefung per Tokenizer; bei nicht tokenisierbarem Code Zeilenfallback.

    Der Tokenizer sieht nur echte Bezeichner (keine Kommentare und keine
    Strings) und ergaenzt damit die AST-Pruefung unabhaengig. Bricht er ab,
    prueft der Fallback die Zeilen ohne Kommentaranteil.
    """
    findings: list[Finding] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.NAME and token.string in FORBIDDEN_NAMES:
                findings.append(
                    Finding(path=path, line=token.start[0], column=token.start[1], name=token.string, kind="Text")
                )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        findings.extend(_scan_lines_fallback(path, text))
    return findings


def _scan_lines_fallback(path: Path, text: str) -> list[Finding]:
    """Zeilenweise Suche ohne Kommentaranteil fuer nicht tokenisierbaren Code."""
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        for name in sorted(FORBIDDEN_NAMES):
            if re.search(rf"\b{re.escape(name)}\b", code):
                findings.append(Finding(path=path, line=lineno, column=0, name=name, kind="Text"))
    return findings


def scan_file(path: Path) -> list[Finding]:
    """Prueft eine Quelldatei und liefert die deduplizierten Fundstellen."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [Finding(path=path, line=0, column=0, name="<unlesbar>", kind=f"Lesefehler {exc.__class__.__name__}")]

    findings: list[Finding] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError):
        # Nicht parsebar: die Textpruefung uebernimmt (bewusst kein Zusatzbefund
        # fuer den Syntaxfehler selbst, das ist Aufgabe von ruff/mypy).
        tree = None
    if tree is not None:
        visitor = _AstVisitor(path)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    findings.extend(_scan_text(path, text))

    seen: set[tuple[int, str]] = set()
    unique: list[Finding] = []
    for finding in sorted(findings, key=lambda item: (item.line, item.column, item.name, item.kind)):
        key = (finding.line, finding.name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def scan_root(root: Path) -> tuple[list[Finding], int]:
    """Prueft alle Python-Quellen unter ``root``; liefert Funde und Dateizahl."""
    sources = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in _SOURCE_SUFFIXES and not _SKIP_DIRS.intersection(path.parts)
    )
    findings: list[Finding] = []
    for path in sources:
        findings.extend(scan_file(path))
    return findings, len(sources)


def main(argv: list[str] | None = None) -> int:
    """CLI: Exit 0 ohne Fund, Exit 1 mit Fundstelle, Exit 2 bei ungueltigem Ziel."""
    parser = argparse.ArgumentParser(description="Read-only-Guard fuer FinTS (schreibende python-fints-Namen).")
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="zu pruefendes Verzeichnis (Standard: backend/)",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    if not root.is_dir():
        print(f"FINTS_READONLY_FEHLER Zielverzeichnis fehlt: {root}")
        return 2

    findings, file_count = scan_root(root)
    if not file_count:
        print(f"FINTS_READONLY_FEHLER keine Python-Dateien unter {root}")
        return 1

    if findings:
        for finding in findings:
            print("FINTS_READONLY_FEHLER " + finding.format())
        print(f"FINTS_READONLY_FEHLER gesamt={len(findings)} dateien={file_count}")
        return 1

    print(f"FINTS_READONLY_OK dateien={file_count} verbotene_namen={len(FORBIDDEN_NAMES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
