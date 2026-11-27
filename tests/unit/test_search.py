"""Tests des gemeinsamen Such- und Filterbausteins (T-092).

Referenziert F-SUC-01 (Feld ueber Gegenpartei, Verwendungszweck, Notiz, IBAN,
Betrag; Begriffe UND-verknuepft, Gross-/Kleinschreibung egal), F-SUC-02
(Betragssuche mit/ohne Vorzeichen), F-SUC-03 (Bereich Konto/Gruppe/alle),
F-SUC-04 (Zeitraeume, Europe/Berlin ueber injizierbare Uhr), F-SUC-05
(Sortierung Datum/Betrag/Gegenpartei mit stabilem Tiebreaker), F-SUC-06
(portables SQL), F-SUC-07/F-ABS-04 (Filter abgestimmt/geprueft/mit Notiz),
F-ABS-05 (externe Referenz durchsuchbar) und F-DUP-04 (zusammengefuehrte
Buchungen ausgeschlossen).

Alle Buchungen und Konten sind erfundene Testdaten (D8); es gibt keine echten
Zugangsdaten und keine Bankzugriffe.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Final

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account, AccountGroup
from finue.models.transaction import Transaction
from finue.services.search import (
    AmountTerm,
    DateRange,
    SearchPeriod,
    SearchScope,
    SearchValidationError,
    SortDirection,
    SortField,
    TransactionQuery,
    build_query,
    build_select,
    local_today,
    parse_amount_term,
    resolve_period,
    run_search,
    split_search_terms,
)
from finue.services.search_text import build_search_text

IBAN: Final = "DE02 1203 0000 0000 2020 51"

_sequence = 0


def _next_fingerprint() -> str:
    """Liefert einen eindeutigen, 64-stelligen Fingerprint (F-DUP-01)."""

    global _sequence
    _sequence += 1
    return f"{_sequence:064x}"


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _add_account(session: Session, name: str = "Konto", **overrides: object) -> Account:
    """Legt ein Testkonto an (erfundene Daten, D8)."""

    fields: dict[str, object] = {"name": name, "currency": "EUR", "source": "datei"}
    fields.update(overrides)
    account = Account(**fields)
    session.add(account)
    session.commit()
    return account


def _add_group(session: Session, name: str = "Gruppe") -> AccountGroup:
    """Legt eine Testgruppe an (erfundene Daten, D8)."""

    group = AccountGroup(name=name)
    session.add(group)
    session.commit()
    return group


def _add_transaction(
    session: Session,
    account: Account,
    *,
    booking_date: date,
    amount_cents: int,
    counterparty: str | None = None,
    purpose: str | None = None,
    note: str | None = None,
    counterparty_iban: str | None = None,
    external_ref: str | None = None,
    status: str = "gebucht",
    checked: bool = False,
    reconciled: bool = False,
    merged: bool = False,
    currency: str = "EUR",
) -> Transaction:
    """Legt eine Testbuchung mit gefuellter Suchspalte an (F-SUC-01)."""

    transaction = Transaction(
        account_id=account.id,
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=currency,
        origin="csv",
        status=status,
        counterparty=counterparty,
        purpose=purpose,
        note=note,
        counterparty_iban=counterparty_iban,
        external_ref=external_ref,
        checked=checked,
        reconciled=reconciled,
        merged=merged,
        fingerprint=_next_fingerprint(),
        occurrence_index=1,
        search_text=build_search_text(
            counterparty=counterparty,
            purpose=purpose,
            note=note,
            counterparty_iban=counterparty_iban,
            external_ref=external_ref,
        ),
    )
    session.add(transaction)
    session.commit()
    return transaction


def _run(session: Session, query: TransactionQuery) -> list[Transaction]:
    """Fuehrt eine Abfrage aus und liefert die Treffer in Reihenfolge."""

    result = run_search(session, query, limit=200, offset=0)
    return list(result.items)


# --------------------------------------------------------------- F-SUC-02 Betrag


class TestAmountParsing:
    """F-SUC-02: Betragsbegriffe in deutscher und englischer Schreibweise."""

    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            ("48,83", AmountTerm(4883, False)),
            ("48.83", AmountTerm(4883, False)),
            ("-48,83", AmountTerm(-4883, True)),
            ("+48,83", AmountTerm(4883, True)),
            ("1.234,56", AmountTerm(123456, False)),
            ("1,234.56", AmountTerm(123456, False)),
        ],
    )
    def test_betraege_werden_erkannt(self, term: str, expected: AmountTerm) -> None:
        assert parse_amount_term(term) == expected

    @pytest.mark.parametrize("term", ["2020", "Gehalt", "48.", "abc", "-", ".", "DE02", ""])
    def test_keine_betraege(self, term: str) -> None:
        """Ohne Dezimalteil oder mit Buchstaben gilt ein Begriff als Text."""

        assert parse_amount_term(term) is None

    def test_aufteilung_in_text_und_betrag(self) -> None:
        """F-SUC-01/F-SUC-02: Text- und Betragsbegriffe sind kombinierbar."""

        text, amounts = split_search_terms("Gehalt 48,83 Miete")
        assert text == ["gehalt", "miete"]
        assert amounts == [AmountTerm(4883, False)]


class TestAmountSearch:
    """F-SUC-02: Betragssuche mit und ohne Vorzeichen."""

    def test_ohne_vorzeichen_trifft_den_absolutwert(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-4883)
            _add_transaction(session, account, booking_date=date(2026, 5, 2), amount_cents=4883)
            _add_transaction(session, account, booking_date=date(2026, 5, 3), amount_cents=1000)

            query = build_query(today=date(2026, 6, 1), search="48,83")
            hits = _run(session, query)

            assert len(hits) == 2
            assert sorted(hit.amount_cents for hit in hits) == [-4883, 4883]

    def test_mit_vorzeichen_trifft_exakt(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-4883)
            _add_transaction(session, account, booking_date=date(2026, 5, 2), amount_cents=4883)

            query = build_query(today=date(2026, 6, 1), search="-48,83")
            hits = _run(session, query)

            assert [hit.amount_cents for hit in hits] == [-4883]

    def test_betrag_und_text_kombiniert(self, factory: sessionmaker[Session]) -> None:
        """F-SUC-01/F-SUC-02: Ein Textbegriff schraenkt den Betragstreffer ein."""

        with factory() as session:
            account = _add_account(session)
            _add_transaction(
                session, account, booking_date=date(2026, 5, 1), amount_cents=-4883, counterparty="Miete Mai"
            )
            _add_transaction(session, account, booking_date=date(2026, 5, 2), amount_cents=-4883, counterparty="Strom")

            query = build_query(today=date(2026, 6, 1), search="miete 48,83")
            hits = _run(session, query)

            assert len(hits) == 1
            assert hits[0].counterparty == "Miete Mai"

    def test_tausenderpunkt(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=123456)

            hits = _run(session, build_query(today=date(2026, 6, 1), search="1.234,56"))
            assert len(hits) == 1
            assert hits[0].amount_cents == 123456


# --------------------------------------------------------------- F-SUC-01 Text


class TestTextSearch:
    """F-SUC-01: Suchfeld auf der normalisierten Spalte."""

    def test_gross_kleinschreibung_egal(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(
                session, account, booking_date=date(2026, 5, 1), amount_cents=-100, counterparty="MUSTERMANN"
            )

            assert len(_run(session, build_query(today=date(2026, 6, 1), search="mustermann"))) == 1

    def test_mehrere_begriffe_und_verknuepft(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(
                session,
                account,
                booking_date=date(2026, 5, 1),
                amount_cents=-100,
                counterparty="Max Mustermann",
                purpose="Gehalt Juni",
            )
            _add_transaction(
                session,
                account,
                booking_date=date(2026, 5, 2),
                amount_cents=-100,
                counterparty="Max Mustermann",
                purpose="Miete",
            )

            hits = _run(session, build_query(today=date(2026, 6, 1), search="muster gehalt"))

            assert len(hits) == 1
            assert hits[0].purpose == "Gehalt Juni"

    def test_wildcards_werden_maskiert(self, factory: sessionmaker[Session]) -> None:
        """F-SUC-01: ``%`` und ``_`` sind Literale, keine Platzhalter."""

        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100, counterparty="A%Z")
            _add_transaction(session, account, booking_date=date(2026, 5, 2), amount_cents=-100, counterparty="ABZ")

            # Ohne Maskierung wuerde "A%Z" auf beide Zeilen passen.
            percent_hits = _run(session, build_query(today=date(2026, 6, 1), search="a%z"))
            assert [hit.counterparty for hit in percent_hits] == ["A%Z"]

            _add_transaction(session, account, booking_date=date(2026, 5, 3), amount_cents=-100, counterparty="A_Z")
            underscore_hits = _run(session, build_query(today=date(2026, 6, 1), search="a_z"))
            assert [hit.counterparty for hit in underscore_hits] == ["A_Z"]

    def test_iban_und_notiz_durchsuchbar(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(
                session,
                account,
                booking_date=date(2026, 5, 1),
                amount_cents=-100,
                counterparty="Vermieter",
                counterparty_iban=IBAN,
                note="Kaution",
            )

            assert len(_run(session, build_query(today=date(2026, 6, 1), search="kaution"))) == 1
            assert len(_run(session, build_query(today=date(2026, 6, 1), search="DE02120300000000202051"))) == 1


class TestExternalRefSearch:
    """F-ABS-05: Die externe Referenz ist durchsuchbar."""

    def test_externe_referenz_durchsuchbar(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(
                session,
                account,
                booking_date=date(2026, 5, 1),
                amount_cents=-100,
                counterparty="Vermieter",
                external_ref="TX-42",
            )

            hits = _run(session, build_query(today=date(2026, 6, 1), search="tx-42"))
            assert len(hits) == 1


# --------------------------------------------------------------- F-SUC-03 Bereich


class TestScope:
    """F-SUC-03: Konto, Gruppe oder alle aktiven Konten."""

    def test_alle_aktiven_konten_archiv_ausgeschlossen(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            active = _add_account(session, "Aktiv")
            archived = _add_account(session, "Archiv", active=False)
            _add_transaction(session, active, booking_date=date(2026, 5, 1), amount_cents=-100, counterparty="Treffer")
            _add_transaction(
                session, archived, booking_date=date(2026, 5, 1), amount_cents=-100, counterparty="Treffer"
            )

            hits = _run(session, build_query(today=date(2026, 6, 1)))
            assert [hit.account_id for hit in hits] == [active.id]

    def test_bereich_konto(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            first = _add_account(session, "Erstes")
            second = _add_account(session, "Zweites")
            _add_transaction(session, first, booking_date=date(2026, 5, 1), amount_cents=-100)
            _add_transaction(session, second, booking_date=date(2026, 5, 1), amount_cents=-100)

            hits = _run(session, build_query(today=date(2026, 6, 1), scope=SearchScope.ACCOUNT, account_id=first.id))
            assert [hit.account_id for hit in hits] == [first.id]

    def test_bereich_gruppe(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            group = _add_group(session, "Haushalt")
            member = _add_account(session, "Mitglied", group_id=group.id)
            other = _add_account(session, "Anderes")
            _add_transaction(session, member, booking_date=date(2026, 5, 1), amount_cents=-100)
            _add_transaction(session, other, booking_date=date(2026, 5, 1), amount_cents=-100)

            hits = _run(session, build_query(today=date(2026, 6, 1), scope=SearchScope.GROUP, group_id=group.id))
            assert [hit.account_id for hit in hits] == [member.id]

    def test_bereich_konto_ohne_id_ist_fehler(self) -> None:
        """F-SUC-03: Ein Konto-/Gruppenbereich ohne ID wird abgelehnt."""

        with pytest.raises(SearchValidationError):
            build_query(today=date(2026, 6, 1), scope=SearchScope.ACCOUNT)
        with pytest.raises(SearchValidationError):
            build_query(today=date(2026, 6, 1), scope=SearchScope.GROUP)


# --------------------------------------------------------------- F-SUC-04 Zeitraum


class TestPeriodResolution:
    """F-SUC-04: Zeitraeume werden in absolute Grenzen aufgeloest."""

    TODAY: Final = date(2026, 6, 15)

    def test_30_90_365(self) -> None:
        assert resolve_period(SearchPeriod.DAYS_30, today=self.TODAY) == DateRange(date(2026, 5, 16), self.TODAY)
        assert resolve_period(SearchPeriod.DAYS_90, today=self.TODAY) == DateRange(date(2026, 3, 17), self.TODAY)
        assert resolve_period(SearchPeriod.DAYS_365, today=self.TODAY) == DateRange(date(2025, 6, 15), self.TODAY)

    def test_laufendes_und_vorjahr(self) -> None:
        assert resolve_period(SearchPeriod.CURRENT_YEAR, today=self.TODAY) == DateRange(
            date(2026, 1, 1), date(2026, 12, 31)
        )
        assert resolve_period(SearchPeriod.PREVIOUS_YEAR, today=self.TODAY) == DateRange(
            date(2025, 1, 1), date(2025, 12, 31)
        )

    def test_alle_und_von_bis(self) -> None:
        assert resolve_period(SearchPeriod.ALL, today=self.TODAY) is None
        assert resolve_period(
            SearchPeriod.CUSTOM, today=self.TODAY, from_date=date(2026, 2, 1), to_date=date(2026, 3, 1)
        ) == DateRange(date(2026, 2, 1), date(2026, 3, 1))

    def test_custom_ohne_grenzen_ist_fehler(self) -> None:
        with pytest.raises(SearchValidationError):
            resolve_period(SearchPeriod.CUSTOM, today=self.TODAY)
        with pytest.raises(SearchValidationError):
            resolve_period(SearchPeriod.CUSTOM, today=self.TODAY, from_date=date(2026, 3, 1), to_date=date(2026, 2, 1))

    def test_heute_kommt_aus_der_uhr_in_berlin(self) -> None:
        """F-SUC-04: Die Uhr wird auf Europe/Berlin umgerechnet."""

        # 2025-12-31 23:30 UTC ist in Berlin bereits der 01.01.2026.
        clock = lambda: datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc)  # noqa: E731
        assert local_today(clock) == date(2026, 1, 1)


class TestPeriodFiltering:
    """F-SUC-04: Der Zeitraumfilter grenzt Buchungen ein."""

    def test_standard_365_tage(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 6, 1), amount_cents=-100)
            _add_transaction(session, account, booking_date=date(2025, 1, 1), amount_cents=-100)

            hits = _run(session, build_query(today=date(2026, 6, 15)))
            assert [hit.booking_date for hit in hits] == [date(2026, 6, 1)]

    def test_alle_ohne_grenze(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2020, 1, 1), amount_cents=-100)
            hits = _run(session, build_query(today=date(2026, 6, 15), period=SearchPeriod.ALL))
            assert len(hits) == 1

    def test_vorjahr(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2025, 7, 1), amount_cents=-100)
            _add_transaction(session, account, booking_date=date(2026, 7, 1), amount_cents=-100)
            hits = _run(session, build_query(today=date(2026, 6, 15), period=SearchPeriod.PREVIOUS_YEAR))
            assert [hit.booking_date for hit in hits] == [date(2025, 7, 1)]

    def test_von_bis(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 2, 1), amount_cents=-100)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)
            hits = _run(
                session,
                build_query(
                    today=date(2026, 6, 15),
                    period=SearchPeriod.CUSTOM,
                    from_date=date(2026, 1, 1),
                    to_date=date(2026, 3, 1),
                ),
            )
            assert [hit.booking_date for hit in hits] == [date(2026, 2, 1)]


# --------------------------------------------------------------- F-SUC-05 Sortierung


class TestSorting:
    """F-SUC-05: Sortierung mit stabilem Tiebreaker ``id``."""

    def test_datum_auf_ab(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            first = _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)
            second = _add_transaction(session, account, booking_date=date(2026, 5, 2), amount_cents=-200)

            ascending = _run(
                session, build_query(today=date(2026, 6, 1), sort=SortField.DATE, direction=SortDirection.ASC)
            )
            descending = _run(
                session, build_query(today=date(2026, 6, 1), sort=SortField.DATE, direction=SortDirection.DESC)
            )
            assert [hit.id for hit in ascending] == [first.id, second.id]
            assert [hit.id for hit in descending] == [second.id, first.id]

    def test_betrag_auf_ab(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=300)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-500)

            ascending = _run(
                session, build_query(today=date(2026, 6, 1), sort=SortField.AMOUNT, direction=SortDirection.ASC)
            )
            assert [hit.amount_cents for hit in ascending] == [-500, 300]

    def test_gegenpartei_auf_ab(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=100, counterparty="Zebra")
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=100, counterparty="anna")
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=100, counterparty=None)

            ascending = _run(
                session, build_query(today=date(2026, 6, 1), sort=SortField.COUNTERPARTY, direction=SortDirection.ASC)
            )
            assert [hit.counterparty for hit in ascending] == [None, "anna", "Zebra"]

    def test_tiebreaker_id_bei_gleichem_datum(self, factory: sessionmaker[Session]) -> None:
        """F-SUC-05: Bei gleichem Sortierwert ist ``id`` aufsteigend stabil."""

        with factory() as session:
            account = _add_account(session)
            first = _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)
            second = _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)
            third = _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)

            hits = _run(session, build_query(today=date(2026, 6, 1), sort=SortField.DATE, direction=SortDirection.DESC))
            assert [hit.id for hit in hits] == [first.id, second.id, third.id]


# --------------------------------------------------------------- F-SUC-07 Filter


class TestFilters:
    """F-SUC-07/F-ABS-04: abgestimmt, geprueft, mit Notiz; F-DUP-04: merged raus."""

    def test_geprueft_ja_nein_alle(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            checked = _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100, checked=True)
            unchecked = _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)

            only_checked = _run(session, build_query(today=date(2026, 6, 1), checked=True))
            only_unchecked = _run(session, build_query(today=date(2026, 6, 1), checked=False))
            both = _run(session, build_query(today=date(2026, 6, 1)))

            assert [hit.id for hit in only_checked] == [checked.id]
            assert [hit.id for hit in only_unchecked] == [unchecked.id]
            assert len(both) == 2

    def test_abgestimmt_ja_nein(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            reconciled = _add_transaction(
                session, account, booking_date=date(2026, 5, 1), amount_cents=-100, reconciled=True
            )
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)

            assert [hit.id for hit in _run(session, build_query(today=date(2026, 6, 1), reconciled=True))] == [
                reconciled.id
            ]

    def test_mit_notiz(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            with_note = _add_transaction(
                session, account, booking_date=date(2026, 5, 1), amount_cents=-100, note="Wichtig"
            )
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)

            hits = _run(session, build_query(today=date(2026, 6, 1), with_note=True))
            assert [hit.id for hit in hits] == [with_note.id]

    def test_zusammengefuehrte_buchungen_ausgeschlossen(self, factory: sessionmaker[Session]) -> None:
        """F-DUP-04: ``merged``-Buchungen sind nie Teil einer Suche."""

        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-100, merged=True)

            hits = _run(session, build_query(today=date(2026, 6, 1)))
            assert len(hits) == 1


# --------------------------------------------------------------- F-SUC-06 Portabilitaet


class TestPortability:
    """F-SUC-06: Die Abfrage ist ohne dialektspezifisches Konstrukt kompilierbar."""

    @pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect(), mysql.dialect()])
    def test_abfrage_kompiliert(self, dialect: Dialect) -> None:
        query = build_query(
            today=date(2026, 6, 15),
            scope=SearchScope.GROUP,
            group_id=1,
            search="miete 48,83",
            period=SearchPeriod.DAYS_30,
            checked=True,
            reconciled=False,
            with_note=True,
            sort=SortField.COUNTERPARTY,
            direction=SortDirection.ASC,
        )
        rendered = str(build_select(query).compile(dialect=dialect))
        assert "search_text LIKE" in rendered


# --------------------------------------------------------------- Ergebniszahl/-summe


class TestResultSummary:
    """DoD: Trefferzahl und Trefferumme beziehen sich auf die Gesamtmenge."""

    def test_zaehlung_und_summe_ueber_alle_treffer(self, factory: sessionmaker[Session]) -> None:
        with factory() as session:
            account = _add_account(session)
            _add_transaction(session, account, booking_date=date(2026, 5, 1), amount_cents=-4883, counterparty="Miete")
            _add_transaction(session, account, booking_date=date(2026, 5, 2), amount_cents=1000, counterparty="Miete")
            _add_transaction(
                session, account, booking_date=date(2026, 5, 3), amount_cents=-1000, counterparty="Anderes"
            )

            result = run_search(session, build_query(today=date(2026, 6, 1), search="miete"), limit=1, offset=0)
            assert result.total_count == 2
            assert result.total_cents == -3883
            assert len(result.items) == 1
