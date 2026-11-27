"""Zentrale Router-Registrierung unter ``/api`` (F-CON-02, N-SEC-04).

Jede fachliche Route wird hier eingetragen. Folge-Tasks ergaenzen je eine Zeile
``protected.include_router(<modul>.router)``; das Modul bringt sein ``/api``-Praefix
selbst mit und traegt die Auth-Naht ``require_user`` als Router-Dependency. Die
Health-Routen liegen bewusst ausserhalb, damit Liveness und Readiness ohne
Anmeldung erreichbar bleiben (N-OPS-04).

Der geschuetzte Router traegt selbst kein Praefix: die Fachrouter bringen
jeweils ``prefix="/api"`` mit. So stehen die vollstaendigen Pfade bereits in
``route.path``, und die Vertragspruefung ``tests/unit/test_api_contract.py``
findet jede Route unter ihrem echten Pfad.

Die Reihenfolge der Includes bestimmt die Reihenfolge im OpenAPI-Dokument, nicht
die Erreichbarkeit. Jede fachliche Route gehoert in den ``protected``-Router,
damit die Vertragspruefung sie erfasst.
"""

from __future__ import annotations

from fastapi import APIRouter

from finue.api import accounts, banks, groups, icons, reconciliation, search, transaction_list
from finue.api.health import router as health_router

__all__ = ["build_protected_router", "build_router"]


def build_protected_router() -> APIRouter:
    """Erzeugt den geschuetzten Router.

    Jeder eingebundene Fachrouter bringt sein ``prefix="/api"`` und die Auth-Naht
    ``require_user`` selbst mit (N-SEC-04). Folge-Tasks erweitern genau diesen
    Router um eine Zeile je Fachmodul.
    """

    protected = APIRouter()
    # Fachliche Router; jeder weitere Task ergaenzt hier eine Zeile (T-088: Konten/Gruppen).
    # Die Auth-Naht traegt jeder Fachrouter selbst als Router-Dependency (N-SEC-04),
    # damit sie auch in verschachtelten Routen (FastAPI 0.16x) an jeder Route haengt.
    protected.include_router(accounts.router)
    protected.include_router(groups.router)
    # Abstimmungsuebersicht je Konto und Gruppe (T-095, F-ABS-03).
    protected.include_router(reconciliation.router)
    # Banken-Stammdaten und Icon-Upload/-Auslieferung (T-090, F-KTO-10/11).
    protected.include_router(banks.router)
    protected.include_router(icons.router)
    # Buchungssuche mit Filtern (T-092, F-SUC-01..07, F-ABS-04/05).
    protected.include_router(search.router)
    # Buchungsliste je Konto und Gruppe mit Vormerkungen und Tagessalden (T-093, F-BUC-01..06).
    protected.include_router(transaction_list.router)
    return protected


def build_router() -> APIRouter:
    """Baut den Gesamt-Router: Health ohne Anmeldung, Fachrouten mit Naht."""

    root = APIRouter()
    # Health-Routen bleiben ohne Anmeldung erreichbar (N-OPS-04).
    root.include_router(health_router)
    # Fachliche Routen unter /api; Folge-Tasks ergaenzen hier je eine Zeile.
    root.include_router(build_protected_router())
    return root
