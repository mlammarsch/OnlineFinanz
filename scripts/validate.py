#!/usr/bin/env python3
"""Deterministisches Pruefskript fuer Finanzuebersicht.

Aufruf: python scripts/validate.py [--list] <stufe>
Exit 0 nur, wenn alle Pruefungen der Stufe bestehen. Nicht umgesetzte Stufen enden mit Exit 1
("Stufe nicht implementiert"), nie stillschweigend mit Erfolg. Nur Standardbibliothek.
Stufen m<n> sind kumulativ: m4 prueft auch m0 bis m3.
--list <stufe> gibt Pruefungen ohne Ausfuehrung aus (Exit 0).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
    "api-e2e",
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

# Jeder '# type: ignore[...]' unter backend/ muss eine nachgestellte Begruendung
# tragen (T-084, verhindert undokumentierte/pauschale ignores, siehe T-041).
TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore(?:\[[^\]]*\])?")
TYPE_IGNORE_REASON_RE = re.compile(r"#\s*type:\s*ignore(?:\[[^\]]*\])?\s+#\s*\S")


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
        errors="replace",
    )
    return proc.returncode == 0, f"hx.py status Exit {proc.returncode}"


def run_external(cmd: list[str]) -> Result:
    """Externe Pruefung ausfuehren (ruff, mypy, pytest)."""
    try:
        # errors="replace": uv/pytest geben unter Windows teils cp1252 aus (Umlaute),
        # sonst bricht der Reader-Thread mit UnicodeDecodeError ab.
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
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


def unjustified_type_ignore_lines(text: str) -> list[int]:
    """Zeilennummern (1-basiert) mit '# type: ignore[...]' ohne Begruendung."""
    return [
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if TYPE_IGNORE_RE.search(line) and not TYPE_IGNORE_REASON_RE.search(line)
    ]


def check_type_ignore_reasons() -> Result:
    """Jeder '# type: ignore[...]' unter backend/ braucht eine nachgestellte Begruendung.

    Referenziert N-DB-01 (typisierter Zugriff) und die T-041-/T-084-Regel, dass
    ignores nicht pauschal gesetzt werden duerfen.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "backend").rglob("*.py")):
        for lineno in unjustified_type_ignore_lines(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    if offenders:
        return False, "type: ignore ohne Begruendung: " + ", ".join(offenders)
    return True, "alle type: ignore begruendet"


def check_lint() -> Result:
    """ruff check und Formatpruefung (ruff format --check) sowie type-ignore-Regel."""
    checks = [
        ["uv", "run", "ruff", "check", "backend", "tests", "scripts"],
        ["uv", "run", "ruff", "format", "--check", "backend", "tests", "scripts"],
    ]
    messages: list[str] = []
    for cmd in checks:
        ok, msg = run_external(cmd)
        if not ok:
            return False, msg
        messages.append(msg)
    ok, msg = check_type_ignore_reasons()
    if not ok:
        return False, msg
    messages.append(msg)
    return True, "; ".join(messages)


def check_types() -> Result:
    """mypy: Typ-Pruefung fuer backend/."""
    return run_external(["uv", "run", "mypy", "backend"])


def check_unit() -> Result:
    """pytest: Unit-Tests."""
    return run_external(["uv", "run", "pytest", "tests/unit", "-q"])


def check_smoke() -> Result:
    """pytest: Smoke-Tests (Integration)."""
    return run_external(["uv", "run", "pytest", "-m", "smoke", "tests/integration", "-q"])


def check_fints_readonly() -> Result:
    """Read-only-Guard: keine schreibenden FinTS-Operationen in backend/ (D2)."""
    return run_external([sys.executable, "scripts/check_fints_readonly.py"])


def _run_pytest(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def check_integration() -> Result:
    """pytest: Integrationstests des M3-Sync-Pfads, mindestens ein gesammelter Test.

    Referenziert F-SYN-01..11 (Sync-Orchestrierung, TAN, Fehler-Mapping,
    Protokoll, Sperre), F-AUF-04/05 (Auftragsabruf und Rueckfall) und
    F-CON-05 (Sync aendert keine Nutzerfelder).
    """
    plan = ["uv", "run", "pytest", "-m", "integration", "tests/integration"]
    try:
        collected = _run_pytest([*plan, "--collect-only", "-q"])
        if collected.returncode != 0:
            detail = (collected.stderr.strip() or collected.stdout.strip()).splitlines()
            return False, f"Sammellauf Exit {collected.returncode}: {detail[-1] if detail else 'keine Ausgabe'}"
        count = sum(1 for line in collected.stdout.splitlines() if "::" in line)
        if count < 1:
            return False, "keine Integrations-Tests gesammelt"

        proc = _run_pytest([*plan, "-q"])
    except Exception as exc:  # Pruefung darf nie still abbrechen
        return False, f"Fehler beim Ausfuehren: {exc!r}"

    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    summary = lines[-1] if lines else "keine Ausgabe"
    if proc.returncode != 0:
        return False, f"Exit {proc.returncode}: {summary}"
    return True, f"{count} gesammelt; {summary}"


# Das Import-Roundtrip-Pruefprogramm liegt ausgelagert in
# scripts/roundtrip_check.py (T-084) und laeuft im Projekt-Interpreter
# (uv run python), damit scripts/validate.py selbst nur die Standardbibliothek
# nutzt. Der Aufruf unten gibt dessen Ausgabe unveraendert weiter
# (F-EXP-04, F-EXP-06, F-IMP-04).


def check_roundtrip_import() -> Result:
    """Datei-Roundtrip: JSON (v1 + Mehrkonto), CSV-Vorlage, Schema-Versionen.

    Referenziert F-EXP-04 (verlustfreier Export/Re-Import), F-EXP-06
    (Roundtrip ohne neue Buchungen, JSON und CSV mit Vorlage), F-EXP-07
    (Schema-Versionen importierbar, neuere Version klar abgelehnt), F-ABS-06
    (Kennzeichen bleiben nach Re-Import und Fake-Sync erhalten), F-DUP-04
    (zusammengefuehrte Buchungen sind ausgeschlossen) und F-IMP-04 (eigenes
    JSON ohne Mapping). Das Pruefprogramm liegt in scripts/roundtrip_check.py
    (T-084, erweitert in T-105) und meldet ``ROUNDTRIP_OK``.
    """
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "scripts/roundtrip_check.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    "Roundtrip Datei-Import/-Export (JSON v1+Mehrkonto, CSV-Vorlage, Schema-Versionen; 0 neue bei Re-Import)",
    check_roundtrip_import,
)

STAGE_M2: list[tuple[str, Callable[[], Result]]] = [ROUNDTRIP_CHECK]

# M3-Pruefungen: Read-only-Guard und Integrationstests des Sync-Pfads. lint,
# types, unit und smoke liefert die m1-Stufe, die run_stage kumulativ vor m3
# ausfuehrt; STAGE_M3 wiederholt sie deshalb nicht.
READONLY_CHECK: tuple[str, Callable[[], Result]] = (
    "FinTS Read-only-Guard (keine schreibenden Operationen)",
    check_fints_readonly,
)

INTEGRATION_CHECK: tuple[str, Callable[[], Result]] = (
    "integration (pytest, mind. 1 gesammelter Test)",
    check_integration,
)

STAGE_M3: list[tuple[str, Callable[[], Result]]] = [READONLY_CHECK, INTEGRATION_CHECK]

# M4-Pruefungen. Die M4-Abnahme (T-107) fuehrt die erweiterte roundtrip-Pruefung
# (T-105) sowie die Bloecke perf (T-103), concurrency (T-104) und api-e2e (T-106)
# ein; die tragen ihre Checks hier ein. Der Roundtrip-Check (T-105) steht zusaetzlich
# in STAGE_M4, damit `validate.py m4` die erweiterte Pruefung explizit ausweist.
# Der Performance-Block (T-103, N-PERF-01/F-BUC-06) misst Suche und Buchungsliste
# bei 100.000 Buchungen und ist als eigener BLOCK "perf" registriert; die Messwerte
# stehen im Meldungstext.
PERF_REPORT_ENV = "FINUE_PERF_REPORT"


def check_perf() -> Result:
    """Performance-Block bei 100.000 Buchungen (N-PERF-01, F-BUC-06).

    Fuehrt die Perf-Tests (pytest-Marker ``perf``) gegen den in
    ``scripts/perf_data.py`` erzeugten Bestand aus und gibt die gemessenen
    Median-/Maximum-Werte je Messung mit aus. Der Exit-Code der Tests entscheidet;
    der Messbericht wird ueber :data:`PERF_REPORT_ENV` eingesammelt.
    """
    with tempfile.TemporaryDirectory(prefix="finue-perf-report-") as tmp:
        report = Path(tmp) / "perf_report.txt"
        env = dict(os.environ)
        env[PERF_REPORT_ENV] = str(report)
        try:
            proc = subprocess.run(
                ["uv", "run", "pytest", "-m", "perf", "tests/perf", "-q", "-s"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as exc:  # Pruefung darf nie still abbrechen
            return False, f"Fehler beim Ausfuehren: {exc!r}"

        measurements: list[str] = []
        if report.is_file():
            for line in report.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    measurements.append(f"{parts[0]} median={parts[1]}ms max={parts[2]}ms")

    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        summary = detail[-1] if detail else "keine Ausgabe"
        return False, f"Exit {proc.returncode}: {summary}"
    if not measurements:
        return False, "keine Messwerte erzeugt"
    return True, "; ".join(measurements)


PERF_CHECK: tuple[str, Callable[[], Result]] = (
    "Performance 100.000 Buchungen (N-PERF-01, F-BUC-06)",
    check_perf,
)

# Der Concurrency-Block (T-104, F-CON-01..05, N-DB-04) faehrt viele parallele
# Schreiber gegen Datei-SQLite im WAL-Modus. Die Tests synchronisieren ueber
# Barrieren (kein sleep) und muessen reproduzierbar sein; der Check wiederholt
# den Lauf deshalb mehrfach und verlangt jeden Lauf gruen.
CONCURRENCY_RUNS = 3


def check_concurrency() -> Result:
    """Concurrency-Block gegen Datei-SQLite (F-CON-01..05, N-DB-04).

    Fuehrt die Nebenlaeufigkeits-Tests (pytest-Marker ``concurrency``) mehrfach
    hintereinander aus; nur wenn **jeder** Lauf gruen ist, gilt der Block als
    bestanden (reproduzierbare Tests, Barrieren statt ``sleep``). Der Exit-Code
    der Laeufe entscheidet.
    """

    summaries: list[str] = []
    for run_index in range(1, CONCURRENCY_RUNS + 1):
        proc = _run_pytest(["uv", "run", "pytest", "-m", "concurrency", "tests/concurrency", "-q"])
        lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        summary = lines[-1] if lines else "keine Ausgabe"
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            message = detail[-1] if detail else summary
            return False, f"Lauf {run_index}/{CONCURRENCY_RUNS} rot: {message}"
        summaries.append(summary)
    return True, f"{CONCURRENCY_RUNS} Laeufe gruen ({summaries[-1]})"


CONCURRENCY_CHECK: tuple[str, Callable[[], Result]] = (
    "Nebenlaeufigkeit Datei-SQLite, 3x gruen (F-CON-01..05, N-DB-04)",
    check_concurrency,
)

# Der API-E2E-Block (T-113/T-115, Abschlussgate T-106) startet die
# Ende-zu-Ende-Szenarien gegen einen echten Uvicorn-Prozess mit genau einem
# Worker (D3), bei dem nur der FinTS-Client ein Fake ist (D8). Die Szenarien
# sind eigenstaendig und langsam (je Lauf eigener Serverstart), deshalb laeuft
# der Block dreimal hintereinander; jeder Lauf muss gruen sein und die
# Gesamtlaufzeit unter dem Budget von 3 Minuten bleiben.
API_E2E_RUNS = 3
API_E2E_BUDGET_SECONDS = 180.0


def check_api_e2e() -> Result:
    """API-E2E-Szenarien gegen echten Uvicorn mit Fake-FinTS (T-106).

    Fuehrt die Ende-zu-Ende-Szenarien (pytest-Marker ``api_e2e``) gegen einen
    echten Uvicorn-Prozess in einem Worker (D3) aus, bei dem nur der FinTS-Client
    durch den Fake ersetzt ist. Verlangt mindestens einen gesammelten Test und
    dreimal hintereinander einen gruenen Lauf; die Gesamtlaufzeit der drei Laeufe
    muss unter :data:`API_E2E_BUDGET_SECONDS` (3 Minuten) bleiben. Der Exit-Code
    der Laeufe entscheidet.
    """
    plan = ["uv", "run", "pytest", "-m", "api_e2e", "tests/e2e_api"]
    try:
        collected = _run_pytest([*plan, "--collect-only", "-q"])
        if collected.returncode != 0:
            detail = (collected.stderr.strip() or collected.stdout.strip()).splitlines()
            return False, f"Sammellauf Exit {collected.returncode}: {detail[-1] if detail else 'keine Ausgabe'}"
        count = sum(1 for line in collected.stdout.splitlines() if "::" in line)
        if count < 1:
            return False, "keine API-E2E-Tests gesammelt"
    except Exception as exc:  # Pruefung darf nie still abbrechen
        return False, f"Fehler beim Ausfuehren: {exc!r}"

    summaries: list[str] = []
    started = time.monotonic()
    for run_index in range(1, API_E2E_RUNS + 1):
        proc = _run_pytest([*plan, "-q"])
        lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        summary = lines[-1] if lines else "keine Ausgabe"
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            message = detail[-1] if detail else summary
            return False, f"Lauf {run_index}/{API_E2E_RUNS} rot: {message}"
        summaries.append(summary)
    elapsed = time.monotonic() - started
    if elapsed > API_E2E_BUDGET_SECONDS:
        return False, f"ueber Budget: {elapsed:.1f}s > {API_E2E_BUDGET_SECONDS:.0f}s ({summaries[-1]})"
    return True, f"{count} gesammelt; {API_E2E_RUNS} Laeufe gruen in {elapsed:.1f}s ({summaries[-1]})"


API_E2E_CHECK: tuple[str, Callable[[], Result]] = (
    "API-E2E gegen echten Uvicorn mit Fake-FinTS, 3x gruen unter 3 Minuten (T-106)",
    check_api_e2e,
)

STAGE_M4: list[tuple[str, Callable[[], Result]]] = [
    ROUNDTRIP_CHECK,
    PERF_CHECK,
    CONCURRENCY_CHECK,
    API_E2E_CHECK,
]

# Umgesetzte Stufen. Neue Stufen mit dem jeweiligen Milestone hier eintragen.
# Die m<n>-Kette haengt run_stage selbst an; die Eintraege enthalten daher nur
# die Stufe selbst, sonst laufen m0/m1-Checks doppelt.
IMPLEMENTED: dict[str, list[tuple[str, Callable[[], Result]]]] = {
    "m0": STAGE_M0,
    "m1": STAGE_M1,
    "m2": STAGE_M2,
    "m3": STAGE_M3,
    "m4": STAGE_M4,
    "roundtrip": [ROUNDTRIP_CHECK],
    "perf": [PERF_CHECK],
    "concurrency": [CONCURRENCY_CHECK],
    "api-e2e": [API_E2E_CHECK],
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
