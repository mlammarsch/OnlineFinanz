#!/usr/bin/env python3
"""Deterministisches Pruefskript fuer Finanzuebersicht.

Aufruf: python scripts/validate.py <stufe>
Exit 0 nur, wenn alle Pruefungen der Stufe bestehen. Nicht umgesetzte Stufen enden mit Exit 1
("Stufe nicht implementiert"), nie stillschweigend mit Erfolg. Nur Standardbibliothek.
Stufen m<n> sind kumulativ: m3 prueft auch m1 und m2.
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
    "lint", "types", "unit", "integration", "roundtrip", "smoke", "perf",
    "e2e", "mcp", "concurrency", "backup", "docker",
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
    return (not missing, "fehlende Abschnitte: " + ", ".join(missing) if missing else "alle Pflichtabschnitte vorhanden")


def check_settings() -> Result:
    try:
        cfg = json.loads(read(".claude/settings.json"))
    except (OSError, ValueError) as exc:
        return False, f"settings.json nicht lesbar: {exc}"
    perms = cfg.get("permissions", {})
    allow, deny = set(perms.get("allow", [])), set(perms.get("deny", []))
    need_allow = {
        "Bash(python hx.py *)", "PowerShell(python hx.py *)", "PowerShell(uv run *)", "PowerShell(uv sync*)",
        "PowerShell(npm run *)", "PowerShell(git add *)", "PowerShell(git commit *)",
    }
    need_deny = {
        "Read(./.env)", "Read(./.env.*)", "Read(**/*.pem)", "Read(**/*.key)",
        "Bash(git push*)", "Bash(docker push*)", "PowerShell(git push*)", "PowerShell(docker push*)",
    }
    errors = [f"allow fehlt: {r}" for r in sorted(need_allow - allow)] + [f"deny fehlt: {r}" for r in sorted(need_deny - deny)]
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
    return (not missing, ".gitignore ohne: " + ", ".join(missing) if missing else "Git-Repo vorhanden, .gitignore vollstaendig")


def check_hx_status() -> Result:
    proc = subprocess.run(
        [sys.executable, "hx.py", "status", PROJECT_ID], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    return proc.returncode == 0, f"hx.py status Exit {proc.returncode}"


STAGE_M0: list[tuple[str, Callable[[], Result]]] = [
    ("Pflichtdateien", check_required_files),
    ("Skill-Frontmatter", check_skill_frontmatter),
    ("AGENTS.md Pflichtabschnitte", check_agents_sections),
    ("settings.json Rechte", check_settings),
    ("plan.md Milestones", check_plan),
    ("Git-Repo", check_git),
    ("hx.py status", check_hx_status),
]

# Umgesetzte Stufen. Neue Stufen mit dem jeweiligen Milestone hier eintragen.
IMPLEMENTED: dict[str, list[tuple[str, Callable[[], Result]]]] = {"m0": STAGE_M0}


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


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Aufruf: python scripts/validate.py <stufe>")
        return 1
    return run_stage(argv[1].lower())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
