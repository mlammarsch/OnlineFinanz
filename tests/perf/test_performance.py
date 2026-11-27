"""Performance-Block mit 100.000 Buchungen (N-PERF-01, F-BUC-06).

Referenziert:

* **N-PERF-01** Suche im Standardzeitraum (365 Tage) unter 500 ms bei 100.000
  Buchungen - mit einem Begriff, mit zwei Begriffen und als Betragssuche ueber
  alle Konten. Budgetkriterium ist der Median aus 5 Laeufen nach Aufwaermen; das
  Maximum wird zusaetzlich protokolliert.
* **F-BUC-06** Fluessige Buchungsliste bei 100.000 Buchungen: erste Seite und
  Seite 50 der Kontoliste sowie die Gruppenansicht unter 500 ms.
* **F-SUC-06** Die Suche laeuft portabel ueber ``LIKE`` auf der normalisierten
  Suchspalte; die Messung belegt, dass die Indizes aus Migration 0005 die
  Trefferzahl-/Treffersummen-Abfrage abdecken.

Zusaetzlich wird die Suche und die erste Listenseite **einmal ueber HTTP**
(``TestClient``) gemessen. Der Datenbestand kommt aus ``scripts/perf_data.py``
(5 Konten, 3 Jahre, fester Seed); es werden keine echten Daten verwendet (D8).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import perf_data
from finue.config import Settings
from finue.main import create_app
from finue.services.search import (
    SearchPeriod,
    SearchScope,
    SortDirection,
    SortField,
    TransactionQuery,
    build_query,
    run_search,
)
from finue.services.transaction_list import ListCursor, build_transaction_list

pytestmark = pytest.mark.perf

#: Seitengroesse der Buchungsliste (Standard aus ``Settings.api_default_page_size``).
PAGE_SIZE = 50

#: Seitennummer der zweiten gemessenen Buchungsliste-Seite (F-BUC-06).
FIFTH_PAGE_INDEX = 50

#: Feste Uhr fuer die HTTP-Messung: gleicher Bezugstag wie der Service-Test.
FIXED_CLOCK = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)


def _record(measurement: perf_data.Measurement) -> None:
    """Protokolliert eine Messung und prueft das Zeitbudget (Median)."""

    print("PERF", perf_data.format_measurement(measurement), flush=True)
    perf_data.append_report(measurement)
    assert measurement.within_budget(perf_data.BUDGET_MS), (
        f"{measurement.name}: Median {measurement.median_ms:.1f} ms "
        f">= Budget {perf_data.BUDGET_MS:.0f} ms (Maximum {measurement.max_ms:.1f} ms)"
    )


def _search_query(reference_day, search: str) -> TransactionQuery:
    """Zeitraeume/Sortierung der Standardsuche ueber alle Konten (F-SUC-04/05)."""

    return build_query(
        today=reference_day,
        scope=SearchScope.ALL,
        search=search,
        period=SearchPeriod.DAYS_365,
        sort=SortField.DATE,
        direction=SortDirection.DESC,
    )


def _account_query(reference_day, account_id: int) -> TransactionQuery:
    """Buchungsliste eines Kontos im Standardzeitraum (F-BUC-01..06)."""

    return build_query(
        today=reference_day,
        scope=SearchScope.ACCOUNT,
        account_id=account_id,
        period=SearchPeriod.DAYS_365,
    )


def _group_query(reference_day, group_id: int) -> TransactionQuery:
    """Gruppenansicht im Standardzeitraum (F-BUC-05)."""

    return build_query(
        today=reference_day,
        scope=SearchScope.GROUP,
        group_id=group_id,
        period=SearchPeriod.DAYS_365,
    )


def _page_cursor(session: Session, query: TransactionQuery, page_index: int) -> ListCursor:
    """Erreicht per Keyset-Cursor die ``page_index``-te Seite (1-basiert)."""

    cursor: ListCursor | None = None
    for _ in range(page_index - 1):
        result = build_transaction_list(session, query=query, limit=PAGE_SIZE, cursor=cursor)
        cursor = result.next_cursor
        if cursor is None:
            pytest.skip(f"Buchungsliste hat weniger als {page_index} Seiten im Standardzeitraum")
    assert cursor is not None
    return cursor


# ------------------------------------------------------------------- Suche (N-PERF-01)


def test_suche_ein_begriff_unter_budget(perf_db: perf_data.PerfInfo, perf_session: Session) -> None:
    """Ein-Begriff-Suche im Standardzeitraum unter 500 ms (N-PERF-01)."""

    query = _search_query(perf_db.reference_day, perf_data.SEARCH_ONE_TERM)
    measurement = perf_data.measure_ms(
        "suche_1_begriff",
        lambda: run_search(perf_session, query, limit=PAGE_SIZE, offset=0),
    )
    _record(measurement)


def test_suche_zwei_begriffe_unter_budget(perf_db: perf_data.PerfInfo, perf_session: Session) -> None:
    """Zwei-Begriff-Suche (UND-verknuepft) im Standardzeitraum unter 500 ms (N-PERF-01)."""

    query = _search_query(perf_db.reference_day, perf_data.SEARCH_TWO_TERMS)
    measurement = perf_data.measure_ms(
        "suche_2_begriffe",
        lambda: run_search(perf_session, query, limit=PAGE_SIZE, offset=0),
    )
    _record(measurement)


def test_suche_betrag_unter_budget(perf_db: perf_data.PerfInfo, perf_session: Session) -> None:
    """Betragssuche ueber alle Konten im Standardzeitraum unter 500 ms (N-PERF-01)."""

    query = _search_query(perf_db.reference_day, perf_data.AMOUNT_SEARCH)
    measurement = perf_data.measure_ms(
        "suche_betrag",
        lambda: run_search(perf_session, query, limit=PAGE_SIZE, offset=0),
    )
    _record(measurement)


# ------------------------------------------------- Buchungsliste/Gruppe (F-BUC-06)


def test_liste_erste_seite_unter_budget(perf_db: perf_data.PerfInfo, perf_session: Session) -> None:
    """Erste Seite der Buchungsliste unter 500 ms (F-BUC-06)."""

    query = _account_query(perf_db.reference_day, perf_db.first_account_id)
    measurement = perf_data.measure_ms(
        "liste_seite_1",
        lambda: build_transaction_list(perf_session, query=query, limit=PAGE_SIZE),
    )
    _record(measurement)


def test_liste_seite_50_unter_budget(perf_db: perf_data.PerfInfo, perf_session: Session) -> None:
    """Seite 50 der Buchungsliste (Keyset) unter 500 ms (F-BUC-06)."""

    query = _account_query(perf_db.reference_day, perf_db.first_account_id)
    cursor = _page_cursor(perf_session, query, FIFTH_PAGE_INDEX)
    measurement = perf_data.measure_ms(
        "liste_seite_50",
        lambda: build_transaction_list(perf_session, query=query, limit=PAGE_SIZE, cursor=cursor),
    )
    _record(measurement)


def test_gruppenansicht_unter_budget(perf_db: perf_data.PerfInfo, perf_session: Session) -> None:
    """Gruppenansicht (alle Konten, Kontokennzeichnung je Zeile) unter 500 ms (F-BUC-06)."""

    query = _group_query(perf_db.reference_day, perf_db.group_id)
    measurement = perf_data.measure_ms(
        "gruppe_seite_1",
        lambda: build_transaction_list(perf_session, query=query, limit=PAGE_SIZE),
    )
    _record(measurement)


# --------------------------------------------------------------- HTTP (einmalig)


def test_http_suche_und_liste_unter_budget(
    perf_db: perf_data.PerfInfo,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Suche und erste Listenseite einmal ueber HTTP unter 500 ms (N-PERF-01, F-BUC-06)."""

    settings = Settings(database_url=perf_db.db_url, data_dir=tmp_path_factory.mktemp("http"))
    app = create_app(settings, clock=lambda: FIXED_CLOCK)
    with TestClient(app) as client:

        def http_search() -> None:
            response = client.get(
                "/api/search",
                params={"q": perf_data.SEARCH_ONE_TERM, "period": "365", "limit": PAGE_SIZE},
            )
            assert response.status_code == 200, response.text

        def http_list() -> None:
            response = client.get(
                f"/api/accounts/{perf_db.first_account_id}/transactions",
                params={"period": "365", "limit": PAGE_SIZE},
            )
            assert response.status_code == 200, response.text

        _record(perf_data.measure_ms("http_suche_1_begriff", http_search))
        _record(perf_data.measure_ms("http_liste_seite_1", http_list))
