#!/usr/bin/env python3
"""Deterministisches Pruefskript fuer Finanzuebersicht.

Aufruf: python scripts/validate.py [--list] <stufe>
Exit 0 nur, wenn alle Pruefungen der Stufe bestehen. Nicht umgesetzte Stufen enden mit Exit 1
("Stufe nicht implementiert"), nie stillschweigend mit Erfolg. Nur Standardbibliothek.
Stufen m<n> sind kumulativ: m3 prueft auch m1 und m2.
--list <stufe> gibt Pruefungen ohne Ausfuehrung aus (Exit 0).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ID = "finanzuebersicht"

MILESTONES = [f"m{i}" for i in range(0, 11)]
BLOCKS = [
    "lint",
    "types",
    "unit",
    "integration",
    "roundtrip",
    "smoke",
    "perf",
    "e2e",
    "mcp",
    "concurrency",
    "backup",
    "docker",
]

REQUIRED_FILES = [
    "SPEC.md",
    "initialPrompt.md",
    "AGENTS.md",
    "CLAUDE.md",
    "hx.py",
    ".gitignore",
    ".claude/settings.json",
    ".claude/agents/finanzuebersicht-lead.md",
    ".claude/skills/finanzuebersicht-fints/SKILL.md",
    ".claude/skills/finanzuebersicht-fints/references/fints.md",
    ".claude/skills/finanzuebersicht-fints/references/banken.md",
    ".claude/skills/finanzuebersicht-fints/references/csv.md",
    ".claude/skills/finanzuebersicht-fints/references/dubletten.md",
    "scripts/validate.py",
    f"projects/{PROJECT_ID}/decisions.md",
    f"projects/{PROJECT_ID}/plan.md",
    f"projects/{PROJECT_ID}/handoff.md",
    f"projects/{PROJECT_ID}/status.md",
]

# Ueberschriften bzw. Stichworte, die AGENTS.md enthalten muss (Abschnitt 0.2).
AGENTS_SECTIONS = [
    "Ziel, Umfang, Nicht-Ziele",
    "Stack",
    "Verzeichnisstruktur",
    "Abweichungen vom globalen Harness",
    "Sicherheitsregeln",
    "Risikostufen",
    "Review-Checkliste",
    "Konventionen",
    "## Harness",
]

Result = tuple[bool, str]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def check_required_files() -> Result:
    missing = [f for f in REQUIRED_FILES if not (ROOT / f).is_file()]
    return (not missing, "fehlend: " + ", ".join(missing) if missing else f"{len(REQUIRED_FILES)} Dateien vorhanden")


def check_skill_frontmatter() -> Result:
    path = ROOT / ".claude/skills/finanzuebersicht-fints/SKILL.md"
    if not path.is_file():
        return False, "SKILL.md fehlt"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return False, "Frontmatter fehlt"
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
        elif line.strip():
            return False, f"Frontmatter nicht einzeilig: {line[:40]!r}"
    errors = []
    if fields.get("name") != "finanzuebersicht-fints":
        errors.append("name != Ordnername")
    desc = fields.get("description", "")
    if not desc:
        errors.append("description fehlt")
    elif desc in ("|", ">", "|-", ">-"):
        errors.append("description ist Blockskalar")
    elif len(desc) > 1024:
        errors.append(f"description zu lang ({len(desc)})")
    else:
        for trigger in ("FinTS", "TAN", "Sync", "CSV", "JSON", "Markdown", "Dubletten", "Bankfehler"):
            if trigger.lower() not in desc.lower():
                errors.append(f"Ausloeser fehlt: {trigger}")
    return (not errors, "; ".join(errors) if errors else f"gueltig (description {len(desc)} Zeichen)")


def check_agents_sections() -> Result:
    text = read("AGENTS.md")
    missing = [s for s in AGENTS_SECTIONS if s not in text]
    return (
        not missing,
        "fehlende Abschnitte: " + ", ".join(missing) if missing else "alle Pflichtabschnitte vorhanden",
    )


def check_settings() -> Result:
    try:
        cfg = json.loads(read(".claude/settings.json"))
    except (OSError, ValueError) as exc:
        return False, f"settings.json nicht lesbar: {exc}"
    perms = cfg.get("permissions", {})
    allow, deny = set(perms.get("allow", [])), set(perms.get("deny", []))
    need_allow = {
        "Bash(python hx.py *)",
        "PowerShell(python hx.py *)",
        "PowerShell(uv run *)",
        "PowerShell(uv sync*)",
        "PowerShell(npm run *)",
        "PowerShell(git add *)",
        "PowerShell(git commit *)",
    }
    need_deny = {
        "Read(./.env)",
        "Read(./.env.*)",
        "Read(**/*.pem)",
        "Read(**/*.key)",
        # git push ist bewusst nicht mehr gesperrt (Owner-Entscheidung); docker push bleibt gesperrt.
        "Bash(docker push*)",
        "PowerShell(docker push*)",
    }
    errors = [f"allow fehlt: {r}" for r in sorted(need_allow - allow)] + [
        f"deny fehlt: {r}" for r in sorted(need_deny - deny)
    ]
    return (not errors, "; ".join(errors) if errors else "Allow- und Deny-Regeln vollstaendig")


def check_plan() -> Result:
    text = read(f"projects/{PROJECT_ID}/plan.md")
    missing = [m.upper() for m in MILESTONES if not re.search(rf"\b{m.upper()}\b", text)]
    return (not missing, "plan.md ohne: " + ", ".join(missing) if missing else "plan.md enthaelt M0 bis M10")


def check_git() -> Result:
    if not (ROOT / ".git").exists():
        return False, "kein Git-Repo"
    gitignore = read(".gitignore") if (ROOT / ".gitignore").is_file() else ""
    needed = [".venv/", "node_modules/", "frontend/dist/", "data/", "*.key", ".env*", "test-results/"]
    missing = [n for n in needed if n not in gitignore]
    return (
        not missing,
        ".gitignore ohne: " + ", ".join(missing) if missing else "Git-Repo vorhanden, .gitignore vollstaendig",
    )


def check_hx_status() -> Result:
    proc = subprocess.run(
        [sys.executable, "hx.py", "status", PROJECT_ID],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode == 0, f"hx.py status Exit {proc.returncode}"


def run_external(cmd: list[str]) -> Result:
    """Externe Pruefung ausfuehren (ruff, mypy, pytest)."""
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode == 0:
            # Erfolg: kurze Summary aus stdout extrahieren
            lines = proc.stdout.strip().split("\n") if proc.stdout.strip() else []
            msg = lines[-1] if lines else "bestanden"
            return True, msg
        else:
            # Fehler: stderr oder stdout mit Fehlerinfo
            error = proc.stderr.strip() or proc.stdout.strip()
            lines = error.split("\n")
            # Letzten Fehler oder Summary nehmen
            return False, lines[-1] if lines else "fehlgeschlagen"
    except Exception as exc:
        return False, f"Fehler beim Ausfuehren: {exc!r}"


def check_lint() -> Result:
    """ruff: Code-Stil und Fehler pruefen."""
    return run_external(["uv", "run", "ruff", "check", "backend", "tests", "scripts"])


def check_types() -> Result:
    """mypy: Typ-Pruefung fuer backend/."""
    return run_external(["uv", "run", "mypy", "backend"])


def check_unit() -> Result:
    """pytest: Unit-Tests."""
    return run_external(["uv", "run", "pytest", "tests/unit", "-q"])


def check_smoke() -> Result:
    """pytest: Smoke-Tests (Integration)."""
    return run_external(["uv", "run", "pytest", "-m", "smoke", "tests/integration", "-q"])


# Eingebettetes Pruefprogramm fuer den Import-Roundtrip. Es laeuft im
# Projekt-Interpreter (uv run python), weil scripts/validate.py selbst nur die
# Standardbibliothek nutzen darf. Die Fixture wird in einer temporaeren,
# isolierten SQLite-Datei zweimal ueber execute_import importiert (F-EXP-04,
# F-EXP-06); echte Kontodaten werden nicht verwendet.
ROUNDTRIP_SCRIPT = """
import json
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "backend"))

from sqlalchemy import func, select

import finue.models
from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.transaction import Transaction
from finue.services.import_run import execute_import

fixture = root / "tests" / "fixtures" / "export_v1_sample.json"
if not fixture.is_file():
    print("ROUNDTRIP_FEHLER: Fixture fehlt: %s" % fixture)
    raise SystemExit(2)

document = json.loads(fixture.read_text(encoding="utf-8"))
expected = len(document.get("transactions", []))
if expected < 1:
    print("ROUNDTRIP_FEHLER: Fixture enthaelt keine Buchungen")
    raise SystemExit(2)

with tempfile.TemporaryDirectory(prefix="finue-roundtrip-") as tmp:
    engine = create_db_engine("sqlite:///" + (Path(tmp) / "roundtrip.db").as_posix())
    try:
        Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            account = Account(id=1, name="Roundtrip-Pruefung", currency="EUR", source="datei")
            session.add(account)
            session.commit()

            first = execute_import(account.id, document, None, "json", session)
            total_first = session.scalar(select(func.count()).select_from(Transaction)) or 0
            second = execute_import(account.id, document, None, "json", session)
            total_second = session.scalar(select(func.count()).select_from(Transaction)) or 0
    finally:
        engine.dispose()

fehler = []
if first.status != "erfolgreich":
    fehler.append("erster Lauf Status %r" % first.status)
if first.new_count != expected:
    fehler.append("erster Lauf new_count=%s, erwartet %s" % (first.new_count, expected))
if first.failed_count != 0:
    fehler.append("erster Lauf failed_count=%s" % first.failed_count)
if total_first != expected:
    fehler.append("Bestand nach erstem Lauf %s, erwartet %s" % (total_first, expected))
if second.status != "erfolgreich":
    fehler.append("zweiter Lauf Status %r" % second.status)
if second.new_count != 0:
    fehler.append("zweiter Lauf new_count=%s, erwartet 0" % second.new_count)
if total_second != total_first:
    fehler.append("Bestand nach zweitem Lauf %s, erwartet %s" % (total_second, total_first))

if fehler:
    print("ROUNDTRIP_FEHLER: " + "; ".join(fehler))
    raise SystemExit(1)

print(
    "ROUNDTRIP_OK erster_lauf=%s zweiter_lauf=%s bestand=%s"
    % (first.new_count, second.new_count, total_second)
)
"""


def check_roundtrip_import() -> Result:
    """Import-Roundtrip der JSON-v1-Fixture: zweiter Lauf legt nichts neu an.

    Referenziert F-EXP-04 (verlustfreier Export/Re-Import), F-EXP-06
    (Roundtrip ohne neue Buchungen) und F-IMP-04 (eigenes JSON ohne Mapping).
    """
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "-", str(ROOT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=ROUNDTRIP_SCRIPT,
        )
    except Exception as exc:  # Pruefung darf nie still abbrechen
        return False, f"Fehler beim Ausfuehren: {exc!r}"

    stdout = proc.stdout.strip()
    if proc.returncode == 0 and "ROUNDTRIP_OK" in stdout:
        marker = [line for line in stdout.splitlines() if "ROUNDTRIP_OK" in line]
        return True, marker[-1] if marker else "Roundtrip bestanden"

    detail = ""
    for stream in (stdout, proc.stderr.strip()):
        lines = [line for line in stream.splitlines() if line.strip()]
        if lines:
            detail = lines[-1]
            break
    return False, f"Exit {proc.returncode}: {detail or 'keine Ausgabe'}"


STAGE_M0: list[tuple[str, Callable[[], Result]]] = [
    ("Pflichtdateien", check_required_files),
    ("Skill-Frontmatter", check_skill_frontmatter),
    ("AGENTS.md Pflichtabschnitte", check_agents_sections),
    ("settings.json Rechte", check_settings),
    ("plan.md Milestones", check_plan),
    ("Git-Repo", check_git),
    ("hx.py status", check_hx_status),
]

STAGE_M1: list[tuple[str, Callable[[], Result]]] = [
    ("lint (ruff)", check_lint),
    ("types (mypy)", check_types),
    ("unit (pytest)", check_unit),
    ("smoke (pytest)", check_smoke),
]

# M2-Pruefungen. Die Pruefungen der Import- und Dublettenlogik entstehen mit
# ihren Tasks (T-045 bis T-057). Der Import-Roundtrip wird hier explizit
# mitgeprueft, weil plan.md fuer die M2-Abnahme "validate.py m2 (+ roundtrip
# Import-Teil)" verlangt. Der BLOCKS-Eintrag "roundtrip" bleibt davon getrennt
# und ist unter IMPLEMENTED registriert.
ROUNDTRIP_CHECK: tuple[str, Callable[[], Result]] = (
    "Import-Roundtrip (JSON v1, 0 neue Buchungen bei Re-Import)",
    check_roundtrip_import,
)

STAGE_M2: list[tuple[str, Callable[[], Result]]] = [ROUNDTRIP_CHECK]

# Umgesetzte Stufen. Neue Stufen mit dem jeweiligen Milestone hier eintragen.
IMPLEMENTED: dict[str, list[tuple[str, Callable[[], Result]]]] = {
    "m0": STAGE_M0,
    "m1": STAGE_M0 + STAGE_M1,
    "m2": STAGE_M2,
    "roundtrip": [ROUNDTRIP_CHECK],
}


def run_stage(stage: str) -> int:
    if stage in MILESTONES:
        index = MILESTONES.index(stage)
        chain = MILESTONES[: index + 1]
    elif stage in BLOCKS:
        chain = [stage]
    else:
        print(f"Unbekannte Stufe: {stage}. Gueltig: {', '.join(MILESTONES + BLOCKS)}")
        return 1
    print(f"# validate.py {stage}\n")
    failed = 0
    for name in chain:
        checks = IMPLEMENTED.get(name)
        if checks is None:
            print(f"[FEHLER] Stufe {name}: Stufe nicht implementiert")
            failed += 1
            continue
        for title, fn in checks:
            try:
                ok, msg = fn()
            except Exception as exc:  # Pruefung darf nie still abbrechen
                ok, msg = False, f"Ausnahme: {exc!r}"
            print(f"[{'OK' if ok else 'FEHLER'}] {name}: {title} - {msg}")
            failed += 0 if ok else 1
    print(f"\nErgebnis: {'bestanden' if failed == 0 else f'{failed} Pruefung(en) fehlgeschlagen'}")
    return 0 if failed == 0 else 1


def list_stage(stage: str) -> int:
    """Liste Pruefungen ohne Ausfuehrung auf."""
    if stage in MILESTONES:
        index = MILESTONES.index(stage)
        chain = MILESTONES[: index + 1]
    elif stage in BLOCKS:
        chain = [stage]
    else:
        print(f"Unbekannte Stufe: {stage}. Gueltig: {', '.join(MILESTONES + BLOCKS)}")
        return 1

    print(f"# validate.py --list {stage}\n")
    for name in chain:
        checks = IMPLEMENTED.get(name)
        if checks is None:
            print(f"[INFO] Stufe {name}: noch nicht implementiert")
            continue
        for title, _ in checks:
            print(f"  {name}: {title}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Aufruf: python scripts/validate.py [--list] <stufe>")
        return 1

    if argv[1] == "--list":
        if len(argv) < 3:
            print("Aufruf: python scripts/validate.py --list <stufe>")
            return 1
        return list_stage(argv[2].lower())

    return run_stage(argv[1].lower())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
