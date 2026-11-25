"""Kommandozeile der Finanzuebersicht (``python -m finue``).

Implementiert N-OPS-01 (Demo-Daten per Befehl fuer die lokale Entwicklung) und
N-DB-03 (Schema-Migration per Befehl). Die CLI nutzt ausschliesslich die
Standardbibliothek ``argparse``, keine externe CLI-Bibliothek.

Unterbefehle:
    migrate  Hebt das Schema der konfigurierten Datenbank auf ``head`` an.
    demo     Migriert das Schema und legt die synthetischen Demo-Daten an;
             ``--reset`` entfernt vorhandene Demo-Daten vorher (idempotent).
    serve    Startet die Web-App mit genau einem Worker-Prozess (D3).
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from finue.config import get_settings
from finue.db.engine import create_db_engine, make_session_factory
from finue.db.migrate import upgrade_to_head
from finue.demo import clear_demo, seed_demo

# Sinnvolle Defaults fuer den lokalen Betrieb (Docker/VM ueberschreibt per Flag).
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

# D3: Der Server laeuft mit genau einem Worker-Prozess. Der Wert ist bewusst
# fest kodiert und NICHT ueber ein CLI-Flag aenderbar, weil pausierte
# FinTS-Dialoge nur im Arbeitsspeicher dieses Prozesses liegen (N-SEC-03).
UVICORN_WORKERS = 1

# Fester Seed fuer den deterministischen Demo-Bestand (N-OPS-01).
DEMO_SEED = 42


def build_parser() -> argparse.ArgumentParser:
    """Baut den Argument-Parser mit den Unterbefehlen migrate, demo und serve.

    ``serve`` bietet bewusst keine Option fuer die Worker-Anzahl: D3 verlangt
    genau einen Worker-Prozess (siehe ``UVICORN_WORKERS``).
    """
    parser = argparse.ArgumentParser(
        prog="python -m finue",
        description="Finanzuebersicht: Schema-Migration, Demo-Daten und Serverstart.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "migrate",
        help="Schema der konfigurierten Datenbank auf head anheben (N-DB-03).",
    )

    demo_parser = subparsers.add_parser(
        "demo",
        help="Schema migrieren und synthetische Demo-Daten anlegen (N-OPS-01).",
    )
    demo_parser.add_argument(
        "--reset",
        action="store_true",
        help="Vorhandene Demo-Daten vor dem Anlegen entfernen (wiederholbar).",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Web-App mit genau einem Worker-Prozess starten (D3).",
    )
    serve_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Bind-Adresse des Servers (Standard: {DEFAULT_HOST}).",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP-Port des Servers (Standard: {DEFAULT_PORT}).",
    )

    return parser


def _run_migrate() -> int:
    """Hebt das Schema der Projekt-Datenbank auf ``head`` an (N-DB-03)."""
    settings = get_settings()
    upgrade_to_head(settings.database_url)
    return 0


def _run_demo(reset: bool) -> int:
    """Migriert die Datenbank und legt die Demo-Daten an (N-OPS-01).

    Mit ``reset`` werden vorhandene Demo-Daten vorher entfernt, damit der
    Befehl beliebig oft ohne Eindeutigkeitsfehler laeuft.
    """
    settings = get_settings()
    upgrade_to_head(settings.database_url)

    engine = create_db_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            if reset:
                clear_demo(session)
            seed_demo(session, seed=DEMO_SEED)
    finally:
        engine.dispose()
    return 0


def _run_serve(host: str, port: int) -> int:
    """Startet ``finue.main:app`` mit genau einem Worker-Prozess (D3)."""
    uvicorn.run(
        "finue.main:app",
        host=host,
        port=port,
        workers=UVICORN_WORKERS,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Fuehrt die CLI aus und liefert den Exit-Code.

    Args:
        argv: Argumente ohne Programmnamen; ``None`` nutzt ``sys.argv``.
            Tests uebergeben eine eigene Liste, statt ``sys.argv`` zu patchen.

    Returns:
        Exit-Code (0 bei Erfolg).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "migrate":
        return _run_migrate()
    if args.command == "demo":
        return _run_demo(reset=bool(args.reset))
    return _run_serve(host=str(args.host), port=int(args.port))


if __name__ == "__main__":
    sys.exit(main())
