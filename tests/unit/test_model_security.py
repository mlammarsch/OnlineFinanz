"""Tests für die Modelle Sitzung und MCP-Token.

Referenziert N-DB-01 (kein rohes SQL), N-DB-02 (Zeitstempel in aware UTC),
N-SEC-04 (Anmeldung über Session-Cookie, mehrere Sitzungen gleichzeitig),
N-SEC-06 (MCP-Zugriff nur mit Bearer-Token, Tokens nur als Hash in der DB,
mindestens 256 Bit, widerrufbar) und F-CON-06 (aktive Sitzungen sichtbar und
einzeln abmeldbar).

Die Testdaten enthalten KEINE echten Tokens, Passwörter oder Klartext-
Geheimnisse: ``token_hash``-Werte sind offensichtliche Hex-Testmuster.
Zusätzlich belegt ein Test, dass keines der Modelle ein Klartext-Feld für
Token, Passwort, Secret oder Session-ID besitzt (nur Hashes).
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.security import McpToken, UserSession

# Offensichtliche Hex-Testmuster (64 Zeichen), keine echten Hashes/Secrets.
SESSION_HASH = "0123456789abcdef" * 4
MCP_HASH = "fedcba9876543210" * 4


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    """Erstellt eine In-Memory-SQLite für Tests."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _session_hash(seed: str) -> str:
    """Baut ein 64-stelliges Hex-Testmuster (kein echtes Geheimnis)."""
    return (seed * 64)[:64]


def test_security_mapper_entry_point() -> None:
    """N-DB-01: Modell kann allein importiert und konfiguriert werden.

    Belegt, dass kein Zirkelimport-Workaround nötig ist: configure_mappers()
    ist bereits mit finue.models.security allein erfolgreich.
    """
    code = """
from sqlalchemy.orm import configure_mappers
import finue.models.security
configure_mappers()
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Mapper configuration fehlgeschlagen: {result.stderr}"
    assert "OK" in result.stdout


def test_create_user_session(factory) -> None:  # type: ignore[no-untyped-def]
    """Eine Sitzung wird angelegt und gelesen (N-SEC-04, F-CON-06)."""
    expires = datetime.now(timezone.utc) + timedelta(hours=12)
    with factory() as session:
        user_session = UserSession(
            token_hash=SESSION_HASH,
            expires_at=expires,
            user_agent="Mozilla/5.0 (Test) Firefox/120",
        )
        session.add(user_session)
        session.commit()
        session_id = user_session.id

    with factory() as session:
        retrieved = session.query(UserSession).filter_by(id=session_id).one()
        assert retrieved.token_hash == SESSION_HASH
        assert retrieved.user_agent == "Mozilla/5.0 (Test) Firefox/120"
        assert retrieved.expires_at == expires


def test_user_session_timestamps_aware_utc(factory) -> None:  # type: ignore[no-untyped-def]
    """created_at, last_seen_at und expires_at sind aware UTC (N-DB-02)."""
    with factory() as session:
        user_session = UserSession(
            token_hash=SESSION_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session.add(user_session)
        session.commit()
        session_id = user_session.id

    with factory() as session:
        retrieved = session.query(UserSession).filter_by(id=session_id).one()
        for value in (retrieved.created_at, retrieved.last_seen_at, retrieved.expires_at):
            assert isinstance(value, datetime)
            assert value.tzinfo == timezone.utc


def test_user_session_user_agent_is_optional(factory) -> None:  # type: ignore[no-untyped-def]
    """user_agent ist optional (F-CON-06)."""
    with factory() as session:
        user_session = UserSession(
            token_hash=SESSION_HASH,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            user_agent=None,
        )
        session.add(user_session)
        session.commit()
        session_id = user_session.id

    with factory() as session:
        retrieved = session.query(UserSession).filter_by(id=session_id).one()
        assert retrieved.user_agent is None


def test_user_session_token_hash_unique(factory) -> None:  # type: ignore[no-untyped-def]
    """Zwei Sitzungen können nicht denselben Token-Hash teilen (N-SEC-04)."""
    with factory() as session:
        session.add(
            UserSession(
                token_hash=SESSION_HASH,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        session.commit()

    with factory() as session:
        session.add(
            UserSession(
                token_hash=SESSION_HASH,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_create_mcp_token(factory) -> None:  # type: ignore[no-untyped-def]
    """Ein MCP-Token wird angelegt; last_used_at und revoked_at sind NULL."""
    with factory() as session:
        token = McpToken(name="Laptop", token_hash=MCP_HASH, permission="lesen")
        session.add(token)
        session.commit()
        token_id = token.id

    with factory() as session:
        retrieved = session.query(McpToken).filter_by(id=token_id).one()
        assert retrieved.name == "Laptop"
        assert retrieved.token_hash == MCP_HASH
        assert retrieved.permission == "lesen"
        assert retrieved.last_used_at is None
        assert retrieved.revoked_at is None
        assert retrieved.created_at.tzinfo == timezone.utc


def test_mcp_token_accepts_both_permissions(factory) -> None:  # type: ignore[no-untyped-def]
    """Beide erlaubten Berechtigungsstufen werden akzeptiert (N-SEC-06)."""
    with factory() as session:
        session.add(McpToken(name="Lesen", token_hash=_session_hash("a"), permission="lesen"))
        session.add(
            McpToken(
                name="Schreiben",
                token_hash=_session_hash("b"),
                permission="lesen_schreiben",
            )
        )
        session.commit()

    with factory() as session:
        assert session.query(McpToken).count() == 2


def test_mcp_token_rejects_invalid_permission(factory) -> None:  # type: ignore[no-untyped-def]
    """Eine unbekannte Berechtigungsstufe wird abgelehnt (N-SEC-06)."""
    with factory() as session:
        session.add(McpToken(name="Ungueltig", token_hash=MCP_HASH, permission="admin"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_mcp_token_token_hash_unique(factory) -> None:  # type: ignore[no-untyped-def]
    """Der Token-Hash ist eindeutig; ein Duplikat wirft IntegrityError (N-SEC-06).

    DoD-Kernprüfung: zweites Anlegen mit identischem Hash schlägt fehl.
    """
    with factory() as session:
        session.add(McpToken(name="Erster", token_hash=MCP_HASH, permission="lesen"))
        session.commit()

    with factory() as session:
        session.add(McpToken(name="Zweiter", token_hash=MCP_HASH, permission="lesen"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_mcp_token_revocation_sets_timestamp(factory) -> None:  # type: ignore[no-untyped-def]
    """Widerruf setzt revoked_at (NULL = aktiv, gesetzt = widerrufen, N-SEC-06)."""
    with factory() as session:
        token = McpToken(name="Skript", token_hash=MCP_HASH, permission="lesen_schreiben")
        session.add(token)
        session.commit()
        token_id = token.id

    revoked_at = datetime.now(timezone.utc)
    with factory() as session:
        retrieved = session.query(McpToken).filter_by(id=token_id).one()
        retrieved.revoked_at = revoked_at
        session.commit()

    with factory() as session:
        retrieved = session.query(McpToken).filter_by(id=token_id).one()
        assert retrieved.revoked_at == revoked_at
        assert retrieved.revoked_at.tzinfo == timezone.utc


def test_token_hash_columns_fit_256_bit_hashes(factory) -> None:  # type: ignore[no-untyped-def]
    """token_hash-Spalten fassen mindestens einen 256-Bit-Hash (N-SEC-06).

    Ein SHA-256-Hash als Hex ist 64 Zeichen lang; die Spalte muss das fassen.
    """
    for model in (UserSession, McpToken):
        column_type = model.__table__.c.token_hash.type
        assert column_type.length is not None
        assert column_type.length >= 64


def test_models_have_no_plaintext_secret_columns() -> None:
    """Keines der Modelle hat ein Klartext-Feld (nur Hashes).

    Prüft, dass keine Spalte ein Token, Passwort, Secret oder eine Session-ID
    im Klartext speichert (N-SEC-04, N-SEC-06).
    """
    forbidden = {
        "token",
        "token_plain",
        "plain_token",
        "password",
        "password_hash_plain",
        "secret",
        "session_id",
        "session_token",
        "pin",
    }
    for model in (UserSession, McpToken):
        column_names = set(model.__table__.c.keys())
        assert column_names.isdisjoint(forbidden), f"Klartext-Feld in {model.__name__}"
        assert "token_hash" in column_names
