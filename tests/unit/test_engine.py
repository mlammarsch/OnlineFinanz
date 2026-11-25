"""Tests für Datenbank-Engine und Session-Verwaltung.

Implementiert N-DB-01 (kein rohes SQL für Daten) und N-DB-04 (SQLite WAL mit busy_timeout).
Prüft PRAGMA-Konfiguration für SQLite und Session-Factory-Funktionalität.
"""

from pathlib import Path

from sqlalchemy import text

from finue.db.engine import create_db_engine, make_session_factory


class TestCreateDbEngine:
    """Tests für create_db_engine() Funktion."""

    def test_sqlite_file_db_creates_parent_directory(self, tmp_path: Path) -> None:
        """Prüft, dass parent directory für Datei-DB erstellt wird.

        Testet N-DB-01, N-DB-04: Engine für Datei-basierte SQLite-DB erstellt
        Verzeichnis automatisch.
        """
        db_dir = tmp_path / "data" / "nested" / "dir"
        db_path = db_dir / "test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # Verzeichnis sollte noch nicht existieren
        assert not db_dir.exists()

        # Engine erstellen
        engine = create_db_engine(db_url)

        # Verzeichnis sollte jetzt existieren
        assert db_dir.exists()

        # Engine ist brauchbar
        assert engine is not None
        engine.dispose()

    def test_sqlite_file_db_wal_enabled(self, tmp_path: Path) -> None:
        """Prüft, dass WAL für Datei-basierte SQLite-DB aktiviert ist.

        Testet N-DB-04, D-12: journal_mode muss WAL sein für bessere Concurrency.
        """
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        engine = create_db_engine(db_url)

        # PRAGMA abfragen um WAL-Modus zu prüfen
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA journal_mode"))
            mode = result.scalar()

        assert mode.upper() == "WAL"
        engine.dispose()

    def test_sqlite_file_db_busy_timeout_set(self, tmp_path: Path) -> None:
        """Prüft, dass busy_timeout für SQLite gesetzt ist.

        Testet N-DB-04, D-12: busy_timeout muss 5000 ms sein.
        """
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        engine = create_db_engine(db_url)

        # PRAGMA abfragen um busy_timeout zu prüfen
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA busy_timeout"))
            timeout_ms = result.scalar()

        assert timeout_ms == 5000
        engine.dispose()

    def test_sqlite_file_db_foreign_keys_enabled(self, tmp_path: Path) -> None:
        """Prüft, dass foreign_keys für SQLite aktiviert sind.

        Testet N-DB-04: foreign_keys muss ON sein für referenzielle Integrität.
        """
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        engine = create_db_engine(db_url)

        # PRAGMA abfragen um foreign_keys zu prüfen
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA foreign_keys"))
            enabled = result.scalar()

        assert enabled == 1  # 1 = ON
        engine.dispose()

    def test_sqlite_memory_db_no_wal(self) -> None:
        """Prüft, dass :memory: DB kein WAL hat.

        Testet N-DB-04: WAL ist für :memory: nicht unterstützt.
        """
        db_url = "sqlite:///:memory:"

        engine = create_db_engine(db_url)

        # PRAGMA abfragen um journal_mode zu prüfen
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA journal_mode"))
            mode = result.scalar()

        # :memory: kann MEMORY, DELETE oder andere Modi haben, aber nicht WAL
        assert mode.upper() != "WAL"
        engine.dispose()

    def test_sqlite_memory_db_has_busy_timeout(self) -> None:
        """Prüft, dass :memory: DB trotzdem busy_timeout hat.

        Testet N-DB-04: busy_timeout sollte auch für :memory: gesetzt sein.
        """
        db_url = "sqlite:///:memory:"

        engine = create_db_engine(db_url)

        # PRAGMA abfragen um busy_timeout zu prüfen
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA busy_timeout"))
            timeout_ms = result.scalar()

        assert timeout_ms == 5000
        engine.dispose()

    def test_sqlite_memory_db_has_foreign_keys(self) -> None:
        """Prüft, dass :memory: DB foreign_keys aktiviert hat.

        Testet N-DB-04: foreign_keys sollte auch für :memory: ON sein.
        """
        db_url = "sqlite:///:memory:"

        engine = create_db_engine(db_url)

        # PRAGMA abfragen um foreign_keys zu prüfen
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA foreign_keys"))
            enabled = result.scalar()

        assert enabled == 1  # 1 = ON
        engine.dispose()


class TestMakeSessionFactory:
    """Tests für make_session_factory() Funktion."""

    def test_session_factory_creates_usable_sessions(self, tmp_path: Path) -> None:
        """Prüft, dass Session-Factory brauchbare Sessions erstellt.

        Testet N-DB-01: Sessions können für ORM-Operationen verwendet werden.
        """
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        engine = create_db_engine(db_url)
        session_factory = make_session_factory(engine)

        # Session erstellen und verwenden
        session = session_factory()
        assert session is not None

        # Session sollte verwendbar sein (einfache Abfrage)
        result = session.execute(text("SELECT 1"))
        value = result.scalar()
        assert value == 1

        session.close()
        engine.dispose()

    def test_session_factory_has_expire_on_commit_false(self, tmp_path: Path) -> None:
        """Prüft, dass Session-Factory mit expire_on_commit=False konfiguriert ist.

        Testet N-DB-01: Objekte sollten nach Commit nicht aus dem Cache entfernt werden.
        """
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        engine = create_db_engine(db_url)
        session_factory = make_session_factory(engine)

        # expire_on_commit sollte False sein
        assert session_factory.kw.get("expire_on_commit") is False

        engine.dispose()
