"""Modelle für Sitzungen und MCP-Tokens.

Implementiert N-DB-01 (kein rohes SQL für Daten), N-DB-02 (Zeitstempel in
aware UTC), N-SEC-04 (Anmeldung über ein Session-Cookie; die Sitzung wird
serverseitig über den Hash des Session-Tokens identifiziert), N-SEC-06
(MCP-Zugriff nur mit Bearer-Token; Tokens nur als Hash in der DB, zufällig
erzeugt mit mindestens 256 Bit, widerrufbar) und F-CON-06 (aktive Sitzungen
sichtbar und einzeln abmeldbar).

Sicherheitsentscheidung (kein Klartext):
    Keines der beiden Modelle speichert ein Klartext-Token, ein Passwort, ein
    Secret oder eine Session-ID im Klartext. Persistiert wird ausschliesslich
    ``token_hash`` (Hash-Wert des Tokens). Die Erzeugung des Tokens, dessen
    Hashing und der Vergleich liegen in der Service-Schicht, nicht im Modell.
    Die Modelle enthalten daher bewusst keine Methode, die ein Klartext-Token
    erzeugt oder zurückgibt.

Sicherheitsentscheidung (bewusst kein ``VersionedMixin``):
    Anders als die Nachbarmodelle (Konto, Bankzugang, Buchung) verwenden
    ``UserSession`` und ``McpToken`` bewusst KEINE optimistische Sperre
    (F-CON-01/02). Begründung: Sitzungs- und Token-Datensätze werden nicht
    durch Nutzer-Bearbeitung editiert, sondern durch betriebliche
    Metadaten-Aktualisierungen (``last_seen_at``, ``last_used_at``) und durch
    Widerruf (``revoked_at``). Solche Aktualisierungen sind häufig (Heartbeat)
    und von Natur aus idempotent bzw. Setz-Semantik: ``revoked_at`` wird einmal
    gesetzt und bleibt gesetzt (F-CON-01: setzen statt umschalten). Eine
    Versionsspalte würde hier einen harmlosen ``last_seen_at``-Heartbeat mit
    einer parallelen Abmeldung kollidieren lassen und ein unnötiges
    ``StaleDataError`` erzeugen, obwohl kein Update verloren geht. Ein
    verlorenes Update, gegen das optimistisches Sperren schützt, entsteht
    durch ``NULL``-Setzen einmaliger Zeitstempel nicht.

    F-CON-02 nennt Sitzungen und MCP-Tokens nicht unter den versionierten
    Entitäten. Beide Modelle nutzen deshalb ``TimestampMixin`` (created_at,
    updated_at) und die fachspezifischen Zeitstempel explizit.
"""

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from finue.db.base import Base
from finue.db.mixins import TimestampMixin, UtcDateTime
from finue.timeutil import utc_now


class UserSession(Base, TimestampMixin):
    """Eine angemeldete Sitzung eines Geräts/Browsers (F-CON-06, N-SEC-04).

    Implementiert N-SEC-04 (serverseitige Sitzung, Mehrfachanmeldung auf
    mehreren Geräten) und F-CON-06 (Sitzung mit Gerät/Browser und letztem
    Zugriff in den Einstellungen sichtbar und einzeln abmeldbar).

    Es wird ausschliesslich ``token_hash`` persistiert, nie das Klartext-Token
    (N-SEC-06 sinngemäss, keine Klartext-Geheimnisse). Die Fehlversuchs- und
    Wartezeit-Logik aus N-SEC-04 ist nicht Teil dieses Modells; sie gehört in
    die Service-Schicht bzw. den Anmelde-Endpunkt.
    """

    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Hash des Session-Tokens (N-SEC-04): eindeutig, kein Klartext-Token.
    # String(255) fasst jeden gängigen Hash (SHA-256 Hex 64, SHA-512 Hex 128,
    # Argon2-Encoding ~96-120 Zeichen) und dient zugleich als Suchschlüssel.
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Letzter Zugriff dieser Sitzung (F-CON-06), aware UTC (N-DB-02)
    last_seen_at: Mapped[UtcDateTime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
    )

    # Ablaufzeitpunkt der Sitzung, aware UTC (N-DB-02); Pflichtfeld, da eine
    # Sitzung immer abläuft (N-SEC-04)
    expires_at: Mapped[UtcDateTime] = mapped_column(UtcDateTime, nullable=False)

    # Gerät/Browser für die Anzeige aktiver Sitzungen (F-CON-06). User-Agent-
    # Zeichenketten können lang sein, daher Text und optional.
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


class McpToken(Base, TimestampMixin):
    """Ein MCP-Zugriffstoken (Bearer) mit Berechtigungsstufe (N-SEC-06).

    Implementiert N-SEC-06: Der MCP-Zugriff erfolgt nur mit Bearer-Token, das
    Token liegt ausschliesslich als Hash in der Datenbank und ist widerrufbar.
    ``permission`` ist auf ``lesen`` oder ``lesen_schreiben`` beschränkt.

    ``revoked_at`` ist der Widerruf (N-SEC-06): ``NULL`` bedeutet aktiv, ein
    gesetzter Zeitstempel bedeutet widerrufen. Der Zeitstempel wird einmal
    gesetzt (Setz-Semantik), daher kein ``VersionedMixin`` (siehe Modul-Docstring).
    ``name`` ist das Anzeige-/Zuordnungslabel (z. B. "Laptop", "Skript") und
    enthält keine Geheimnisse (N-SEC-02).
    """

    __tablename__ = "mcp_token"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Anzeigename des Tokens, kein Geheimnis (N-SEC-02)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Hash des Bearer-Tokens (N-SEC-06): eindeutig, kein Klartext-Token.
    # String(255) fasst jeden gängigen Hash (SHA-256 Hex 64, SHA-512 Hex 128,
    # Argon2-Encoding ~96-120 Zeichen) für mindestens 256 Bit Tokens.
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Berechtigungsstufe, exakt auf zwei Werte beschränkt (N-SEC-06)
    permission: Mapped[str] = mapped_column(String(20), nullable=False)

    # Letzte Nutzung des Tokens (F-CON-06 / Statusseite); NULL = noch nie genutzt
    last_used_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)

    # Widerruf (N-SEC-06); NULL = aktiv, gesetzt = widerrufen
    revoked_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime, nullable=True)

    # Constraints zur Validierung der Berechtigungsstufe
    __table_args__ = (
        CheckConstraint(
            "permission IN ('lesen', 'lesen_schreiben')",
            name="ck_mcp_token_permission",
        ),
    )


# Kein Zirkelimport-Workaround nötig: UserSession und McpToken haben weder
# einen Foreign Key noch eine Beziehung zu account.py oder anderen Modellen.
# configure_mappers() ist daher bereits mit finue.models.security allein
# erfolgreich (siehe Tests).
