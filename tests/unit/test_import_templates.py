"""Tests fuer den Service der Import-Vorlagen (F-IMP-05).

Belegt Speichern/Wiederfinden, die gemeinsame Liste aus kontospezifischen und
kontounabhaengigen Vorlagen, die Passgenauigkeits-Reihenfolge beim Vorschlagen,
das idempotente Seeding genau fuer N26, Easybank und PayPal (ohne eigenen
CSV-Export) sowie die Verwendbarkeit der Seed-Mappings in der Mapping-Engine
(F-IMP-02, F-IMP-03). Die PayPal-Vorlage bindet den Vorverarbeitungsschritt aus
F-IMP-08 ueber ``template_pre_filter``. Alle Zugriffe laufen ueber das ORM
(N-DB-01).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.bank import Bank, BankAccess  # noqa: F401
from finue.services.import_templates import (
    DEFAULT_TEMPLATE_NAMES,
    EASYBANK_TEMPLATE_NAME,
    N26_TEMPLATE_NAME,
    PAYPAL_PRE_FILTER,
    PAYPAL_TEMPLATE_NAME,
    create_template,
    delete_template,
    get_template,
    list_templates,
    seed_default_templates,
    suggest_templates,
    template_pre_filter,
    update_template,
)
from finue.services.mapping import apply_mapping

_MINIMAL_MAPPING = {
    "Datum": "booking_date",
    "Betrag": "amount_cents",
}


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite fuer die Vorlagen-Tests."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _add_account(session: Session, name: str = "Testkonto") -> Account:
    """Legt ein Datei-Konto an und liefert es mit gesetzter ID."""
    account = Account(name=name, currency="EUR", source="datei")
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def test_create_template_and_find_again(factory: sessionmaker[Session]) -> None:
    """F-IMP-05: Vorlage anlegen und in einer frischen Session wiederfinden."""
    with factory() as session:
        account = _add_account(session)
        created = create_template(
            session,
            name="Mein N26-Export",
            kind="csv",
            mapping=_MINIMAL_MAPPING,
            account_id=account.id,
            delimiter=";",
            encoding="utf-8",
            date_format="TT.MM.JJJJ",
            decimal_separator="comma",
        )
        template_id = created.id

    with factory() as session:
        found = get_template(session, template_id)
        assert found is not None
        assert found.name == "Mein N26-Export"
        assert found.kind == "csv"
        assert found.account_id == account.id
        assert found.mapping == _MINIMAL_MAPPING
        assert found.delimiter == ";"
        assert found.encoding == "utf-8"
        assert found.date_format == "TT.MM.JJJJ"
        assert found.decimal_separator == "comma"


def test_list_templates_combines_account_specific_and_global(factory: sessionmaker[Session]) -> None:
    """F-IMP-05: Fuer ein Konto erscheinen eigene und globale Vorlagen zusammen."""
    with factory() as session:
        account = _add_account(session)
        other = _add_account(session, name="Anderes Konto")
        global_template = create_template(
            session,
            name="Global CSV",
            kind="csv",
            mapping=_MINIMAL_MAPPING,
        )
        own_template = create_template(
            session,
            name="Konto CSV",
            kind="csv",
            mapping=_MINIMAL_MAPPING,
            account_id=account.id,
        )
        create_template(
            session,
            name="Fremd CSV",
            kind="csv",
            mapping=_MINIMAL_MAPPING,
            account_id=other.id,
        )
        account_id = account.id
        global_id = global_template.id
        own_id = own_template.id

    with factory() as session:
        listed = list_templates(session, account_id)
        listed_ids = {template.id for template in listed}

        assert listed_ids == {global_id, own_id}
        assert "Fremd CSV" not in {template.name for template in listed}

    with factory() as session:
        all_templates = list_templates(session)
        assert {template.name for template in all_templates} == {
            "Global CSV",
            "Konto CSV",
            "Fremd CSV",
        }


def test_suggest_prefers_account_specific_over_global_with_same_name(factory: sessionmaker[Session]) -> None:
    """F-IMP-05: Bei gleichem Namen gewinnt die kontospezifische Vorlage."""
    with factory() as session:
        account = _add_account(session)
        other = _add_account(session, name="Anderes Konto")

        global_template = create_template(
            session,
            name=N26_TEMPLATE_NAME,
            kind="csv",
            mapping={**_MINIMAL_MAPPING, "fields": {"booking_date": "Datum"}},
        )
        specific_template = create_template(
            session,
            name=N26_TEMPLATE_NAME,
            kind="csv",
            mapping={**_MINIMAL_MAPPING, "fields": {"booking_date": "Buchungstag"}},
            account_id=account.id,
        )
        create_template(
            session,
            name="Fremde N26-Vorlage",
            kind="csv",
            mapping=_MINIMAL_MAPPING,
            account_id=other.id,
        )
        account_id = account.id
        global_id = global_template.id
        specific_id = specific_template.id

    with factory() as session:
        suggested = suggest_templates(session, account_id)

        assert [template.id for template in suggested] == [specific_id, global_id]
        assert suggested[0].account_id == account_id
        assert suggested[1].account_id is None
        # Gleicher Anzeigename: die Zuordnung entscheidet, nicht der Name.
        assert suggested[0].name == suggested[1].name == N26_TEMPLATE_NAME
        assert suggested[0].mapping["fields"]["booking_date"] == "Buchungstag"


def test_suggest_requires_account_id(factory: sessionmaker[Session]) -> None:
    """F-IMP-05: Ohne Konto gibt es keine Vorschlagsliste."""
    with factory() as session:
        with pytest.raises(ValueError):
            suggest_templates(session, None)  # type: ignore[arg-type]


def test_seed_creates_exactly_n26_easybank_paypal_and_is_idempotent(factory: sessionmaker[Session]) -> None:
    """F-IMP-05: Genau drei Standardvorlagen, zweiter Aufruf legt nichts an."""
    with factory() as session:
        first = seed_default_templates(session)
        assert {template.name for template in first} == set(DEFAULT_TEMPLATE_NAMES)
        assert len(first) == 3
        for template in first:
            assert template.account_id is None
            assert template.kind == "csv"

    with factory() as session:
        seeded = list_templates(session)
        assert {template.name for template in seeded} == {
            N26_TEMPLATE_NAME,
            EASYBANK_TEMPLATE_NAME,
            PAYPAL_TEMPLATE_NAME,
        }
        assert len(seeded) == 3
        # Der eigene CSV-Export entsteht erst in M4 (F-EXP-03) und wird nicht geseedet.
        assert not any("eigene" in template.name.lower() for template in seeded)

    with factory() as session:
        second = seed_default_templates(session)
        assert second == []

    with factory() as session:
        assert len(list_templates(session)) == 3


def test_seeded_mappings_are_apply_ready(factory: sessionmaker[Session]) -> None:
    """F-IMP-02/03: Jede Seed-Vorlage laesst sich direkt auf eine Zeile anwenden."""
    with factory() as session:
        seed_default_templates(session)
        templates = {template.name: template for template in list_templates(session)}

    expected_rows = {
        N26_TEMPLATE_NAME: (
            {
                "Booking Date": "2026-01-31",
                "Value Date": "2026-01-31",
                "Partner Name": "Online-Shop",
                "Partner Iban": "DE02120300000000202051",
                "Payment Reference": "Rechnung 42",
                "Amount (EUR)": "-12.34",
            },
            -1234,
        ),
        EASYBANK_TEMPLATE_NAME: (
            {
                "Buchungsdatum": "31.01.2026",
                "Beschreibung": "Online-Shop",
                "Waehrung": "EUR",
                "Betrag": "12,34",
            },
            -1234,
        ),
        PAYPAL_TEMPLATE_NAME: (
            {
                "Date": "01/31/2026",
                "Name": "Online-Shop",
                "Currency": "EUR",
                "Net": "-12.34",
                "Transaction ID": "TX-123",
            },
            -1234,
        ),
    }

    for name, (row, expected_cents) in expected_rows.items():
        template = templates[name]
        options = {
            "date_format": template.date_format,
            "decimal_separator": template.decimal_separator,
        }

        result = apply_mapping([row], template.mapping, options)

        assert len(result) == 1
        assert result[0].booking_date == date(2026, 1, 31)
        assert result[0].amount_cents == expected_cents
        assert result[0].currency == "EUR"


def test_paypal_template_binds_pre_filter_and_others_do_not(factory: sessionmaker[Session]) -> None:
    """F-IMP-08: Nur die PayPal-Vorlage nennt den Vorverarbeitungsschritt."""
    with factory() as session:
        seed_default_templates(session)
        templates = {template.name: template for template in list_templates(session)}

    assert template_pre_filter(templates[PAYPAL_TEMPLATE_NAME]) == PAYPAL_PRE_FILTER
    assert PAYPAL_PRE_FILTER == "paypal"
    assert template_pre_filter(templates[N26_TEMPLATE_NAME]) is None
    assert template_pre_filter(templates[EASYBANK_TEMPLATE_NAME]) is None


def test_update_and_delete_template(factory: sessionmaker[Session]) -> None:
    """F-IMP-05: Aendern und Loeschen funktionieren inklusive NULL-Feldern."""
    with factory() as session:
        created = create_template(
            session,
            name="Alt",
            kind="csv",
            mapping=_MINIMAL_MAPPING,
            delimiter=";",
            decimal_separator="comma",
        )
        template_id = created.id

    with factory() as session:
        updated = update_template(
            session,
            template_id,
            name="Neu",
            delimiter=None,
            date_format="JJJJ-MM-TT",
        )

        assert updated.name == "Neu"
        assert updated.delimiter is None
        assert updated.date_format == "JJJJ-MM-TT"
        assert updated.decimal_separator == "comma"
        assert updated.version == 2

    with factory() as session:
        assert delete_template(session, template_id) is True
        assert get_template(session, template_id) is None
        assert delete_template(session, template_id) is False


@pytest.mark.parametrize(
    ("name", "kind", "mapping"),
    [
        ("   ", "csv", _MINIMAL_MAPPING),
        ("Test", "excel", _MINIMAL_MAPPING),
        ("Test", "csv", ["kein", "objekt"]),
    ],
)
def test_create_template_rejects_invalid_input(
    factory: sessionmaker[Session],
    name: str,
    kind: str,
    mapping: object,
) -> None:
    """F-IMP-05: Ungueltige Eingaben werden vor dem Schreiben abgelehnt."""
    with factory() as session:
        with pytest.raises(ValueError):
            create_template(
                session,
                name=name,
                kind=kind,
                mapping=mapping,  # type: ignore[arg-type]
            )
        assert list_templates(session) == []
