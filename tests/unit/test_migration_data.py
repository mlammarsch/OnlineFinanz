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

from pathlib import Path
from typing import Any, Final

from helpers_migration import upgrade_preserves_data

# Ausgangsrevision des Tests (Vorgaenger-Stand), bewusst nicht "head".
BASE_REVISION: Final[str] = "0001"

# Gewaehlte Tabelle: "bank" hat nur zwei Pflichtfelder (blz, name) und keine
# Fremdschluessel. Sie laesst sich daher ohne vorherige Befuellung abhaengiger
# Tabellen ueber eine reflektierte Table befuellen und eignet sich ideal, um das
# Muster "Daten ueberleben ein Upgrade" zu zeigen.
SAMPLE_TABLE: Final[str] = "bank"

# Beispieldaten ohne echten Bankbezug (N-SEC: keine echten Kontodaten).
SAMPLE_ROWS: Final[list[dict[str, Any]]] = [
    {"blz": "10010010", "name": "Testbank Eins", "website": "https://bank-eins.example", "fints_url": None},
    {"blz": "20020020", "name": "Testbank Zwei", "website": None, "fints_url": None},
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

    assert len(after) == len(SAMPLE_ROWS), f"Erwartet {len(SAMPLE_ROWS)} Bank-Zeilen, gefunden {len(after)}"

    # Nach blz sortiert vergleichen, unabhaengig von der Einfuegereihenfolge.
    expected_by_blz = {row["blz"]: row for row in SAMPLE_ROWS}
    actual_by_blz = {row["blz"]: row for row in after}
    assert set(actual_by_blz) == set(expected_by_blz)

    for blz, expected_row in expected_by_blz.items():
        actual_row = actual_by_blz[blz]
        for column, value in expected_row.items():
            assert actual_row[column] == value, f"Spalte {column} weicht ab fuer blz={blz}"
