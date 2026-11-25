"""Policy-Tests fuer Alembic-Migrationen (T-031).

Referenziert N-DB-03 (Schema-Versionierung ueber Alembic-Migrationen, nur
vorwaerts). Drei Pruefungen:

1. Ein Upgrade von einer leeren, Datei-basierten SQLite-Datenbank laeuft durch
   und bringt das Schema auf ``head`` (ueber ``finue.db.migrate``; kein
   paralleler Alembic-Code im Test).
2. Nach dem Upgrade gibt es keinen Drift zwischen den SQLAlchemy-Modellen
   (``Base.metadata`` aus ``finue.models``) und dem migrierten Schema: die von
   ``alembic.autogenerate.compare_metadata`` berechnete Diff-Liste ist leer.
3. Keine Migrationsdatei unter ``backend/finue/migrations/versions/`` definiert
   ein ``downgrade()`` mit Schema-Logik (Vorwaerts-Policy, N-DB-03). Erlaubt
   sind ausschliesslich ``raise NotImplementedError(...)``, ``pass`` und ein
   Docstring. Die Pruefung laeuft generisch ueber ALLE Migrationsdateien, nicht
   nur ``0001_initial.py``, damit kuenftige Migrationen automatisch mitgeprueft
   werden.

Kein rohes SQL (N-DB-01): der Test nutzt ausschliesslich die Alembic- und
SQLAlchemy-Programm-API.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

import finue.models  # noqa: F401  registriert ALLE Tabellen in Base.metadata
from finue.db.base import Base
from finue.db.engine import create_db_engine
from finue.db.migrate import current_revision, head_revision, upgrade_to_head

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
VERSIONS_DIR: Final[Path] = PROJECT_ROOT / "backend" / "finue" / "migrations" / "versions"


def _sqlite_url(db_path: Path) -> str:
    """Bildet eine SQLAlchemy-URL fuer eine Datei-basierte SQLite-Datenbank."""
    return f"sqlite:///{db_path.as_posix()}"


def test_upgrade_from_empty_database(tmp_path: Path) -> None:
    """Upgrade einer leeren DB hebt das Schema bis ``head`` an (N-DB-03, N-OPS-04).

    Die Datenbank ist eine echte, Datei-basierte SQLite-Datei in ``tmp_path``
    (keine ``:memory:``-URL), damit das Ergebnis auf der Platte ueberpruefbar
    ist. Vor dem Upgrade traegt sie keine Revision, danach steht sie auf head.
    """
    db_path = tmp_path / "migration_policy_empty.db"
    url = _sqlite_url(db_path)

    engine = create_db_engine(url)
    try:
        assert current_revision(engine) is None
        upgrade_to_head(url)
        expected_head = head_revision()
        assert expected_head is not None
        assert current_revision(engine) == expected_head
    finally:
        engine.dispose()

    assert db_path.is_file(), "upgrade_to_head hat keine SQLite-Datei angelegt"


def test_models_have_no_drift_to_migrated_schema(tmp_path: Path) -> None:
    """Kein Drift zwischen Modellen und migriertem Schema (N-DB-01, N-DB-03).

    Nach ``upgrade_to_head`` vergleicht ``compare_metadata`` das migrierte
    Schema mit ``Base.metadata``. Eine leere Diff-Liste bedeutet: ein weiterer
    Autogenerate-Lauf wuerde keine Aenderung erzeugen.
    """
    db_path = tmp_path / "migration_policy_drift.db"
    url = _sqlite_url(db_path)
    upgrade_to_head(url)

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True}
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"Drift zwischen Modellen und migriertem Schema: {diff}"


def test_every_migration_only_migrates_forward() -> None:
    """Keine Migrationsdatei baut mit Schema-Logik zurueck (N-DB-03: nur vorwaerts).

    Geprueft wird generisch ueber alle ``*.py``-Dateien im versions-Verzeichnis.
    Der Funktionskoerper von ``downgrade()`` wird per AST geprueft (praeziser und
    robuster als String-Matching): erlaubt sind nur ``raise
    NotImplementedError(...)``, ``pass`` und ein Docstring.
    """
    migration_files = sorted(VERSIONS_DIR.glob("*.py"))
    assert migration_files, f"Keine Migrationsdateien gefunden in {VERSIONS_DIR}"

    violations: list[str] = []
    for migration_file in migration_files:
        violations.extend(_downgrade_violations(migration_file))

    assert violations == [], "Vorwaerts-Policy verletzt (N-DB-03):\n" + "\n".join(violations)


def _downgrade_violations(source_path: Path) -> list[str]:
    """Prueft den ``downgrade``-Rumpf einer Migrationsdatei und meldet Verstoesse.

    Liefert eine Liste lesbarer Beschreibungen; leer heisst konform. Eine Datei
    ohne ``downgrade``-Funktion ist kein Verstoss (sie definiert dann keinen
    Rueckbau mit Inhalt).
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            return _check_downgrade_body(source_path, node)
    return []


def _check_downgrade_body(source_path: Path, func: ast.FunctionDef) -> list[str]:
    """Prueft den Rumpf von ``downgrade()`` auf erlaubte Anweisungen."""
    violations: list[str] = []
    has_guard = False

    for statement in func.body:
        if _is_docstring(statement):
            continue
        if isinstance(statement, ast.Pass):
            has_guard = True
            continue
        if isinstance(statement, ast.Raise) and _raises_not_implemented(statement):
            has_guard = True
            continue
        violations.append(
            f"{source_path.name}:{statement.lineno}: "
            f"unerlaubte Anweisung in downgrade(): {type(statement).__name__}"
        )

    if not has_guard:
        violations.append(
            f"{source_path.name}:{func.lineno}: "
            "downgrade() enthaelt weder raise NotImplementedError noch pass"
        )
    return violations


def _is_docstring(statement: ast.stmt) -> bool:
    """Erkennt einen reinen String-Ausdruck (Docstring) als Anweisung."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _raises_not_implemented(statement: ast.Raise) -> bool:
    """Prueft, ob ``raise`` ein ``NotImplementedError`` ausloest (mit/ohne Aufruf)."""
    exc = statement.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
