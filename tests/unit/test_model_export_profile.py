"""Tests fuer das Modell der Exportprofile (T-085).

Referenziert N-DB-01 (kein rohes SQL), F-EXP-03 (speicherbare CSV-Exportprofile
mit Optionen und Spaltenauswahl) und F-CON-02 (Versionsspalte fuer
optimistisches Sperren).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.export_profile import ExportProfile


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_export_profile_defaults_and_roundtrip(factory: sessionmaker[Session]) -> None:
    """F-EXP-03: Profil speichert Optionen und Spaltenauswahl als JSON."""
    with factory() as session:
        profile = ExportProfile(name="Finanzblick-angelehnt", is_default=True)
        session.add(profile)
        session.commit()
        profile_id = profile.id
        assert profile.version == 1

    with factory() as session:
        stored = session.get(ExportProfile, profile_id)
        assert stored is not None
        assert stored.name == "Finanzblick-angelehnt"
        assert stored.is_default is True
        # Standardwerte sind leere Container (options -> dict, columns -> list).
        assert stored.options == {}
        assert stored.columns == []

    with factory() as session:
        stored = session.get(ExportProfile, profile_id)
        assert stored is not None
        stored.options = {"delimiter": ";", "decimal": "deutsch", "date_format": "TT.MM.JJJJ", "encoding": "utf-8"}
        stored.columns = ["booking_date", "amount", "note"]
        session.commit()
        assert stored.version == 2

    with factory() as session:
        stored = session.get(ExportProfile, profile_id)
        assert stored is not None
        assert stored.options["delimiter"] == ";"
        assert stored.columns == ["booking_date", "amount", "note"]


def test_export_profile_name_is_unique(factory: sessionmaker[Session]) -> None:
    """F-EXP-03: Der Profilname ist eindeutig."""
    with factory() as session:
        session.add(ExportProfile(name="Standard"))
        session.commit()

    with factory() as session:
        session.add(ExportProfile(name="Standard"))
        with pytest.raises(IntegrityError):
            session.commit()
