"""FastAPI-Dependencies fuer die API-Schicht.

Stellt die Datenbank-Session als Dependency bereit. Die Engine und die
Session-Factory werden beim App-Start im Lifespan gesetzt (``finue.main``) und
hier aus ``app.state`` gelesen, damit kein globaler Zustand noetig ist
(N-DB-01, N-OPS-05).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker


def get_session(request: Request) -> Iterator[Session]:
    """Liefert eine SQLAlchemy-Session als FastAPI-Dependency.

    Liest die Session-Factory aus ``app.state.session_factory`` (im Lifespan von
    ``finue.main`` gesetzt), oeffnet eine Session und schliesst sie nach dem
    Request wieder. Der Generator-Aufbau stellt das garantierte Schliessen auch
    bei Fehlern in der Route sicher (N-DB-01).

    Args:
        request: Aktueller FastAPI-Request, ueber den ``app.state`` erreichbar ist.

    Yields:
        Eine offene SQLAlchemy-Session.
    """
    session_factory: sessionmaker[Session] = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
