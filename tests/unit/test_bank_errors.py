"""Tests fuer das Bankfehler-Mapping (T-066).

Referenziert F-SYN-05 (Bankcode -> Klartext, unbekannte Codes zeigen den
Originaltext), F-SYN-06 (9800/9010 wiederholbar), F-SYN-09 (Prioritaet,
Standardeintraege, Reset ohne Verlust eigener Eintraege) sowie F-CON-01/02
(Versionspruefung bei Aenderungen) und N-SEC-02 (keine Geheimnisse in
Standardtexten). Alle Daten sind erfunden.

Der Test deckt bewusst auch das T-063-Risiko ab: SQLite (wie PostgreSQL und
MySQL) erlaubt mehrere ``NULL``-Werte in ``UNIQUE (code, blz)``. Der Service
muss allgemeine Duplikate deshalb selbst verhindern.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.bank_error import BankErrorMapping
from finue.services.bank_error_defaults import (
    DEFAULT_BANK_ERRORS,
    DEFAULT_BANK_ERROR_CODES,
    ING_LOGIN_EXPIRED_CODE,
    UNVERIFIED_DEFAULT_CODES,
    DefaultBankError,
    reset_to_defaults,
    seed_defaults,
)
from finue.services.bank_errors import (
    BankErrorScopeConflict,
    BankErrorVersionConflict,
    create_error,
    delete_error,
    get_error,
    list_errors,
    resolve_error,
    update_error,
)


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Isolierte In-Memory-SQLite-Datenbank mit allen ORM-Tabellen."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _default(code: str) -> DefaultBankError:
    """Liefert den Seed-Eintrag zu einem Code."""
    matches = [entry for entry in DEFAULT_BANK_ERRORS if entry.code == code]
    assert len(matches) == 1, f"Seed-Code {code} fehlt oder ist doppelt"
    return matches[0]


def _entry_by_code(session: Session, code: str) -> BankErrorMapping:
    """Liest genau einen Eintrag zu einem Code."""
    rows = list_errors(session, code=code)
    assert len(rows) == 1, f"Erwartet genau einen Eintrag fuer {code}, gefunden {len(rows)}"
    return rows[0]


def _add_default(
    session: Session,
    code: str,
    *,
    blz: str | None = None,
    message: str,
    recommendation: str = "Standardempfehlung",
    retryable: bool = False,
) -> BankErrorMapping:
    """Legt fuer Prioritaetstests einen Standardeintrag direkt an."""
    entry = BankErrorMapping(
        code=code,
        blz=blz,
        message=message,
        recommendation=recommendation,
        retryable=retryable,
        is_default=True,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def test_seed_is_idempotent_and_marks_all_defaults(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Seed legt alle Standards an und wiederholt sich ohne Duplikate."""
    with factory() as session:
        assert seed_defaults(session) == len(DEFAULT_BANK_ERRORS)
        assert seed_defaults(session) == 0

        entries = list_errors(session)
        assert {entry.code for entry in entries} == set(DEFAULT_BANK_ERROR_CODES)
        assert all(entry.is_default is True for entry in entries)
        assert all(entry.blz is None for entry in entries)
        assert len({(entry.code, entry.blz) for entry in entries}) == len(entries)


def test_default_retryable_codes_f_syn_06(factory: sessionmaker[Session]) -> None:
    """F-SYN-06: Nur 9800 und 9010 sind im Standard wiederholbar."""
    with factory() as session:
        seed_defaults(session)
        entries = {entry.code: entry for entry in list_errors(session)}

        assert entries["9800"].retryable is True
        assert entries["9010"].retryable is True
        for code in ("9340", "9910", "9930", "9931", "9942", "3938", ING_LOGIN_EXPIRED_CODE):
            assert entries[code].retryable is False, f"{code} darf nicht wiederholbar sein"


def test_default_entries_have_expected_codes_and_no_secrets(factory: sessionmaker[Session]) -> None:
    """F-SYN-05/09, N-SEC-02: Belegte Codes und geheimnisfreie Standardtexte."""
    with factory() as session:
        seed_defaults(session)
        entries = list_errors(session)
        codes = {entry.code for entry in entries}

        assert {"9800", "9010", "3938", ING_LOGIN_EXPIRED_CODE} <= codes
        assert {"9340", "9910", "9930", "9931", "9942"} <= codes
        assert UNVERIFIED_DEFAULT_CODES == frozenset({ING_LOGIN_EXPIRED_CODE})
        assert _default(ING_LOGIN_EXPIRED_CODE).verified is False

        forbidden = ("FANTASIE-PIN-4711", "FANTASIE-TAN-0815", "geheimes-passwort")
        for entry in entries:
            assert entry.message.strip()
            assert entry.recommendation.strip()
            for value in (entry.message, entry.recommendation):
                assert not any(secret in value for secret in forbidden)


def test_resolve_uses_default_and_unknown_original_text(factory: sessionmaker[Session]) -> None:
    """F-SYN-05: Standardtreffer und Originaltext bei unbekanntem Code."""
    with factory() as session:
        seed_defaults(session)

        hit = resolve_error(session, "9800", None, "Originaltext der Bank")
        expected = _default("9800")
        assert hit.source == "default"
        assert hit.message == expected.message
        assert hit.recommendation == expected.recommendation
        assert hit.retryable is True

        miss = resolve_error(session, "9999", "10010010", "Die Bank hat etwas abgelehnt.")
        assert miss.source == "unbekannt"
        assert miss.message == "Die Bank hat etwas abgelehnt."
        assert miss.retryable is False
        assert miss.recommendation.strip()

        empty = resolve_error(session, "9999", None, "   ")
        assert empty.source == "unbekannt"
        assert "9999" in empty.message


def test_own_blz_entry_beats_default_general(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Eigener BLZ-Eintrag vor allgemeinem Standard."""
    with factory() as session:
        seed_defaults(session)
        own = create_error(
            session,
            code="9800",
            blz="10010010",
            message="Eigener BLZ-Klartext",
            recommendation="Eigene BLZ-Empfehlung",
        )

        specific = resolve_error(session, "9800", "10010010", "Original")
        assert specific.source == "eigen"
        assert specific.message == own.message

        other_blz = resolve_error(session, "9800", "20020020", "Original")
        assert other_blz.source == "default"
        assert other_blz.message == _default("9800").message


def test_own_general_entry_beats_default_blz(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Eigener allgemeiner Eintrag vor BLZ-Standardeintrag."""
    with factory() as session:
        _add_default(session, "TEST-A", blz="20020020", message="Standard nur fuer BLZ")
        own = create_error(
            session,
            code="TEST-A",
            blz=None,
            message="Eigener allgemeiner Klartext",
            recommendation="Eigene allgemeine Empfehlung",
        )

        resolved = resolve_error(session, "TEST-A", "20020020", "Original")
        assert resolved.source == "eigen"
        assert resolved.message == own.message


def test_default_blz_entry_beats_default_general(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: BLZ-Standard vor allgemeinem Standard."""
    with factory() as session:
        _add_default(session, "TEST-B", blz=None, message="Standard allgemein")
        _add_default(session, "TEST-B", blz="20020020", message="Standard nur fuer BLZ")

        specific = resolve_error(session, "TEST-B", "20020020", "Original")
        assert specific.source == "default"
        assert specific.message == "Standard nur fuer BLZ"

        general = resolve_error(session, "TEST-B", "30030030", "Original")
        assert general.source == "default"
        assert general.message == "Standard allgemein"


def test_own_blz_entry_beats_own_general(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Innerhalb eigener Eintraege gewinnt die BLZ-Bindung."""
    with factory() as session:
        own_general = create_error(
            session,
            code="TEST-C",
            blz=None,
            message="Eigener allgemeiner Klartext",
            recommendation="Eigene allgemeine Empfehlung",
        )
        own_blz = create_error(
            session,
            code="TEST-C",
            blz="10010010",
            message="Eigener BLZ-Klartext",
            recommendation="Eigene BLZ-Empfehlung",
        )

        specific = resolve_error(session, "TEST-C", "10010010", "Original")
        assert specific.source == "eigen"
        assert specific.message == own_blz.message

        general = resolve_error(session, "TEST-C", "20020020", "Original")
        assert general.source == "eigen"
        assert general.message == own_general.message


def test_seed_does_not_overwrite_existing_own_entry(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Eigener Eintrag bleibt beim Seeding unangetastet."""
    with factory() as session:
        own = create_error(
            session,
            code="9800",
            blz=None,
            message="Eigene 9800-Meldung",
            recommendation="Eigene 9800-Empfehlung",
        )

        created = seed_defaults(session)
        assert created == len(DEFAULT_BANK_ERRORS) - 1

        rows = list_errors(session, code="9800")
        assert len(rows) == 1
        assert rows[0].id == own.id
        assert rows[0].is_default is False
        assert rows[0].message == "Eigene 9800-Meldung"


def test_reset_restores_defaults_and_keeps_own_entries(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Reset stellt Standards wieder her, eigene Eintraege bleiben."""
    with factory() as session:
        seed_defaults(session)
        mutable_default = _entry_by_code(session, "9800")
        update_error(
            session,
            mutable_default.id,
            mutable_default.version,
            message="Vom Nutzer geaendert",
            retryable=False,
        )
        own = create_error(
            session,
            code="CUSTOM",
            blz="10010010",
            message="Eigener Klartext",
            recommendation="Eigene Empfehlung",
        )
        own_id = own.id
        own_message = own.message

        created = reset_to_defaults(session)
        assert created == len(DEFAULT_BANK_ERRORS)

        restored = _entry_by_code(session, "9800")
        expected = _default("9800")
        assert restored.message == expected.message
        assert restored.retryable is True
        assert restored.is_default is True

        own_after = get_error(session, own_id)
        assert own_after is not None
        assert own_after.is_default is False
        assert own_after.message == own_message
        assert own_after.blz == "10010010"


def test_reset_keeps_own_entry_that_shadows_a_default(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Eigener Eintrag mit gleichem Scope bleibt auch beim Reset."""
    with factory() as session:
        own = create_error(
            session,
            code="9800",
            blz=None,
            message="Eigene 9800-Meldung",
            recommendation="Eigene 9800-Empfehlung",
        )

        reset_to_defaults(session)

        rows = list_errors(session, code="9800")
        assert len(rows) == 1
        assert rows[0].id == own.id
        assert rows[0].is_default is False
        assert rows[0].message == "Eigene 9800-Meldung"


def test_service_blocks_duplicate_null_scope(factory: sessionmaker[Session]) -> None:
    """F-SYN-09, T-063: Allgemeine Duplikate trotz NULL-Unique verhindern."""
    with factory() as session:
        create_error(
            session,
            code="DUP",
            blz=None,
            message="Erster allgemeiner Eintrag",
            recommendation="Erste Empfehlung",
        )
        with pytest.raises(BankErrorScopeConflict):
            create_error(
                session,
                code="DUP",
                blz=None,
                message="Zweiter allgemeiner Eintrag",
                recommendation="Zweite Empfehlung",
            )

        seed_defaults(session)
        with pytest.raises(BankErrorScopeConflict):
            create_error(
                session,
                code="9800",
                blz=None,
                message="Eigene Kopie des Standards",
                recommendation="Eigene Empfehlung",
            )


def test_database_allows_multiple_null_scopes_but_service_guards(factory: sessionmaker[Session]) -> None:
    """F-SYN-09, T-063: SQLite-NULL-Unique-Risiko ist real und service-seitig abgesichert."""
    with factory() as session:
        for message in ("Erster", "Zweiter"):
            session.add(
                BankErrorMapping(
                    code="NULL-DUP",
                    blz=None,
                    message=message,
                    recommendation="Empfehlung",
                    retryable=False,
                    is_default=False,
                )
            )
            session.commit()

        assert len(list_errors(session, code="NULL-DUP")) == 2

        with pytest.raises(BankErrorScopeConflict):
            create_error(
                session,
                code="NULL-DUP",
                blz=None,
                message="Dritter",
                recommendation="Empfehlung",
            )


def test_update_requires_current_version_f_con_02(factory: sessionmaker[Session]) -> None:
    """F-CON-02: Update mit alter Version wird abgewiesen, ohne Aenderung."""
    with factory() as session:
        entry = create_error(
            session,
            code="VERSION",
            blz=None,
            message="Alt",
            recommendation="Empfehlung",
        )
        assert entry.version == 1

        updated = update_error(session, entry.id, 1, message="Neu")
        assert updated.version == 2

        with pytest.raises(BankErrorVersionConflict) as info:
            update_error(session, entry.id, 1, message="Nochmal")
        assert info.value.current_version == 2

        current = get_error(session, entry.id)
        assert current is not None
        assert current.message == "Neu"


def test_update_detects_concurrent_change_f_con_01(factory: sessionmaker[Session]) -> None:
    """F-CON-01/02: Wettlauf zwischen Pruefung und Commit liefert Konflikt."""
    with factory() as session:
        entry_id = create_error(
            session,
            code="RACE",
            blz=None,
            message="Alt",
            recommendation="Empfehlung",
        ).id

    with factory() as first, factory() as second:
        loaded_first = get_error(first, entry_id)
        loaded_second = get_error(second, entry_id)
        assert loaded_first is not None
        assert loaded_second is not None

        update_error(first, entry_id, loaded_first.version, message="Erste Sitzung")
        with pytest.raises(BankErrorVersionConflict):
            update_error(second, entry_id, loaded_second.version, message="Zweite Sitzung")


def test_delete_requires_current_version_f_con_02(factory: sessionmaker[Session]) -> None:
    """F-CON-02: Loeschen prueft die Version ebenfalls."""
    with factory() as session:
        entry = create_error(
            session,
            code="DELETE",
            blz=None,
            message="Alt",
            recommendation="Empfehlung",
        )
        update_error(session, entry.id, 1, message="Neu")

        with pytest.raises(BankErrorVersionConflict):
            delete_error(session, entry.id, 1)

        delete_error(session, entry.id, 2)
        assert get_error(session, entry.id) is None


def test_update_scope_conflict_is_rejected(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Umhaengen auf einen belegten Scope wird abgewiesen."""
    with factory() as session:
        first = create_error(
            session,
            code="SCOPE",
            blz="10010010",
            message="Erster",
            recommendation="Empfehlung",
        )
        second = create_error(
            session,
            code="SCOPE",
            blz="20020020",
            message="Zweiter",
            recommendation="Empfehlung",
        )

        with pytest.raises(BankErrorScopeConflict):
            update_error(session, second.id, second.version, blz="10010010")

        current = get_error(session, second.id)
        assert current is not None
        assert current.blz == "20020020"
        assert first.blz == "10010010"


def test_crud_list_and_delete(factory: sessionmaker[Session]) -> None:
    """F-SYN-09: Lesen, Filtern und Loeschen eigener Eintraege."""
    with factory() as session:
        first = create_error(
            session,
            code="CRUD",
            blz="10010010",
            message="Erster",
            recommendation="Empfehlung",
        )
        second = create_error(
            session,
            code="CRUD",
            blz="20020020",
            message="Zweiter",
            recommendation="Empfehlung",
        )
        other = create_error(
            session,
            code="OTHER",
            blz=None,
            message="Anderer",
            recommendation="Empfehlung",
        )

        assert [entry.id for entry in list_errors(session, code="CRUD")] == [first.id, second.id]
        assert get_error(session, first.id) is not None

        delete_error(session, other.id, other.version)
        assert get_error(session, other.id) is None
        assert len(list_errors(session)) == 2


def test_resolved_error_repr_contains_no_bank_text(factory: sessionmaker[Session]) -> None:
    """N-SEC-02: technisches repr gibt keinen Banktext aus."""
    with factory() as session:
        resolved = resolve_error(session, "9999", None, "Vertraulicher Originaltext")
        representation = repr(resolved)

        assert "Vertraulicher Originaltext" not in representation
        assert "source='unbekannt'" in representation
        assert resolved.message == "Vertraulicher Originaltext"
