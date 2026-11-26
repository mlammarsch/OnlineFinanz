"""Tests fuer die Alembic-Einrichtung (T-028).

Referenziert N-DB-01 (kein rohes SQL) und N-DB-03 (Schema-Versionierung ueber
Migrationen).

Geprueft wird ueber die Alembic-Programm-API (``alembic.config``,
``alembic.script``) statt ueber CLI-Subprozesse:

* ``alembic.ini`` ist ladbar und das Migrationsverzeichnis existiert,
* ``versions/`` ist vorhanden und enthaelt die Revisionen 0001, 0002 und 0003
  (siehe T-029, T-063 und T-085),
* die Migrationshistorie hat genau einen Head und ist eine lineare Kette ohne
  Verzweigung (N-DB-03).
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


def test_migration_history_is_single_linear_head() -> None:
    """Die Historie hat genau einen Head und die Kette 0001 -> 0002 -> 0003 (N-DB-03).

    Die Pruefung ist generisch: Sie verlangt genau einen Head, die Basis 0001
    und eine lineare Kette ohne Verzweigung. Die fuer T-063/T-085 geforderten
    Kanten 0001 -> 0002 und 0002 -> 0003 werden zusaetzlich geprueft; kuenftige
    Migrationen (0004, ...) brechen den Test nicht, solange die Historie linear
    bleibt.
    """
    script = ScriptDirectory.from_config(load_config())

    heads = list(script.get_heads())
    assert len(heads) == 1, f"Erwartet wurde genau ein Head, gefunden: {heads}"
    assert script.get_base() == "0001", f"Erwartete Basis '0001', gefunden: {script.get_base()}"

    revision_0002 = script.get_revision("0002")
    assert revision_0002 is not None, "Revision 0002 fehlt in versions/"
    assert revision_0002.down_revision == "0001", (
        f"Revision 0002 muss auf 0001 folgen, gefunden: {revision_0002.down_revision}"
    )

    revision_0003 = script.get_revision("0003")
    assert revision_0003 is not None, "Revision 0003 fehlt in versions/"
    assert revision_0003.down_revision == "0002", (
        f"Revision 0003 muss auf 0002 folgen, gefunden: {revision_0003.down_revision}"
    )

    for revision in script.walk_revisions():
        assert len(revision.nextrev) <= 1, (
            f"Migrationshistorie verzweigt bei {revision.revision}: Nachfolger {revision.nextrev}"
        )
