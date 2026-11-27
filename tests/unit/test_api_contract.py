"""Vertragstest der API als Guardrail (T-086).

Dieser Test haelt die Konventionen aus ``docs/api.md`` fest und wird von jedem
Folge-Task erweitert:

- Jede schreibende Route (POST/PUT/PATCH/DELETE) unter ``/api`` steht in der
  Vertragstabelle :data:`CONTRACT` mit Art ``version``, ``setzen``, ``anlegen``
  oder ``aktion`` und einer Begruendung (F-CON-02, F-CON-03).
- Jede Listenroute ist als ``is_list`` markiert und hat den Parameter ``limit``
  (SPEC F-MCP-11).
- Jede ``/api``-Route ausser ``/api/health*`` haengt an der Auth-Naht
  ``require_user`` (N-SEC-04).
- Unbekannte Routen und veraltete Eintraege machen den Test rot.

Bei nur Health-Routen ist keine schreibende Route vorhanden; die Prueffunktionen
werden darum zusaetzlich an synthetischen Apps positiv und negativ belegt.
Referenzierte Anforderungen: F-CON-02, F-CON-03, N-SEC-04, SPEC F-MCP-11.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from finue.api.deps import require_user
from finue.config import Settings
from finue.main import create_app

#: Methoden, die den Datenbestand aendern und daher einen Vertragseintrag brauchen.
CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Zulaessige Art eines schreibenden Eintrags (DoD T-086).
CHANGING_KINDS = frozenset({"version", "setzen", "anlegen", "aktion"})

#: Health-Routen liegen ausserhalb der Auth-Naht (N-OPS-04).
HEALTH_PREFIX = "/api/health"


@dataclass(frozen=True)
class RouteContract:
    """Vertrag einer Route: Methoden, Art, Begruendung, Liste, Auth-Naht."""

    methods: frozenset[str]
    kind: str | None
    reason: str
    is_list: bool = False
    auth: bool = True


# Vertragstabelle: Folge-Tasks ergaenzen je Route einen Eintrag.
CONTRACT: dict[str, RouteContract] = {
    "/api/health": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Liveness-Check ohne Datenbankzugriff (N-OPS-04)",
        auth=False,
    ),
    "/api/health/ready": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Readiness-Check mit Datenbank, Schema und Zeitplaner (N-OPS-04)",
        auth=False,
    ),
    "/api/accounts": RouteContract(
        frozenset({"GET", "POST"}),
        kind="anlegen",
        reason="Liste aktiver/archivierter Konten mit Gesamtsaldo; Offline-Konto anlegen (F-KTO-01/02/05b/06)",
        is_list=True,
    ),
    "/api/accounts/{account_id}": RouteContract(
        frozenset({"GET", "PATCH", "DELETE"}),
        kind="version",
        reason="Kontodetails ansehen/pflegen und Konto loeschen mit Versionspruefung (F-KTO-04/07, F-CON-02)",
    ),
    "/api/accounts/{account_id}/archive": RouteContract(
        frozenset({"POST"}),
        kind="version",
        reason="Konto archivieren mit Versionspruefung (F-KTO-06, F-CON-02)",
    ),
    "/api/accounts/{account_id}/restore": RouteContract(
        frozenset({"POST"}),
        kind="version",
        reason="Konto wiederherstellen mit Versionspruefung (F-KTO-06, F-CON-02)",
    ),
    "/api/groups": RouteContract(
        frozenset({"GET", "POST"}),
        kind="anlegen",
        reason="Gruppenliste mit Mitgliedern, Kontenzahl und Summe; Gruppe anlegen (F-KTO-03)",
        is_list=True,
    ),
    "/api/groups/order": RouteContract(
        frozenset({"PUT"}),
        kind="setzen",
        reason="Neuordnung der Gruppen als ID-Liste (Setz-Semantik, F-KTO-03)",
    ),
    "/api/groups/{group_id}": RouteContract(
        frozenset({"PATCH", "DELETE"}),
        kind="version",
        reason="Gruppe umbenennen oder loeschen mit Versionspruefung; Konten bleiben (F-KTO-03, F-CON-02)",
    ),
    "/api/accounts/{account_id}/reconciliation": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Abstimmungsuebersicht eines Kontos (F-ABS-03)",
    ),
    "/api/groups/{group_id}/reconciliation": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Abstimmungsuebersicht einer Gruppe mit Fremdwaehrung getrennt (F-ABS-03)",
    ),
    "/api/banks": RouteContract(
        frozenset({"GET", "POST"}),
        kind="anlegen",
        reason="Banken-Stammdaten auflisten; Bank anlegen, keine Vorbelegung (F-KTO-10)",
        is_list=True,
    ),
    "/api/banks/{bank_id}": RouteContract(
        frozenset({"GET", "PATCH", "DELETE"}),
        kind="version",
        reason="Bank ansehen/pflegen/loeschen mit Versionspruefung (nur ohne Bankzugaenge), F-KTO-10",
    ),
    "/api/accounts/{account_id}/icon": RouteContract(
        frozenset({"GET", "PUT", "DELETE"}),
        kind="version",
        reason="Konto-Icon hochladen/entfernen mit Versionspruefung; GET liefert PNG mit ETag (F-KTO-11, F-CON-02)",
    ),
    "/api/banks/{bank_id}/icon": RouteContract(
        frozenset({"GET", "PUT", "DELETE"}),
        kind="version",
        reason="Bank-Icon hochladen/entfernen mit Versionspruefung; GET liefert PNG mit ETag (F-KTO-10/11, F-CON-02)",
    ),
    "/api/search": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Buchungssuche mit Filter, Sortierung, Trefferzahl und Summe (F-SUC-01..07, F-ABS-04/05)",
        is_list=True,
    ),
    "/api/accounts/{account_id}/transactions": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Buchungsliste eines Kontos mit Vormerkungen, Tagessalden und Keyset-Paginierung (F-BUC-01..04, 06)",
        is_list=True,
    ),
    "/api/groups/{group_id}/transactions": RouteContract(
        frozenset({"GET"}),
        kind="lesen",
        reason="Buchungsliste einer Gruppe mit Kontokennzeichnung je Zeile (F-BUC-05)",
        is_list=True,
    ),
    "/api/export": RouteContract(
        frozenset({"POST"}),
        kind="aktion",
        reason="Export als Datei/Zwischenablage-Text ohne Aenderung des Datenbestands (F-EXP-01/02/04/05)",
    ),
}


def _iter_api_routes(routes: Iterable[Any]) -> Iterator[APIRoute]:
    """Geht verschachtelte Router durch und liefert die APIRoutes (FastAPI 0.165).

    ``include_router`` haengt seit FastAPI 0.16x einen ``_IncludedRouter``
    statt flacher Routen ein; dessen ``original_router.routes`` wird rekursiv
    ausgewertet.
    """

    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _iter_api_routes(nested.routes)
            continue
        sub = getattr(route, "routes", None)
        if sub:
            yield from _iter_api_routes(sub)


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Alle APIRoutes unter ``/api`` (ohne Docs-, OpenAPI- und MCP-Routen)."""

    return [route for route in _iter_api_routes(app.routes) if route.path.startswith("/api")]


def _dependant_contains(dependant: Any, dependency: Any) -> bool:
    """Prueft rekursiv, ob ``dependency`` im Dependant-Baum der Route steckt."""

    if getattr(dependant, "call", None) is dependency:
        return True
    return any(_dependant_contains(sub, dependency) for sub in getattr(dependant, "dependencies", ()))


def unregistered_routes(app: FastAPI, contract: dict[str, RouteContract]) -> list[tuple[str, str]]:
    """Routen in der App, die nicht in der Vertragstabelle stehen."""

    violations: list[tuple[str, str]] = []
    for route in _api_routes(app):
        entry = contract.get(route.path)
        for method in sorted(route.methods):
            if entry is None or method not in entry.methods:
                violations.append((route.path, method))
    return violations


def stale_contract_entries(app: FastAPI, contract: dict[str, RouteContract]) -> list[str]:
    """Vertragseintraege, zu denen es keine Route mehr gibt."""

    present = {(route.path, method) for route in _api_routes(app) for method in route.methods}
    return [path for path, entry in contract.items() if not any((path, method) in present for method in entry.methods)]


def changing_routes_without_valid_kind(contract: dict[str, RouteContract]) -> list[str]:
    """Schreibende Eintraege ohne gueltige Art oder ohne Begruendung."""

    violations: list[str] = []
    for path, entry in contract.items():
        for method in sorted(entry.methods & CHANGING_METHODS):
            if entry.kind not in CHANGING_KINDS or not entry.reason.strip():
                violations.append(f"{method} {path}")
    return violations


def api_routes_without_require_user(app: FastAPI, contract: dict[str, RouteContract]) -> list[tuple[str, str]]:
    """``/api``-Routen, denen die Auth-Naht fehlt (N-SEC-04).

    Unbekannte Routen gelten als auth-pflichtig und werden mitgemeldet.
    """

    violations: list[tuple[str, str]] = []
    for route in _api_routes(app):
        entry = contract.get(route.path)
        requires_auth = True if entry is None else entry.auth
        if not requires_auth:
            continue
        if not _dependant_contains(route.dependant, require_user):
            violations.extend((route.path, method) for method in sorted(route.methods))
    return violations


def list_routes_without_limit(app: FastAPI, contract: dict[str, RouteContract]) -> list[str]:
    """Als Liste markierte GET-Routen ohne ``limit``-Parameter (SPEC F-MCP-11).

    Die ``limit``-Pflicht gilt nur fuer die lesende GET-Route; ein POST auf
    demselben Pfad (z. B. Anlage unter ``/api/accounts``) ist keine Listenroute.
    """

    violations: list[str] = []
    for route in _api_routes(app):
        entry = contract.get(route.path)
        if entry is None or not entry.is_list:
            continue
        if "GET" not in route.methods:
            continue
        if "limit" not in inspect.signature(route.endpoint).parameters:
            violations.append(route.path)
    return violations


def _real_app() -> FastAPI:
    """App ohne Lifespan-Start; die Router stehen schon nach ``create_app``."""

    return create_app(Settings(database_url="sqlite:///:memory:"))


# -- Pruefungen gegen die echte App ------------------------------------------------


def test_alle_api_routen_sind_registriert() -> None:
    """Jede /api-Route ist in der Vertragstabelle; unbekannte Routen sind rot."""
    app = _real_app()
    assert unregistered_routes(app, CONTRACT) == []


def test_keine_veralteten_vertragseintraege() -> None:
    """Zu jedem Vertragseintrag existiert eine Route."""
    app = _real_app()
    assert stale_contract_entries(app, CONTRACT) == []


def test_schreibende_routen_haben_art_und_begruendung() -> None:
    """Jede schreibende Route traegt Art (version|setzen|anlegen|aktion) und Begruendung (F-CON-02/03)."""
    assert changing_routes_without_valid_kind(CONTRACT) == []
    assert any(method in CHANGING_METHODS for entry in CONTRACT.values() for method in entry.methods), (
        "Es muss mindestens eine schreibende Route mit Vertragseintrag geben."
    )


def test_api_routen_haengen_an_require_user() -> None:
    """Jede /api-Route ausser /api/health* haengt an der Auth-Naht (N-SEC-04)."""
    app = _real_app()
    assert api_routes_without_require_user(app, CONTRACT) == []


def test_listen_routen_haben_limit() -> None:
    """Jede markierte Listenroute hat den Parameter limit (SPEC F-MCP-11)."""
    app = _real_app()
    assert list_routes_without_limit(app, CONTRACT) == []


def test_health_routen_sind_ohne_auth() -> None:
    """Health-Routen sind als auth-frei eingetragen und liegen ausserhalb der Naht."""
    assert CONTRACT["/api/health"].auth is False
    assert CONTRACT["/api/health/ready"].auth is False


# -- Beleg der Prueffunktionen an synthetischen Apps -------------------------------


def test_checker_meldet_unregistrierte_route() -> None:
    """Eine Route ohne Vertragseintrag wird als Verstoss erkannt."""
    api = APIRouter(prefix="/api")

    @api.post("/konten")
    def create_konto() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(api)

    assert ("/api/konten", "POST") in unregistered_routes(app, {})


def test_checker_meldet_fehlende_art() -> None:
    """Ein schreibender Eintrag ohne Art/Begruendung wird erkannt."""
    contract = {"/api/konten": RouteContract(frozenset({"POST"}), kind=None, reason="")}
    assert changing_routes_without_valid_kind(contract) == ["POST /api/konten"]


def test_checker_meldet_fehlende_auth_naht() -> None:
    """Eine /api-Route ohne require_user wird als Verstoss erkannt (N-SEC-04)."""
    api = APIRouter(prefix="/api")

    @api.get("/konten")
    def list_konten(limit: int = 50) -> list[int]:
        return []

    app = FastAPI()
    app.include_router(api)
    contract = {"/api/konten": RouteContract(frozenset({"GET"}), kind="lesen", reason="Liste", is_list=True)}

    assert api_routes_without_require_user(app, contract) == [("/api/konten", "GET")]


def test_checker_meldet_fehlendes_limit() -> None:
    """Eine markierte Listenroute ohne limit wird erkannt (SPEC F-MCP-11)."""
    api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

    @api.get("/konten")
    def list_konten() -> list[int]:
        return []

    app = FastAPI()
    app.include_router(api)
    contract = {"/api/konten": RouteContract(frozenset({"GET"}), kind="lesen", reason="Liste", is_list=True)}

    assert list_routes_without_limit(app, contract) == ["/api/konten"]


def test_checker_liste_prueft_nur_get_route() -> None:
    """Ein POST auf einem Listenpfad braucht kein ``limit`` (SPEC F-MCP-11)."""
    api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

    @api.get("/konten")
    def list_konten(limit: int = 50) -> list[int]:
        return []

    @api.post("/konten")
    def create_konto() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(api)
    contract = {"/api/konten": RouteContract(frozenset({"GET", "POST"}), kind="anlegen", reason="Anlage", is_list=True)}

    assert list_routes_without_limit(app, contract) == []
    assert unregistered_routes(app, contract) == []


def test_checker_akzeptiert_korrekte_route() -> None:
    """Eine vollstaendig vertragskonforme Route loest keinen Verstoss aus."""
    api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

    @api.get("/konten")
    def list_konten(limit: int = 50) -> list[int]:
        return []

    app = FastAPI()
    app.include_router(api)
    contract = {"/api/konten": RouteContract(frozenset({"GET"}), kind="lesen", reason="Liste", is_list=True)}

    assert unregistered_routes(app, contract) == []
    assert api_routes_without_require_user(app, contract) == []
    assert list_routes_without_limit(app, contract) == []


def test_checker_erkennt_veralteten_eintrag() -> None:
    """Ein Vertragseintrag ohne passende Route wird als veraltet erkannt."""
    app = FastAPI()
    contract = {"/api/konten": RouteContract(frozenset({"GET"}), kind="lesen", reason="Liste")}
    assert stale_contract_entries(app, contract) == ["/api/konten"]
