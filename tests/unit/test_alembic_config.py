"""Tests fuer die Alembic-Einrichtung (T-028).

Referenziert N-DB-01 (kein rohes SQL) und N-DB-03 (Schema-Versionierung ueber
Migrationen).

Geprueft wird ueber die Alembic-Programm-API (``alembic.config``,
``alembic.script``) statt ueber CLI-Subprozesse:

* ``alembic.ini`` ist ladbar und das Migrationsverzeichnis existiert,
* ``versions/`` ist vorhanden und enthaelt die Revision 0001 (siehe T-029),
* die Migrationshistorie hat genau einen Head (Revision 0001).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ALEMBIC_INI: Final[Path] = PROJECT_ROOT / "alembic.ini"


def load_config() -> Config:
    """Laedt alembic.ini ueber die Programm-API."""
    assert ALEMBIC_INI.is_file(), f"alembic.ini fehlt: {ALEMBIC_INI}"
    return Config(str(ALEMBIC_INI))


def test_alembic_config_is_loadable() -> None:
    """alembic.ini ist ladbar, Script-Verzeichnis existiert (N-DB-03)."""
    config = load_config()
    assert config.get_main_option("script_location"), "script_location ist nicht gesetzt"

    script = ScriptDirectory.from_config(config)
    assert Path(script.dir).is_dir(), f"Migrationsverzeichnis fehlt: {script.dir}"
    assert script.versions is not None
    assert Path(script.versions).is_dir(), f"versions/-Verzeichnis fehlt: {script.versions}"


def test_initial_migration_is_single_head() -> None:
    """Die Migrationshistorie beginnt mit genau einem Head: Revision 0001 (N-DB-03).

    Bis T-028 war die Historie leer; mit T-029 (erste Migration) muss genau die
    Revision 0001 als einzelner Head vorliegen.
    """
    script = ScriptDirectory.from_config(load_config())
    heads = list(script.get_heads())
    assert heads == ["0001"], f"Erwartet wurde genau ein Head '0001', gefunden: {heads}"
