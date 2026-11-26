"""Zentrale Router-Registrierung unter ``/api`` (F-CON-02, N-SEC-04).

Jede fachliche Route wird hier eingetragen. Folge-Tasks ergaenzen je eine Zeile
``protected.include_router(<modul>.router)``. Der geschuetzte Router traegt die
Auth-Naht ``require_user`` als Router-Dependency; die Health-Routen liegen
bewusst ausserhalb, damit Liveness und Readiness ohne Anmeldung erreichbar
bleiben (N-OPS-04).

Die Reihenfolge der Includes bestimmt die Reihenfolge im OpenAPI-Dokument, nicht
die Erreichbarkeit. Jede fachliche Route gehoert in den ``protected``-Router,
damit die Vertragspruefung ``tests/unit/test_api_contract.py`` sie erfasst.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from finue.api.deps import require_user
from finue.api.health import router as health_router

__all__ = ["build_protected_router", "build_router"]


def build_protected_router() -> APIRouter:
    """Erzeugt den geschuetzten ``/api``-Router mit der Auth-Naht.

    Der Router traegt ``require_user`` als Router-Dependency, damit jede hier
    eingetragene Route ohne Einzelaufruf an der Auth-Naht haengt (N-SEC-04).
    Folge-Tasks erweitern genau diesen Router.
    """

    protected = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
    return protected


def build_router() -> APIRouter:
    """Baut den Gesamt-Router: Health ohne Anmeldung, Fachrouten mit Naht."""

    root = APIRouter()
    # Health-Routen bleiben ohne Anmeldung erreichbar (N-OPS-04).
    root.include_router(health_router)
    # Fachliche Routen unter /api; Folge-Tasks ergaenzen hier je eine Zeile.
    root.include_router(build_protected_router())
    return root
