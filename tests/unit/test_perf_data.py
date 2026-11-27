"""Fokussierter Generator-Test: fester Seed und 0-basierter ``occurrence_index``.

Belegt zwei Zusicherungen des T-103-Datengenerators ``scripts/perf_data.py``
**ohne** den 100.000er-Bestand zu erzeugen (nur die reine Zeilenfunktion
``_iter_rows`` mit kleiner Anzahl, dadurch keine Auswirkung auf die
Performancebudgets N-PERF-01/F-BUC-06):

* **Reproduzierbarkeit** bei festem Seed (gleicher Seed -> identische Zeilen,
  anderer Seed -> abweichende Zeilen).
* **F-DUP-02**: Der ``occurrence_index`` ist ein Multiset-Zaehler je
  (Konto, Fingerprint) und startet wie die Produktionslogik
  (``services/duplicate.py``, ``demo.py``) **bei 0**, lueckenlos aufsteigend.

Es werden keine echten Daten verwendet (D8).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import perf_data  # noqa: E402  (Import erst nach sys.path-Anpassung)

ACCOUNT_IDS = (1, 2, 3, 4, 5)


def _rows(*, seed: int, count: int) -> list[dict[str, Any]]:
    """Erzeugt ``count`` Zeilen mit festem Bezugstag (klein, ohne DB)."""

    return list(perf_data._iter_rows(ACCOUNT_IDS, seed=seed, count=count, reference_day=perf_data.REFERENCE_DAY))


def _occurrence_by_key(rows: list[dict[str, Any]]) -> dict[tuple[int, str], list[int]]:
    """Gruppiert die Vorkommens-Indizes je (Konto, Fingerprint) in Reihenfolge."""

    grouped: dict[tuple[int, str], list[int]] = {}
    for row in rows:
        grouped.setdefault((row["account_id"], row["fingerprint"]), []).append(row["occurrence_index"])
    return grouped


def test_iter_rows_reproduzierbar_bei_festem_seed() -> None:
    """Gleicher Seed -> identische Zeilen, anderer Seed -> abweichend (T-103-DoD)."""

    first = _rows(seed=perf_data.DEFAULT_SEED, count=200)
    second = _rows(seed=perf_data.DEFAULT_SEED, count=200)
    other = _rows(seed=perf_data.DEFAULT_SEED + 1, count=200)

    assert first == second
    assert first != other
    assert len(first) == 200


def test_occurrence_index_startet_bei_null_f_dup_02() -> None:
    """Jeder (Konto, Fingerprint) beginnt bei Index 0 und zaehlt lueckenlos (F-DUP-02)."""

    grouped = _occurrence_by_key(_rows(seed=perf_data.DEFAULT_SEED, count=500))

    assert grouped, "Der Generator muss Zeilen liefern"
    for indices in grouped.values():
        assert indices == list(range(len(indices))), f"0-basiert und lueckenlos erwartet, war {indices}"


def test_occurrence_index_multiset_bei_wiederholtem_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Erzwungene Fingerprint-Gleichheit je Konto prueft den Multiset-Zaehler ab 0 (F-DUP-02).

    Ohne echte Fingerprint-Kollisionen blieben die Indizes fast immer 0; ein
    konstanter Fingerprint je Konto erzwingt je Konto die Folge 0, 1, 2, ... und
    deckt damit einen Startwert 1 (Verifierbefund) zuverlaessig auf.
    """

    monkeypatch.setattr(perf_data, "compute_fingerprint", lambda *args, **kwargs: "gleicher-fingerprint")

    grouped = _occurrence_by_key(_rows(seed=1, count=30))

    assert set(grouped) == {(account_id, "gleicher-fingerprint") for account_id in ACCOUNT_IDS}
    for indices in grouped.values():
        assert indices == list(range(len(indices)))
        assert indices[0] == 0
