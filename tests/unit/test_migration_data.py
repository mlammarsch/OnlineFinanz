"""Migrationstest mit Beispieldaten ueber den Vorgaenger-Stand (T-032).

Referenziert N-DB-03 (Schema-Versionierung ueber Alembic-Migrationen; Daten
bleiben bei einem Upgrade erhalten) und N-DB-01 (kein rohes SQL: alle
Datenzugriffe laufen ueber SQLAlchemy Core mit reflektierten Tabellen).

Der Test zieht eine frische Datei-basierte SQLite-Datenbank (kein ``:memory:``)
explizit auf die Revision ``"0001"`` hoch - nicht auf ``"head"``, damit
"Vorgaenger-Stand" auch nach kuenftigen Migrationen diese Bedeutung behaelt -
fuegt Beispieldaten ueber die aus der DB reflektierte Tabelle ein und hebt
anschliessend auf ``head`` an. Danach muessen die Daten unveraendert vorhanden
sein. Das pruefende Muster liegt wiederverwendbar in ``helpers_migration`` und
ist parametrisiert nach ``from_revision``, nicht auf ``0001`` hartkodiert.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Final

from helpers_migration import upgrade_preserves_data

# Ausgangsrevision des Tests (Vorgaenger-Stand), bewusst nicht "head".
BASE_REVISION: Final[str] = "0001"

# Gewaehlte Tabelle: "account_group" hat keine Fremdschluessel und wird von
# keiner spaeteren Migration (0002, 0003) im Schema veraendert. Damit bleibt der
# Vergleich vor/nach dem Upgrade auf head aussagekraeftig. Die zuvor genutzte
# Tabelle "bank" erhaelt in 0003 eine Versionsspalte (F-CON-02) und schiede
# dadurch als Beispiel fuer "Daten ueberleben unveraendert" aus.
SAMPLE_TABLE: Final[str] = "account_group"

# Beispieldaten ohne echten Bankbezug (N-SEC: keine echten Kontodaten).
# Die Zeitstempel sind bewusst naiv: Die reflektierte Tabelle liefert auf SQLite
# einen einfachen DateTime-Typ ohne Zone zurueck, sodass der direkte Vergleich
# der gelesenen Werte mit diesen Beispielwerten gelingt.
SAMPLE_ROWS: Final[list[dict[str, Any]]] = [
    {
        "name": "Gruppe Eins",
        "sort_order": 1,
        "version": 1,
        "created_at": datetime(2026, 1, 2, 3, 4, 5),
        "updated_at": datetime(2026, 1, 2, 3, 4, 5),
    },
    {
        "name": "Gruppe Zwei",
        "sort_order": 2,
        "version": 1,
        "created_at": datetime(2026, 1, 2, 3, 4, 5),
        "updated_at": datetime(2026, 1, 2, 3, 4, 5),
    },
]


def test_data_survives_upgrade_from_0001_to_head(tmp_path: Path) -> None:
    """Daten aus Revision 0001 bleiben beim Upgrade auf head erhalten (N-DB-03).

    Nutzt eine echte temporaere Datei-SQLite-DB. ``upgrade_preserves_data``
    vergleicht bereits den Zustand vor und nach dem head-Upgrade; hier werden
    zusaetzlich die konkreten Werte der Beispieldaten geprueft.
    """
    after = upgrade_preserves_data(
        tmp_path / "migration_data.db",
        from_revision=BASE_REVISION,
        table_name=SAMPLE_TABLE,
        rows=SAMPLE_ROWS,
    )

    assert len(after) == len(SAMPLE_ROWS), f"Erwartet {len(SAMPLE_ROWS)} Zeilen, gefunden {len(after)}"

    # Nach name vergleichen, unabhaengig von der Einfuegereihenfolge.
    expected_by_name = {row["name"]: row for row in SAMPLE_ROWS}
    actual_by_name = {row["name"]: row for row in after}
    assert set(actual_by_name) == set(expected_by_name)

    for name, expected_row in expected_by_name.items():
        actual_row = actual_by_name[name]
        for column, value in expected_row.items():
            assert actual_row[column] == value, f"Spalte {column} weicht ab fuer name={name}"
