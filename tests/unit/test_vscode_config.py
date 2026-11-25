"""Tests fuer die VS-Code-Konfiguration der lokalen Entwicklung (T-038).

Referenziert N-OPS-01 (lokale Entwicklung aus VS Code): die Launch-Konfiguration
startet das Backend auf ``127.0.0.1:8000`` gegen die SQLite-Entwicklungsdatenbank,
der Task legt die Demo-Daten an. Beide JSON-Dateien werden per ``json.load``
gelesen, womit reines JSON ohne Kommentare belegt ist.

Zusaetzlich sichern die Tests ab, dass F5 und der Task ``Demo-Daten`` dieselbe
SQLite-Datei oeffnen: ``backend/finue/config.py`` loest einen relativen
``sqlite:///``-Pfad gegen das Prozess-cwd auf, weshalb Launch und Task in
demselben Verzeichnis mit demselben ``DATABASE_URL`` laufen muessen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

# Projektwurzel: tests/unit/test_vscode_config.py -> zwei Ebenen hoch.
REPO_ROOT = Path(__file__).resolve().parents[2]
VSCODE_DIR = REPO_ROOT / ".vscode"

# VS-Code-Variable, die in launch.json und tasks.json auf die Projektwurzel zeigt.
WORKSPACE_FOLDER = "${workspaceFolder}"

# Datenbank der lokalen Entwicklung, relativ zur Projektwurzel.
DEV_DATABASE_URL = "sqlite:///data/dev.db"


def _load_json(path: Path) -> dict[str, Any]:
    """Liest eine JSON-Datei streng; Kommentare oder Trailing-Commas scheitern lassen."""
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _option(args: list[str], name: str) -> str:
    """Liefert den Wert, der in ``args`` auf ``name`` folgt."""
    assert name in args, f"{name} fehlt in {args}"
    return args[args.index(name) + 1]


def _launch_config() -> dict[str, Any]:
    """Liefert die Launch-Konfiguration ``Backend uvicorn``."""
    data = _load_json(VSCODE_DIR / "launch.json")
    configs = {config["name"]: config for config in data["configurations"]}
    assert "Backend uvicorn" in configs
    return cast(dict[str, Any], configs["Backend uvicorn"])


def _demo_task() -> dict[str, Any]:
    """Liefert den Task ``Demo-Daten``."""
    data = _load_json(VSCODE_DIR / "tasks.json")
    tasks = {task["label"]: task for task in data["tasks"]}
    assert "Demo-Daten" in tasks
    return cast(dict[str, Any], tasks["Demo-Daten"])


def _expand_workspace(value: str) -> Path:
    """Ersetzt ``${workspaceFolder}`` durch die Projektwurzel."""
    assert value.startswith(WORKSPACE_FOLDER), f"unerwarteter Pfad: {value}"
    rest = value[len(WORKSPACE_FOLDER) :].strip("/\\")
    return REPO_ROOT / rest if rest else REPO_ROOT


def _effective_db_path(cwd: Path, database_url: str) -> Path:
    """Absoluter SQLite-Dateipfad, den SQLAlchemy fuer ``database_url`` ab ``cwd`` oeffnet.

    Entspricht der Aufloesung in ``backend/finue/db/engine.py`` und dem
    SQLite-Treiber: relative Pfade gelten relativ zum Prozess-cwd.
    """
    prefix = "sqlite:///"
    assert database_url.startswith(prefix), f"keine SQLite-Datei-URL: {database_url}"
    raw = Path(database_url[len(prefix) :])
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def test_launch_json_startet_backend_uvicorn() -> None:
    """Die Launch-Konfiguration startet ``finue.main:app`` auf 127.0.0.1:8000 (N-OPS-01)."""
    data = _load_json(VSCODE_DIR / "launch.json")
    assert data["version"] == "0.2.0"

    config = _launch_config()
    assert config["type"] == "debugpy"
    assert config["request"] == "launch"
    assert config["module"] == "uvicorn"

    args = config["args"]
    assert "finue.main:app" in args
    assert "--reload" in args
    assert _option(args, "--host") == "127.0.0.1"
    assert _option(args, "--port") == "8000"

    assert config["env"]["DATABASE_URL"] == DEV_DATABASE_URL
    assert _expand_workspace(config["env"]["PYTHONPATH"]) == REPO_ROOT / "backend"
    assert _expand_workspace(config["cwd"]) == REPO_ROOT


def test_tasks_json_legt_demo_daten_an() -> None:
    """Der Task ``Demo-Daten`` legt den Demo-Bestand per CLI an (N-OPS-01)."""
    data = _load_json(VSCODE_DIR / "tasks.json")
    assert data["version"] == "2.0.0"

    task = _demo_task()
    assert task["type"] == "shell"
    assert task["command"] == "uv run python -m finue demo --reset"

    assert _expand_workspace(task["options"]["cwd"]) == REPO_ROOT
    assert task["options"]["env"]["DATABASE_URL"] == DEV_DATABASE_URL


def test_launch_und_demo_task_nutzen_dieselbe_datenbank() -> None:
    """F5 und Task ``Demo-Daten`` oeffnen dieselbe SQLite-Datei (N-OPS-01).

    ``backend/finue/config.py`` reicht einen relativen ``sqlite:///``-Pfad
    unveraendert weiter; aufgeloest wird er gegen das Prozess-cwd. Laufen Launch
    und Task in verschiedenen Verzeichnissen, entstehen zwei getrennte Dateien
    und der Demo-Bestand fehlt im per F5 gestarteten Server. Der Test haelt
    cwd und ``DATABASE_URL`` beider Kontexte deckungsgleich und prueft den
    resultierenden absoluten Dateipfad.
    """
    launch = _launch_config()
    task = _demo_task()

    launch_cwd = _expand_workspace(launch["cwd"])
    task_cwd = _expand_workspace(task["options"]["cwd"])
    assert launch_cwd == task_cwd, f"cwd weicht ab: {launch_cwd} vs. {task_cwd}"

    launch_url = launch["env"]["DATABASE_URL"]
    task_url = task["options"]["env"]["DATABASE_URL"]
    assert launch_url == task_url, f"DATABASE_URL weicht ab: {launch_url} vs. {task_url}"

    launch_db = _effective_db_path(launch_cwd, launch_url)
    task_db = _effective_db_path(task_cwd, task_url)
    assert launch_db == task_db, f"verschiedene Datenbanken: {launch_db} vs. {task_db}"
    assert launch_db == (REPO_ROOT / "data" / "dev.db").resolve()


def test_config_loest_relativen_sqlite_pfad_ueber_prozess_cwd_auf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belegt die Ursache des Defekts in ``backend/finue/config.py`` (N-OPS-01).

    ``config.py`` macht aus einem relativen ``DATABASE_URL`` keinen absoluten
    Pfad; die Datei liegt damit im Prozess-cwd. Der Test haelt fest, dass die
    effektive Datei dem cwd folgt - unabhaengig davon, ob die Aufloesung im
    Settings-Objekt oder im Treiber passiert.
    """
    from finue.config import Settings

    monkeypatch.chdir(tmp_path)
    settings = Settings(database_url=DEV_DATABASE_URL)

    effective = _effective_db_path(Path.cwd(), settings.database_url)
    assert effective == (tmp_path / "data" / "dev.db").resolve()


def test_entwicklung_doku_existiert() -> None:
    """Die Entwicklungsdoku beschreibt den lokalen Start (N-OPS-01)."""
    text = (REPO_ROOT / "docs" / "entwicklung.md").read_text(encoding="utf-8")
    assert text.strip()
    assert "finue" in text


def test_vscode_nicht_ignoriert() -> None:
    """.vscode ist versioniert und steht nicht in .gitignore (N-OPS-01)."""
    lines = {line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".vscode" not in lines
    assert ".vscode/" not in lines
