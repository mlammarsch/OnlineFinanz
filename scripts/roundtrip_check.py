#!/usr/bin/env python3
"""Import-Roundtrip der JSON-v1-Fixture (F-EXP-04, F-EXP-06, F-IMP-04).

Ausgelagert aus ``scripts/validate.py`` (T-084): laeuft im Projekt-Interpreter
(``uv run python scripts/roundtrip_check.py``), weil ``validate.py`` selbst nur
die Standardbibliothek nutzen darf. Die Fixture wird in einer temporaeren,
isolierten SQLite-Datei zweimal ueber ``execute_import`` importiert; der zweite
Lauf darf nichts Neues anlegen. Echte Kontodaten werden nicht verwendet (D8).

Ausgabe bei Erfolg: ``ROUNDTRIP_OK erster_lauf=<n> zweiter_lauf=<n> bestand=<n>``.
Exit 0 nur bei vollstaendigem Bestand, sonst Exit 1 mit ``ROUNDTRIP_FEHLER:``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(ROOT / "backend"))

    import json
    import tempfile

    from sqlalchemy import func, select

    import finue.models  # noqa: F401  # registriert alle Tabellen an Base.metadata
    from finue.db.base import Base
    from finue.db.engine import create_db_engine, make_session_factory
    from finue.models.account import Account
    from finue.models.transaction import Transaction
    from finue.services.import_run import execute_import

    fixture = ROOT / "tests" / "fixtures" / "export_v1_sample.json"
    if not fixture.is_file():
        print(f"ROUNDTRIP_FEHLER: Fixture fehlt: {fixture}")
        return 2

    document = json.loads(fixture.read_text(encoding="utf-8"))
    expected = len(document.get("transactions", []))
    if expected < 1:
        print("ROUNDTRIP_FEHLER: Fixture enthaelt keine Buchungen")
        return 2

    with tempfile.TemporaryDirectory(prefix="finue-roundtrip-") as tmp:
        engine = create_db_engine("sqlite:///" + (Path(tmp) / "roundtrip.db").as_posix())
        try:
            Base.metadata.create_all(engine)
            session_factory = make_session_factory(engine)
            with session_factory() as session:
                account = Account(id=1, name="Roundtrip-Pruefung", currency="EUR", source="datei")
                session.add(account)
                session.commit()

                first = execute_import(account.id, document, None, "json", session)
                total_first = session.scalar(select(func.count()).select_from(Transaction)) or 0
                second = execute_import(account.id, document, None, "json", session)
                total_second = session.scalar(select(func.count()).select_from(Transaction)) or 0
        finally:
            engine.dispose()

    fehler = []
    if first.status != "erfolgreich":
        fehler.append(f"erster Lauf Status {first.status!r}")
    if first.new_count != expected:
        fehler.append(f"erster Lauf new_count={first.new_count}, erwartet {expected}")
    if first.failed_count != 0:
        fehler.append(f"erster Lauf failed_count={first.failed_count}")
    if total_first != expected:
        fehler.append(f"Bestand nach erstem Lauf {total_first}, erwartet {expected}")
    if second.status != "erfolgreich":
        fehler.append(f"zweiter Lauf Status {second.status!r}")
    if second.new_count != 0:
        fehler.append(f"zweiter Lauf new_count={second.new_count}, erwartet 0")
    if total_second != total_first:
        fehler.append(f"Bestand nach zweitem Lauf {total_second}, erwartet {total_first}")

    if fehler:
        print("ROUNDTRIP_FEHLER: " + "; ".join(fehler))
        return 1

    print(f"ROUNDTRIP_OK erster_lauf={first.new_count} zweiter_lauf={second.new_count} bestand={total_second}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
