"""Tests fuer die Sync-Persistenz je Konto mit ImportRun-Protokoll (T-074).

Referenziert F-SYN-03 (Saldo, gebuchte und vorgemerkte Umsaetze, Zeitraum ab
dem letzten erfolgreichen Sync minus 14 Tage, beim ersten Sync ``None``),
F-SYN-07 (ImportRun kind sync mit job_id, Zaehlern, Status und Klartext ohne
Geheimnisse), F-SYN-10 (je Konto genau eine Transaktion; Abbruch/Exception
laesst das aktuelle Konto ungespeichert, abgeschlossene Konten bleiben),
F-CON-05 (ein Sync aendert nie Notiz, geprueft, abgestimmt oder externe
Referenz bestehender Buchungen), F-DUP-02 (Multimengen-Abgleich, idempotenter
Re-Sync), F-DUP-03 (Verdachtsdubletten) und F-DUP-04 (Vormerkungen komplett
ersetzen, Notiz/Pruef-Flag uebertragen).

Alle Buchungen und Konten sind erfundene Testdaten (D8). Es finden keine
Bankzugriffe statt; PIN, TAN und Dialogdaten kommen nicht vor.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from finue.db.base import Base
from finue.db.engine import create_db_engine, make_session_factory
from finue.models.account import Account
from finue.models.import_run import ImportRun
from finue.models.transaction import Transaction
from finue.services import sync_persist as sync_persist_module
from finue.services.bank_error_defaults import seed_defaults
from finue.services.duplicate import compute_fingerprint
from finue.services.fints_types import (
    BalanceResult,
    FetchedTransaction,
    FinTSBankError,
    SyncAccountData,
    SyncCancelledError,
    TanTimeoutError,
    TransactionStatus,
)
from finue.services.sync_persist import (
    FETCH_OVERLAP_DAYS,
    compute_fetch_range,
    persist_account_sync,
    persist_sync_failure,
)

UTC = timezone.utc
CHECKED_AT = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)


@pytest.fixture()
def factory() -> sessionmaker[Session]:
    """Erstellt eine In-Memory-SQLite mit vollstaendigem Schema."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture()
def session(factory: sessionmaker[Session]) -> Session:
    """Liefert eine Session je Test."""
    with factory() as db_session:
        yield db_session


def create_account(session: Session, name: str = "Testkonto") -> Account:
    """Legt ein Testkonto an und gibt es mit gesetzter ID zurueck."""
    account = Account(name=name, currency="EUR", source="fints")
    session.add(account)
    session.commit()
    return account


def make_fetched(
    *,
    booking_date: date = date(2026, 1, 10),
    amount_cents: int = -1000,
    currency: str = "EUR",
    status: TransactionStatus = "gebucht",
    value_date: date | None = None,
    counterparty: str | None = "Gegenpartei",
    counterparty_iban: str | None = "DE00370400440532013000",
    purpose: str | None = "Verwendungszweck",
    raw_reference: str | None = None,
) -> FetchedTransaction:
    """Erzeugt eine erfundene, vom Adapter gelieferte Buchung."""
    return FetchedTransaction(
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=currency,
        status=status,
        value_date=value_date,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        raw_reference=raw_reference,
    )


def make_data(
    *transactions: FetchedTransaction,
    balance: BalanceResult | None = None,
) -> SyncAccountData:
    """Baut die Abrufdaten eines Kontos fuer die Persistenz."""
    return SyncAccountData(
        account_ref="DE00370400440532013000",
        balance=balance,
        transactions=transactions,
    )


def add_stored_transaction(
    session: Session,
    account_id: int,
    *,
    status: str = "gebucht",
    booking_date: date = date(2026, 1, 10),
    amount_cents: int = -1000,
    currency: str = "EUR",
    counterparty: str | None = "Gegenpartei",
    counterparty_iban: str | None = "DE00370400440532013000",
    purpose: str | None = "Verwendungszweck",
    occurrence_index: int = 0,
    note: str | None = None,
    checked: bool = False,
    checked_at: datetime | None = None,
    reconciled: bool = False,
    reconciled_at: datetime | None = None,
    external_ref: str | None = None,
) -> Transaction:
    """Legt eine Bestandsbuchung mit gueltigem Fingerprint an."""
    fingerprint = compute_fingerprint(
        account_id=account_id,
        booking_date=booking_date,
        value_date=None,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
    )
    transaction = Transaction(
        account_id=account_id,
        status=status,
        booking_date=booking_date,
        amount_cents=amount_cents,
        currency=currency,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        origin="fints",
        fingerprint=fingerprint,
        occurrence_index=occurrence_index,
        note=note,
        checked=checked,
        checked_at=checked_at,
        reconciled=reconciled,
        reconciled_at=reconciled_at,
        external_ref=external_ref,
    )
    session.add(transaction)
    session.commit()
    return transaction


def count(session: Session, model: type[object]) -> int:
    """Zaehlt die Zeilen einer Tabelle."""
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def account_transactions(session: Session, account_id: int) -> list[Transaction]:
    """Liest alle Buchungen eines Kontos stabil sortiert."""
    return list(
        session.scalars(
            select(Transaction)
            .where(Transaction.account_id == account_id)
            .order_by(Transaction.status, Transaction.id)
        ).all()
    )


def account_runs(session: Session, account_id: int) -> list[ImportRun]:
    """Liest alle Import-Laeufe eines Kontos stabil sortiert."""
    return list(
        session.scalars(
            select(ImportRun)
            .where(ImportRun.account_id == account_id)
            .order_by(ImportRun.id)
        ).all()
    )


class TestPersistAccountSync:
    """Erfolgreiche Persistenz nach F-SYN-03, F-SYN-07 und F-DUP-02."""

    def test_schreibt_saldo_gebuchte_vormerkungen_und_genau_einen_importrun(
        self, session: Session
    ) -> None:
        """F-SYN-03/07: Saldo, Buchungen, Vormerkungen und ein ImportRun."""
        account = create_account(session)
        data = make_data(
            make_fetched(booking_date=date(2026, 1, 10), amount_cents=-1000, raw_reference="REF-1"),
            make_fetched(
                booking_date=date(2026, 1, 11),
                amount_cents=-2000,
                counterparty="Zweite",
                purpose="Zweck 2",
            ),
            make_fetched(
                booking_date=date(2026, 1, 12),
                amount_cents=-3000,
                status="vorgemerkt",
                counterparty="Dritte",
                purpose="Zweck 3",
            ),
            balance=BalanceResult(amount_cents=123_456, currency="EUR"),
        )

        run = persist_account_sync(session, account.id, data, "job-1")

        assert run.kind == "sync"
        assert run.job_id == "job-1"
        assert run.status == "erfolgreich"
        assert run.new_count == 2
        assert run.skipped_count == 0
        assert run.suspected_count == 0
        assert run.failed_count == 0
        assert run.finished_at is not None
        assert "2 neu" in (run.message or "")
        assert account_runs(session, account.id) == [run]

        refreshed = session.get(Account, account.id)
        assert refreshed is not None
        assert refreshed.balance_cents == 123_456
        assert refreshed.balance_at is not None

        transactions = account_transactions(session, account.id)
        assert len(transactions) == 3
        booked = [row for row in transactions if row.status == "gebucht"]
        pending = [row for row in transactions if row.status == "vorgemerkt"]
        assert len(booked) == 2
        assert len(pending) == 1
        assert all(row.origin == "fints" for row in transactions)
        assert booked[0].external_ref == "REF-1"

    def test_vormerkungen_werden_vor_dem_abgleich_komplett_ersetzt(self, session: Session) -> None:
        """F-DUP-04: Ein Sync ohne Vormerkungen entfernt den alten Bestand."""
        account = create_account(session)
        add_stored_transaction(
            session,
            account.id,
            status="vorgemerkt",
            booking_date=date(2026, 1, 5),
            amount_cents=-5000,
        )
        assert count(session, Transaction) == 1

        persist_account_sync(session, account.id, make_data(), "job-1")

        assert account_transactions(session, account.id) == []

    def test_verdachtsdublette_wird_importiert_und_getrennt_gezaehlt(self, session: Session) -> None:
        """F-DUP-03: Gleicher Betrag, +/- 2 Tage, abweichender Text."""
        account = create_account(session)
        add_stored_transaction(
            session,
            account.id,
            booking_date=date(2026, 1, 10),
            amount_cents=-1000,
            counterparty="Alter Text",
            purpose="Alt",
        )
        incoming = make_fetched(
            booking_date=date(2026, 1, 11),
            amount_cents=-1000,
            counterparty="Neuer Text",
            purpose="Neu",
        )

        run = persist_account_sync(session, account.id, make_data(incoming), "job-1")

        assert run.new_count == 1
        assert run.skipped_count == 0
        assert run.suspected_count == 1
        transactions = account_transactions(session, account.id)
        assert len(transactions) == 2

    def test_wiederholter_sync_mit_identischen_daten_erzeugt_keine_neue_buchung(
        self, session: Session
    ) -> None:
        """F-DUP-02: Der zweite Lauf mit denselben Daten legt nichts neu an."""
        account = create_account(session)
        data = make_data(
            make_fetched(booking_date=date(2026, 1, 10), amount_cents=-1000),
            make_fetched(
                booking_date=date(2026, 1, 11),
                amount_cents=-2000,
                counterparty="Zweite",
                purpose="Zweck 2",
            ),
            make_fetched(
                booking_date=date(2026, 1, 12),
                amount_cents=-3000,
                status="vorgemerkt",
                counterparty="Dritte",
                purpose="Zweck 3",
            ),
        )

        first = persist_account_sync(session, account.id, data, "job-1")
        second = persist_account_sync(session, account.id, data, "job-2")

        assert first.new_count == 2
        assert second.new_count == 0
        assert second.skipped_count == 2
        assert second.status == "erfolgreich"
        assert len(account_runs(session, account.id)) == 2
        transactions = account_transactions(session, account.id)
        assert len([row for row in transactions if row.status == "gebucht"]) == 2
        assert len([row for row in transactions if row.status == "vorgemerkt"]) == 1

    def test_zwei_gleiche_buchungen_werden_beide_uebernommen(self, session: Session) -> None:
        """F-DUP-02: Zwei echte gleiche Buchungen einer Lieferung bleiben beide."""
        account = create_account(session)
        identical = make_fetched(booking_date=date(2026, 1, 10), amount_cents=-1000)

        run = persist_account_sync(session, account.id, make_data(identical, identical), "job-1")

        assert run.new_count == 2
        assert len(account_transactions(session, account.id)) == 2


class TestUserFieldsUnchanged:
    """F-CON-05: Ein Sync aendert keine Nutzerfelder bestehender Buchungen."""

    def test_bestehende_buchung_bleibt_vollstaendig_unveraendert(self, session: Session) -> None:
        """Notiz, geprueft, abgestimmt und externe Referenz bleiben gleich."""
        account = create_account(session)
        stored = add_stored_transaction(
            session,
            account.id,
            booking_date=date(2026, 1, 10),
            amount_cents=-1000,
            note="Eigene Notiz",
            checked=True,
            checked_at=CHECKED_AT,
            reconciled=True,
            reconciled_at=CHECKED_AT,
            external_ref="USER-REF",
        )
        version_before = stored.version

        incoming = make_fetched(booking_date=date(2026, 1, 10), amount_cents=-1000)
        run = persist_account_sync(session, account.id, make_data(incoming), "job-1")

        unchanged = session.get(Transaction, stored.id)
        assert unchanged is not None
        assert run.new_count == 0
        assert run.skipped_count == 1
        assert unchanged.note == "Eigene Notiz"
        assert unchanged.checked is True
        assert unchanged.checked_at == CHECKED_AT
        assert unchanged.reconciled is True
        assert unchanged.reconciled_at == CHECKED_AT
        assert unchanged.external_ref == "USER-REF"
        assert unchanged.version == version_before

    def test_vormerkungs_notiz_geht_auf_genau_eine_neue_buchung_ueber(self, session: Session) -> None:
        """F-DUP-04: Die einzige Ausnahme betrifft nur die neu gebuchte Buchung."""
        account = create_account(session)
        add_stored_transaction(
            session,
            account.id,
            status="vorgemerkt",
            booking_date=date(2026, 1, 5),
            amount_cents=-5000,
            counterparty="Vermieter",
            purpose="Miete",
            note="Wichtig",
            checked=True,
            checked_at=CHECKED_AT,
        )
        incoming = make_fetched(
            booking_date=date(2026, 1, 6),
            amount_cents=-5000,
            counterparty="Vermieter",
            purpose="Miete",
        )

        run = persist_account_sync(session, account.id, make_data(incoming), "job-1")

        booked = [row for row in account_transactions(session, account.id) if row.status == "gebucht"]
        assert run.new_count == 1
        assert len(booked) == 1
        assert booked[0].note == "Wichtig"
        assert booked[0].checked is True
        assert booked[0].checked_at == CHECKED_AT
        assert not [row for row in account_transactions(session, account.id) if row.status == "vorgemerkt"]

    def test_mehrdeutige_vormerkung_bleibt_als_hinweis_im_protokoll(self, session: Session) -> None:
        """F-DUP-04: Ohne eindeutigen Kandidaten entsteht eine VORMERKUNG-Zeile."""
        account = create_account(session)
        add_stored_transaction(
            session,
            account.id,
            status="vorgemerkt",
            booking_date=date(2026, 1, 5),
            amount_cents=-5000,
            counterparty="Vermieter",
            purpose="Miete",
            note="Wichtig",
        )
        incoming = make_fetched(
            booking_date=date(2026, 1, 6),
            amount_cents=-5000,
            counterparty="Jemand anderes",
            purpose="Anderer Zweck",
        )

        run = persist_account_sync(session, account.id, make_data(incoming), "job-1")

        assert run.failed_count == 0
        assert any(error.code == "VORMERKUNG" for error in run.errors)

    def test_gebuchte_zeile_neben_vormerkung_gleichen_fingerprints_wird_angelegt(self, session: Session) -> None:
        """F-DUP-02/04: Eine Vormerkung verdeckt keine gebuchte Zeile.

        Liefert die Bank dieselbe Buchung zugleich als Vormerkung und als
        gebucht, muss die gebuchte Zeile angelegt werden. Notiz und
        Pruef-Flag der eindeutigen Vormerkung gehen auf die neue Buchung ueber.
        """
        account = create_account(session)
        add_stored_transaction(
            session,
            account.id,
            status="vorgemerkt",
            booking_date=date(2026, 1, 10),
            amount_cents=-5000,
            counterparty="Vermieter",
            purpose="Miete Januar",
            note="Wichtig",
            checked=True,
            checked_at=CHECKED_AT,
        )
        pending = make_fetched(
            booking_date=date(2026, 1, 10),
            amount_cents=-5000,
            status="vorgemerkt",
            counterparty="Vermieter",
            purpose="Miete Januar",
        )
        booked = make_fetched(
            booking_date=date(2026, 1, 10),
            amount_cents=-5000,
            counterparty="Vermieter",
            purpose="Miete Januar",
        )

        run = persist_account_sync(session, account.id, make_data(pending, booked), "job-1")

        assert run.new_count == 1
        assert run.skipped_count == 0
        assert run.suspected_count == 0
        transactions = account_transactions(session, account.id)
        booked_rows = [row for row in transactions if row.status == "gebucht"]
        pending_rows = [row for row in transactions if row.status == "vorgemerkt"]
        assert len(booked_rows) == 1
        assert len(pending_rows) == 1
        # Beide Zeilen tragen denselben F-DUP-01-Fingerprint.
        assert booked_rows[0].fingerprint == pending_rows[0].fingerprint
        assert booked_rows[0].note == "Wichtig"
        assert booked_rows[0].checked is True
        assert booked_rows[0].checked_at == CHECKED_AT
        assert not [error for error in run.errors if error.code == "VORMERKUNG"]


class TestAtomicityAndFailure:
    """F-SYN-10: Ein Konto je Transaktion, Fehlerstatus ohne Kontodaten."""

    def test_exception_laesst_aktuelles_konto_ungespeichert_und_frueheres_erhalten(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zwei Konten: Das erste bleibt, das zweite wird vollstaendig zurueckgerollt."""
        first_account = create_account(session, "Erstes Konto")
        second_account = create_account(session, "Zweites Konto")
        second_account_id = second_account.id

        persist_account_sync(
            session,
            first_account.id,
            make_data(make_fetched(booking_date=date(2026, 1, 10), amount_cents=-1000)),
            "job-1",
        )

        def failing_reconcile(
            account_id: int,
            incoming: list[object],
            session_: Session,
            *,
            include_pending_in_existing: bool = True,
        ) -> None:
            session_.add(
                Transaction(
                    account_id=account_id,
                    booking_date=date(2026, 1, 11),
                    amount_cents=-2000,
                    currency="EUR",
                    origin="fints",
                    fingerprint="0" * 64,
                    occurrence_index=0,
                )
            )
            session_.flush()
            raise RuntimeError("Simulierter Persistenzfehler")

        monkeypatch.setattr(sync_persist_module, "reconcile_multiset", failing_reconcile)

        with pytest.raises(RuntimeError):
            persist_account_sync(
                session,
                second_account_id,
                make_data(make_fetched(booking_date=date(2026, 1, 11), amount_cents=-2000)),
                "job-1",
            )

        # Erstes Konto bleibt vollstaendig gespeichert.
        assert len(account_runs(session, first_account.id)) == 1
        assert len(account_transactions(session, first_account.id)) == 1

        # Zweites Konto: keine Buchung, kein Saldo, kein ImportRun.
        assert account_transactions(session, second_account_id) == []
        assert account_runs(session, second_account_id) == []
        reloaded = session.get(Account, second_account_id)
        assert reloaded is not None
        assert reloaded.balance_cents is None

    def test_bankfehler_wird_mit_klartext_ohne_geheimnisse_protokolliert(self, session: Session) -> None:
        """F-SYN-05/07: Status fehler, Klartext aus dem Mapping, keine TAN."""
        account = create_account(session)
        seed_defaults(session)
        original_text = "Banktext mit TAN 999999 und PIN 1234"

        run = persist_sync_failure(
            session,
            account.id,
            "job-1",
            FinTSBankError("9010", original_text),
        )

        assert run.kind == "sync"
        assert run.job_id == "job-1"
        assert run.status == "fehler"
        assert run.failed_count == 1
        assert run.finished_at is not None
        assert "Verarbeitung nicht moeglich" in (run.message or "")
        assert "999999" not in (run.message or "")
        assert "1234" not in (run.message or "")
        assert len(run.errors) == 1
        assert run.errors[0].code == "9010"
        assert account_transactions(session, account.id) == []

    def test_abbruch_und_zeitueberschreitung_haben_eigene_statuswerte(self, session: Session) -> None:
        """F-SYN-07/10: Abbruch und Timeout werden getrennt protokolliert."""
        account = create_account(session)

        cancelled = persist_sync_failure(session, account.id, "job-1", SyncCancelledError())
        timed_out = persist_sync_failure(session, account.id, "job-1", TanTimeoutError())

        assert cancelled.status == "abgebrochen"
        assert cancelled.errors[0].code == "ABGEBROCHEN"
        assert timed_out.status == "zeitueberschreitung"
        assert timed_out.errors[0].code == "ZEITUEBERSCHREITUNG"
        assert account_transactions(session, account.id) == []

    def test_unbekannter_fehler_gibt_keine_meldung_aus(self, session: Session) -> None:
        """N-SEC-02: Nur der Typname, nie die moeglicherweise geheime Meldung."""
        account = create_account(session)

        run = persist_sync_failure(
            session,
            account.id,
            "job-1",
            RuntimeError("PIN 1234 TAN 5678"),
        )

        assert run.status == "fehler"
        assert "RuntimeError" in (run.message or "")
        assert "1234" not in (run.message or "")
        assert "5678" not in (run.message or "")
        assert all("5678" not in error.message for error in run.errors)


class TestComputeFetchRange:
    """F-SYN-03: Abrufzeitraum ab dem letzten erfolgreichen Sync minus 14 Tage."""

    def test_erster_sync_hat_keinen_zeitraum(self, session: Session) -> None:
        """Beim ersten Sync liefert die Bank so weit, wie sie kann."""
        account = create_account(session)

        assert compute_fetch_range(session, account.id, today=date(2026, 2, 1)) is None

    def test_letzter_erfolgreicher_sync_minus_ueberlappung(self, session: Session) -> None:
        """Der Beginn liegt 14 Tage vor dem Ende des letzten erfolgreichen Laufs."""
        account = create_account(session)
        add_sync_run(
            session,
            account.id,
            status="erfolgreich",
            finished_at=datetime(2026, 1, 20, 12, 0, tzinfo=UTC),
        )

        result = compute_fetch_range(session, account.id, today=date(2026, 2, 1))

        assert result == (date(2026, 1, 20) - timedelta(days=FETCH_OVERLAP_DAYS), date(2026, 2, 1))

    def test_spaetere_fehlgeschlagene_und_datei_laeufe_zaehlen_nicht(self, session: Session) -> None:
        """Nur kind sync mit Status erfolgreich bestimmt den Zeitraum."""
        account = create_account(session)
        add_sync_run(
            session,
            account.id,
            status="erfolgreich",
            finished_at=datetime(2026, 1, 20, 12, 0, tzinfo=UTC),
        )
        add_sync_run(
            session,
            account.id,
            status="fehler",
            finished_at=datetime(2026, 1, 25, 12, 0, tzinfo=UTC),
        )
        add_sync_run(
            session,
            account.id,
            kind="csv",
            status="erfolgreich",
            finished_at=datetime(2026, 1, 28, 12, 0, tzinfo=UTC),
        )

        result = compute_fetch_range(session, account.id, today=date(2026, 2, 1))

        assert result == (date(2026, 1, 6), date(2026, 2, 1))

    def test_neuester_erfolgreicher_sync_gewinnt(self, session: Session) -> None:
        """Bei mehreren erfolgreichen Laeufen zaehlt der neueste."""
        account = create_account(session)
        add_sync_run(
            session,
            account.id,
            status="erfolgreich",
            finished_at=datetime(2026, 1, 10, 12, 0, tzinfo=UTC),
        )
        add_sync_run(
            session,
            account.id,
            status="erfolgreich",
            finished_at=datetime(2026, 1, 24, 8, 0, tzinfo=UTC),
        )

        result = compute_fetch_range(session, account.id, today=date(2026, 2, 1))

        assert result == (date(2026, 1, 10), date(2026, 2, 1))

    def test_status_teilweise_zaehlt_nicht_als_erfolgreich(self, session: Session) -> None:
        """F-SYN-07: Nur der Kontostatus erfolgreich ist massgeblich."""
        account = create_account(session)
        add_sync_run(
            session,
            account.id,
            status="teilweise",
            finished_at=datetime(2026, 1, 20, 12, 0, tzinfo=UTC),
        )

        assert compute_fetch_range(session, account.id, today=date(2026, 2, 1)) is None


def add_sync_run(
    session: Session,
    account_id: int,
    *,
    kind: str = "sync",
    status: str = "erfolgreich",
    finished_at: datetime,
) -> ImportRun:
    """Legt einen abgeschlossenen Import-Lauf fuer Zeitraumtests an."""
    run = ImportRun(
        account_id=account_id,
        kind=kind,
        status=status,
        started_at=finished_at - timedelta(minutes=1),
        finished_at=finished_at,
    )
    session.add(run)
    session.commit()
    return run
