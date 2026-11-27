"""Tests der Sitzungsuebersicht und des Abmeldens einzelner Sitzungen (T-102).

Referenziert F-CON-06 (aktive Sitzungen mit Geraet/Browser und letztem Zugriff
sichtbar, einzeln abmeldbar, idempotent), N-SEC-02 (kein ``token_hash`` in
Antwort oder Log) und N-SEC-04 (Anlage der Sitzungen folgt in M10; hier werden
Sitzungszeilen direkt erzeugt). Alle Zugangsdaten sind erfunden (D8); es gibt
keine echten Token oder Passwoerter, nur offensichtliche Testmuster.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finue.config import Settings
from finue.main import create_app
from finue.models.security import UserSession
from finue.services.sessions import UNKNOWN_DEVICE, describe_device, list_active_sessions, revoke_session

# Offensichtliches Testmuster (kein echtes Geheimnis), dient nur dem Leckagetest.
LEAK_HASH = "totally-fake-token-hash-not-a-secret-0000000000"


def _session(client: TestClient):  # type: ignore[no-untyped-def]
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _add_session(
    client: TestClient,
    *,
    token_hash: str,
    expires_in: timedelta = timedelta(hours=12),
    user_agent: str | None = "Mozilla/5.0 (Windows NT 10.0) Firefox/121.0",
    last_seen_ago: timedelta = timedelta(0),
) -> int:
    """Legt eine Sitzungszeile direkt an (Anlage folgt mit N-SEC-04/M10)."""

    now = datetime.now(timezone.utc)
    with _session(client) as session:
        row = UserSession(
            token_hash=token_hash,
            expires_at=now + expires_in,
            user_agent=user_agent,
        )
        row.last_seen_at = now - last_seen_ago
        session.add(row)
        session.commit()
        return row.id


# ------------------------------------------------------------------ F-CON-06


def test_liste_zeigt_geraet_browser_und_letzten_zugriff(client: TestClient) -> None:
    """F-CON-06: aktive Sitzungen mit Geraetetext und letztem Zugriff."""

    _add_session(
        client,
        token_hash="tok-windows-firefox",
        user_agent="Mozilla/5.0 (Windows NT 10.0; rv:121.0) Gecko/20100101 Firefox/121.0",
    )
    _add_session(
        client,
        token_hash="tok-iphone-safari",
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile/15E148 Safari/604.1",
    )

    response = client.get("/api/sessions")

    assert response.status_code == 200
    body = response.json()
    devices = {item["device"] for item in body["items"]}
    assert devices == {"Firefox auf Windows", "Safari auf iPhone"}
    for item in body["items"]:
        assert item["id"] > 0
        assert item["last_seen_at"] is not None
        assert item["current"] is False


def test_abgelaufene_sitzung_wird_nicht_gelistet(client: TestClient) -> None:
    """F-CON-06: nur nicht abgelaufene Sitzungen gelten als aktiv."""

    _add_session(client, token_hash="tok-aktiv", expires_in=timedelta(hours=1))
    _add_session(client, token_hash="tok-abgelaufen", expires_in=timedelta(hours=-1))

    items = client.get("/api/sessions").json()["items"]

    assert len(items) == 1


def test_reihenfolge_zuletzt_genutzt_zuerst(client: TestClient) -> None:
    """F-CON-06: die zuletzt genutzte Sitzung steht oben."""

    _add_session(client, token_hash="tok-aelter", last_seen_ago=timedelta(hours=5))
    _add_session(client, token_hash="tok-neuer", last_seen_ago=timedelta(minutes=1))

    items = client.get("/api/sessions").json()["items"]

    assert items[0]["last_seen_at"] > items[1]["last_seen_at"]


def test_keine_sitzung_ist_aktuell_ohne_anmeldung(client: TestClient) -> None:
    """F-CON-06: in M4 (keine Anmeldung) ist keine Sitzung als aktuell markiert."""

    _add_session(client, token_hash="tok-eine")
    _add_session(client, token_hash="tok-zwei")

    items = client.get("/api/sessions").json()["items"]

    assert all(item["current"] is False for item in items)


def test_abmelden_entfernt_sitzung(client: TestClient) -> None:
    """F-CON-06: DELETE entfernt genau die gewaehlte Sitzung (204)."""

    entfernen = _add_session(client, token_hash="tok-zu-entfernen")
    behalten = _add_session(client, token_hash="tok-zu-behalten")

    response = client.delete(f"/api/sessions/{entfernen}")

    assert response.status_code == 204
    assert response.content == b""
    remaining = {item["id"] for item in client.get("/api/sessions").json()["items"]}
    assert remaining == {behalten}


def test_abmelden_ist_idempotent(client: TestClient) -> None:
    """F-CON-06: wiederholtes Abmelden und unbekannte IDs bleiben wirkungslos."""

    session_id = _add_session(client, token_hash="tok-idempotent")

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.delete("/api/sessions/999999").status_code == 204


def test_trennung_von_sitzungen_ueber_ids(client: TestClient) -> None:
    """F-CON-06: das Abmelden einer Sitzung laesst andere Sitzungen unberuehrt."""

    ids = [_add_session(client, token_hash=f"tok-{index}") for index in range(3)]

    client.delete(f"/api/sessions/{ids[1]}")

    remaining = {item["id"] for item in client.get("/api/sessions").json()["items"]}
    assert remaining == {ids[0], ids[2]}


def test_limit_und_cursor(client: TestClient) -> None:
    """SPEC F-MCP-11: Listenantwort ist begrenzt und paginiert."""

    for index in range(3):
        _add_session(client, token_hash=f"tok-page-{index}")

    erste = client.get("/api/sessions?limit=2").json()
    assert len(erste["items"]) == 2
    assert erste["next_cursor"] is not None

    zweite = client.get(f"/api/sessions?limit=2&cursor={erste['next_cursor']}").json()
    assert len(zweite["items"]) == 1
    assert zweite["next_cursor"] is None


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=abc", "cursor=kaputt"])
def test_ungueltige_listenparameter_sind_422(client: TestClient, query: str) -> None:
    """SPEC F-MCP-11/F-CON-02: ungueltige Listenparameter liefern 422 im Fehlerformat."""

    response = client.get(f"/api/sessions?{query}")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_endpunkt_haengt_an_der_auth_naht(tmp_path: Path) -> None:
    """N-SEC-04: mit aktivierter Anmeldung liefern beide Routen 401."""

    url = f"sqlite:///{(tmp_path / 'auth.db').as_posix()}"
    app = create_app(Settings(database_url=url, data_dir=tmp_path, auth_required=True))
    with TestClient(app) as client:
        assert client.get("/api/sessions").status_code == 401
        assert client.delete("/api/sessions/1").status_code == 401


# --------------------------------------------------------- N-SEC-02 (Leckage)


def test_token_hash_erscheint_nicht_in_der_antwort(client: TestClient) -> None:
    """N-SEC-02: der Token-Hash verlaesst das Backend nie."""

    session_id = _add_session(client, token_hash=LEAK_HASH)

    antwort = client.get("/api/sessions")
    assert LEAK_HASH not in antwort.text
    assert all("token_hash" not in item for item in antwort.json()["items"])

    abmelden = client.delete(f"/api/sessions/{session_id}")
    assert LEAK_HASH not in abmelden.text


def test_token_hash_erscheint_nicht_im_log(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """N-SEC-02: der Token-Hash taucht in keiner Logzeile auf."""

    caplog.set_level("DEBUG")
    session_id = _add_session(client, token_hash=LEAK_HASH)

    client.get("/api/sessions")
    client.delete(f"/api/sessions/{session_id}")

    assert LEAK_HASH not in caplog.text


# ---------------------------------------------------- Service (Einheitstests)


@pytest.mark.parametrize(
    ("user_agent", "erwartet"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Firefox auf Windows",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Safari auf macOS",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Chrome auf Android",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Edge auf Windows",
        ),
        (None, UNKNOWN_DEVICE),
        ("", UNKNOWN_DEVICE),
        ("curl/8.4.0", UNKNOWN_DEVICE),
    ],
)
def test_describe_device(user_agent: str | None, erwartet: str) -> None:
    """F-CON-06: der User-Agent wird zu einem kurzen Geraetetext verkuerzt."""

    assert describe_device(user_agent) == erwartet


def test_service_markiert_aktuelle_sitzung(client: TestClient) -> None:
    """F-CON-06: ``current_session_id`` kennzeichnet genau die laufende Sitzung."""

    erste = _add_session(client, token_hash="tok-current-a")
    zweite = _add_session(client, token_hash="tok-current-b")

    with _session(client) as session:
        summarys = list_active_sessions(
            session,
            now=datetime.now(timezone.utc),
            current_session_id=zweite,
        )

    markiert = {summary.id: summary.current for summary in summarys}
    assert markiert == {erste: False, zweite: True}


def test_service_abmelden_unbekannter_id_ist_false(client: TestClient) -> None:
    """F-CON-06: der Service meldet ein unbekanntes ``id`` als nicht entfernt."""

    with _session(client) as session:
        assert revoke_session(session, 999999) is False
