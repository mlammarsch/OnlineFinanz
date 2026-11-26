"""Tests fuer das Ersetzen von Vormerkungen und die Flag-Uebertragung.

Referenziert F-DUP-04 (Vormerkungen nehmen nicht am Fingerprint-Abgleich teil,
jeder Sync ersetzt sie komplett, Notiz und Pruef-Flag gehen bei genau einem
Kandidaten nach Betrag, Gegenpartei und +/- 10 Tagen ueber, sonst bleibt ein
Hinweis im Protokoll), F-CON-05 (ein Sync aendert nie Notiz, geprueft,
abgestimmt oder externe Referenz bereits gebuchter Buchungen) und F-CON-01
(Versionsspalte wirkt auch beim Loeschen).

Alle Buchungen sind erfundene Testdaten (D8), Bankzugriffe finden nicht statt.
"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.transaction import Transaction
from finue.services.duplicate import TransactionData, compute_fingerprint, normalize_field
from finue.services.pending_sync import (
    CODE_PENDING_HINT,
    PendingReplacement,
    replace_pending,
    transfer_flags,
)

CHECKED_AT = datetime(2026, 1, 6, 9, 30, tzinfo=timezone.utc)


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture()
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Liefert eine Session je Test."""
    with factory() as db_session:
        yield db_session


def create_account(session: Session) -> Account:
    """Legt ein Testkonto an und gibt es mit gesetzter ID zurueck."""
    account = Account(name="Testkonto", currency="EUR", source="fints")
    session.add(account)
    session.commit()
    return account


def add_transaction(
    session: Session,
    account_id: int,
    *,
    status: str = "gebucht",
    amount_cents: int = -100_000,
    booking_date: date = date(2026, 1, 5),
    counterparty: str | None = "Vermieter",
    purpose: str | None = "Miete",
    occurrence_index: int = 0,
    note: str | None = None,
    checked: bool = False,
    checked_at: datetime | None = None,
    reconciled: bool = False,
    external_ref: str | None = None,
) -> Transaction:
    """Legt eine Buchung mit gueltigem Fingerprint an und gibt sie zurueck."""
    fingerprint = compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=None,
        amount_cents=amount_cents,
        currency="EUR",
        counterparty=counterparty,
        counterparty_iban=None,
        purpose=purpose,
    )
    transaction = Transaction(
        account_id=account_id,
        status=status,
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency="EUR",
        counterparty=counterparty,
        purpose=purpose,
        origin="fints",
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
        note=note,
        checked=checked,
        checked_at=checked_at,
        reconciled=reconciled,
        external_ref=external_ref,
    )
    session.add(transaction)
    session.commit()
    return transaction


def make_pending(
    *,
    booking_date: date = date(2026, 1, 5),
    amount_cents: int = -100_000,
    counterparty: str | None = "Vermieter",
    purpose: str | None = "Miete",
    status: str = "vorgemerkt",
) -> TransactionData:
    """Erzeugt eine eingehende Vormerkung mit F-DUP-01-Standardfeldern."""
    return TransactionData(
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency="EUR",
        counterparty=counterparty,
        purpose=purpose,
        origin="fints",
        status=status,
    )


def pending_rows(session: Session, account_id: int) -> list[Transaction]:
    """Liest alle Vormerkungen eines Kontos, sortiert nach ID."""
    statement = (
        select(Transaction)
        .where(Transaction.account_id == account_id, Transaction.status == "vorgemerkt")
        .order_by(Transaction.id)
    )
    return list(session.scalars(statement).all())


def make_run(session: Session, account_id: int) -> ImportRun:
    """Legt einen Sync-ImportRun an, damit Fehlerzeilen angehaengt werden koennen."""
    run = ImportRun(account_id=account_id, kind="sync", status="laeuft")
    session.add(run)
    session.flush()
    return run


def error_rows(run: ImportRun) -> list[ImportRunError]:
    """Liefert die Fehlerzeilen des Laufs."""
    return list(run.errors)


def test_ersetzt_alle_vormerkungen_und_laesst_gebuchte_unberuehrt(session: Session) -> None:
    """F-DUP-04: Der Sync ersetzt alle Vormerkungen, gebuchte bleiben unveraendert."""
    account = create_account(session)
    booked = add_transaction(session, account.id, purpose="Dauerauftrag", note="Bleibt")
    old_one = add_transaction(session, account.id, status="vorgemerkt", purpose="Alt 1")
    old_two = add_transaction(session, account.id, status="vorgemerkt", purpose="Alt 2")
    booked_version = booked.version

    replacement = replace_pending(
        session,
        account.id,
        [
            make_pending(purpose="Neu 1"),
            make_pending(purpose="Neu 2"),
            make_pending(purpose="Neu 3"),
        ],
    )

    assert isinstance(replacement, PendingReplacement)
    assert replacement.removed_count == 2
    assert replacement.inserted_count == 3
    assert {snapshot.transaction_id for snapshot in replacement.removed} == {old_one.id, old_two.id}

    remaining_pending = pending_rows(session, account.id)
    assert len(remaining_pending) == 3
    assert {row.id for row in remaining_pending} == {row.id for row in replacement.inserted}
    assert all(row.status == "vorgemerkt" for row in remaining_pending)

    session.refresh(booked)
    assert booked.note == "Bleibt"
    assert booked.status == "gebucht"
    assert booked.version == booked_version


def test_leere_lieferung_entfernt_alle_vormerkungen(session: Session) -> None:
    """F-DUP-04: Ohne Vormerkungen in der Lieferung bleibt keine alte Vormerkung stehen."""
    account = create_account(session)
    add_transaction(session, account.id, status="vorgemerkt", purpose="Alt 1")
    add_transaction(session, account.id, status="vorgemerkt", purpose="Alt 2")
    booked = add_transaction(session, account.id, purpose="Gebucht")

    replacement = replace_pending(session, account.id, [])

    assert replacement.removed_count == 2
    assert replacement.inserted_count == 0
    assert pending_rows(session, account.id) == []
    session.refresh(booked)
    assert booked.status == "gebucht"


def test_vormerkung_nimmt_nicht_am_fingerprint_abgleich_teil(session: Session) -> None:
    """F-DUP-04: Dieselbe Buchung als Vormerkung wird trotz gleichem Fingerprint angelegt."""
    account = create_account(session)
    booked = add_transaction(session, account.id, occurrence_index=0, purpose="Miete Januar")
    expected_fingerprint = booked.fingerprint

    replacement = replace_pending(
        session,
        account.id,
        [make_pending(purpose="Miete Januar")],
    )

    assert replacement.inserted_count == 1
    inserted = replacement.inserted[0]
    assert inserted.fingerprint == expected_fingerprint
    assert inserted.occurrence_index == 1
    assert inserted.id != booked.id
    assert len(pending_rows(session, account.id)) == 1


def test_eindeutiger_kandidat_uebernimmt_notiz_und_pruef_flag(session: Session) -> None:
    """F-DUP-04: Bei genau einem Kandidaten gehen Notiz und Pruef-Flag auf die Buchung ueber."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        booking_date=date(2026, 1, 5),
        note="Miete Januar",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        booking_date=date(2026, 1, 8),
        counterparty="Vermieter",
        purpose="Miete Januar gebucht",
    )
    booked_version = booked.version
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 1
    assert result.ambiguous == ()
    assert result.hints == ()
    assert result.transferred[0].booked is booked
    session.refresh(booked)
    assert booked.note == "Miete Januar"
    assert booked.checked is True
    assert booked.checked_at is not None
    assert booked.checked_at.replace(tzinfo=timezone.utc) == CHECKED_AT
    assert booked.version > booked_version
    assert error_rows(run) == []


def test_mehrere_kandidaten_ohne_uebertragung_mit_protokoll(session: Session) -> None:
    """F-DUP-04: Mehrere Kandidaten verhindern die Uebertragung und erzeugen einen Hinweis."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        booking_date=date(2026, 1, 5),
        note="Miete Januar",
        checked=True,
        checked_at=CHECKED_AT,
    )
    first = add_transaction(session, account.id, booking_date=date(2026, 1, 7), purpose="Miete Kandidat 1")
    second = add_transaction(session, account.id, booking_date=date(2026, 1, 9), purpose="Miete Kandidat 2")
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [first, second], run=run)

    assert result.transferred_count == 0
    assert len(result.ambiguous) == 1
    assert len(result.hints) == 1
    assert "mehrdeutig" in result.hints[0]
    errors = error_rows(run)
    assert len(errors) == 1
    assert errors[0].code == CODE_PENDING_HINT
    assert errors[0].message == result.hints[0]
    session.refresh(first)
    session.refresh(second)
    assert first.note is None and first.checked is False
    assert second.note is None and second.checked is False


def test_kein_kandidat_ohne_uebertragung_mit_protokoll(session: Session) -> None:
    """F-DUP-04: Ohne Kandidat bleibt die Notiz als Hinweis im Protokoll."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        booking_date=date(2026, 1, 5),
        note="Miete Januar",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        booking_date=date(2026, 2, 20),
        purpose="Voellig anderer Vorgang",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 0
    assert len(result.ambiguous) == 1
    assert result.hints[0].startswith("Vormerkung vom 2026-01-05")
    errors = error_rows(run)
    assert [error.code for error in errors] == [CODE_PENDING_HINT]
    session.refresh(booked)
    assert booked.note is None
    assert booked.checked is False


@pytest.mark.parametrize("offset_days", [-10, 10])
def test_randtag_zehn_tage_zaehlt_als_kandidat(session: Session, offset_days: int) -> None:
    """F-DUP-04: +/- 10 Tage sind einschliesslich, der Randtag passt noch."""
    account = create_account(session)
    pending_date = date(2026, 1, 15)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        booking_date=pending_date,
        note="Randtag",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        booking_date=pending_date + timedelta(days=offset_days),
        purpose="Randtag gebucht",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 1
    session.refresh(booked)
    assert booked.note == "Randtag"
    assert error_rows(run) == []


@pytest.mark.parametrize("offset_days", [-11, 11])
def test_ausserhalb_zehn_tagen_kein_kandidat(session: Session, offset_days: int) -> None:
    """F-DUP-04: Elf Tage Abstand liegen ausserhalb des Kandidatenfensters."""
    account = create_account(session)
    pending_date = date(2026, 1, 15)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        booking_date=pending_date,
        note="Zu weit weg",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        booking_date=pending_date + timedelta(days=offset_days),
        purpose="Zu weit weg gebucht",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 0
    assert len(error_rows(run)) == 1
    session.refresh(booked)
    assert booked.note is None


@pytest.mark.parametrize("delta_cents", [-1, 1])
def test_abweichender_betrag_ist_kein_kandidat(session: Session, delta_cents: int) -> None:
    """F-DUP-04: Schon ein Cent Abweichung macht die Buchung zum Nicht-Kandidaten."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        amount_cents=-100_000,
        note="Betrag anders",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        amount_cents=-100_000 + delta_cents,
        purpose="Betrag anders gebucht",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 0
    assert len(error_rows(run)) == 1
    session.refresh(booked)
    assert booked.note is None


def test_abweichende_gegenpartei_ist_kein_kandidat(session: Session) -> None:
    """F-DUP-04: Die normalisierte Gegenpartei muss uebereinstimmen."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        counterparty="Vermieter GmbH",
        note="Gegenpartei anders",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        counterparty="Stadtwerke AG",
        purpose="Gegenpartei anders gebucht",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 0
    assert len(error_rows(run)) == 1


def test_gegenpartei_vergleich_ist_normalisiert(session: Session) -> None:
    """F-DUP-04: Gross-/Kleinschreibung und Leerraum aendern die Zuordnung nicht."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        counterparty="  vermieter   gmbh ",
        note="Normalisiert",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        counterparty="VERMIETER GMBH",
        purpose="Normalisiert gebucht",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 1
    assert normalize_field("  vermieter   gmbh ") == normalize_field("VERMIETER GMBH")


def test_bestehende_gebuchte_buchung_bleibt_unveraendert(session: Session) -> None:
    """F-CON-05: Ein Sync aendert nie Nutzerfelder bereits gebuchter Buchungen."""
    account = create_account(session)
    existing = add_transaction(
        session,
        account.id,
        booking_date=date(2026, 1, 6),
        note="Eigene Notiz",
        checked=True,
        checked_at=CHECKED_AT,
        reconciled=True,
        external_ref="EXT-42",
    )
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        booking_date=date(2026, 1, 5),
        note="Vormerkungsnotiz",
        checked=True,
        checked_at=CHECKED_AT,
    )
    before = {
        "note": existing.note,
        "checked": existing.checked,
        "checked_at": existing.checked_at,
        "reconciled": existing.reconciled,
        "external_ref": existing.external_ref,
        "version": existing.version,
    }
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    # Die bestehende Buchung passt inhaltlich, wird aber nicht als neu gebucht uebergeben.
    result = transfer_flags(session, account.id, [], run=run)

    assert result.transferred_count == 0
    assert len(error_rows(run)) == 1
    session.refresh(existing)
    assert existing.note == before["note"]
    assert existing.checked is before["checked"]
    assert existing.checked_at == before["checked_at"]
    assert existing.reconciled is before["reconciled"]
    assert existing.external_ref == before["external_ref"]
    assert existing.version == before["version"]


def test_vormerkung_ohne_nutzerdaten_erzeugt_keinen_hinweis(session: Session) -> None:
    """F-DUP-04: Ohne Notiz und ohne Pruef-Flag gibt es nichts zu uebertragen."""
    account = create_account(session)
    add_transaction(session, account.id, status="vorgemerkt")
    booked = add_transaction(session, account.id, booking_date=date(2026, 1, 8))
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert result.transferred_count == 0
    assert result.ambiguous == ()
    assert result.hints == ()
    assert error_rows(run) == []


def test_hinweis_enthaelt_keine_geheimnisse(session: Session) -> None:
    """N-SEC-02: Der Protokollhinweis nennt nur Datum und Betrag, keine Details."""
    account = create_account(session)
    add_transaction(
        session,
        account.id,
        status="vorgemerkt",
        counterparty="GEHEIME GEGENPARTEI",
        note="Geheime Notiz",
        checked=True,
        checked_at=CHECKED_AT,
    )
    booked = add_transaction(
        session,
        account.id,
        counterparty="Stadtwerke AG",
        purpose="Anderer Vorgang",
    )
    run = make_run(session, account.id)

    replace_pending(session, account.id, [])
    result = transfer_flags(session, account.id, [booked], run=run)

    assert len(result.hints) == 1
    hint = result.hints[0]
    assert "2026-01-05" in hint
    assert "-1000,00 EUR" in hint
    assert "GEHEIME GEGENPARTEI" not in hint
    assert "Geheime Notiz" not in hint
    assert "PIN" not in hint
    assert "TAN" not in hint


def test_nur_vormerkungen_als_lieferung(session: Session) -> None:
    """F-DUP-04: Gebuchte Zeilen sind keine gueltige Vormerkungslieferung."""
    account = create_account(session)

    with pytest.raises(ValueError, match="vorgemerkte"):
        replace_pending(session, account.id, [make_pending(status="gebucht")])


def test_versionsspalte_verhindert_stilles_loeschen(tmp_path: Path) -> None:
    """F-CON-01: Eine zwischenzeitlich geaenderte Vormerkung wird nicht still geloescht."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    with factory() as setup:
        account = Account(name="Konfliktkonto", currency="EUR", source="fints")
        setup.add(account)
        setup.commit()
        account_id = account.id
        add_transaction(setup, account_id, status="vorgemerkt", note="Erster Stand")

    with factory() as first:
        stale = first.scalars(
            select(Transaction).where(Transaction.account_id == account_id, Transaction.status == "vorgemerkt")
        ).one()
        stale_id = stale.id
        assert stale.version == 1
        # Transaktion beenden, Objekt bleibt mit Stand 1 im Identity-Map.
        first.commit()

        with factory() as second:
            concurrent = second.get(Transaction, stale_id)
            assert concurrent is not None
            concurrent.note = "Zwischenzeitlich geaendert"
            second.commit()

        with pytest.raises(StaleDataError):
            replace_pending(first, account_id, [])
