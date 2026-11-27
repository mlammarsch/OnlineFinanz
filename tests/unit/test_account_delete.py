"""Tests der Konto-Loeschung mit vollstaendiger Bereinigung (T-089).

Referenziert F-KTO-07 (Loeschen entfernt Buchungen, Notizen und Protokolle des
Kontos; vorheriger verlustfreier JSON-Export folgt im API-E2E T-106), F-KTO-03
(kontogebundene Importvorlagen bleiben erhalten), F-IMP-05 (Vorlage wird nur
entkoppelt), F-SYN-11 und F-KTO-07 (laufender Sync des Bankzugangs sperrt die
Loeschung mit 409), F-CON-02 (Versionspruefung, 409 mit aktuellem Stand) und
F-CON-04 (Ereignis ``account.deleted``). Die Testdaten sind erfunden (D8); es
gibt keine echten Zugangsdaten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.audit import AuditLog
from finue.models.bank import Bank, BankAccess
from finue.models.duplicate import DuplicateDecision
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.import_template import ImportTemplate
from finue.models.standing_order import StandingOrder
from finue.models.transaction import Transaction
from finue.services.accounts import delete_account

#: Tabellen, deren Gesamtzahl vor und nach der Loeschung verglichen wird.
MODELS: dict[str, type] = {
    "account": Account,
    "transaction": Transaction,
    "standing_order": StandingOrder,
    "import_run": ImportRun,
    "import_run_error": ImportRunError,
    "duplicate_decision": DuplicateDecision,
    "audit_log": AuditLog,
    "import_template": ImportTemplate,
}


def _session(client: TestClient) -> Session:
    """Oeffnet eine Session auf derselben Datenbank wie die Test-App."""

    return client.app.state.session_factory()


def _count(client: TestClient, model: type) -> int:
    """Zaehlt die Zeilen einer Tabelle ueber die App-Datenbank."""

    with _session(client) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


@dataclass(frozen=True)
class Seed:
    """IDs der beiden Testkonten aus :func:`_seed_household`."""

    target_id: int
    other_id: int


def _seed_household(client: TestClient) -> Seed:
    """Legt zwei Konten mit vollstaendigem Kindbestand an (erfundene Daten, D8).

    Das Zielkonto traegt Buchungen (gebucht mit Notiz und vorgemerkt), einen
    Auftrag, einen Import-Lauf mit Fehler, Dublettenentscheidungen, einen
    kontogebundenen Audit-Eintrag und eine Importvorlage. Das zweite Konto
    dient als Kontrolle ("andere Konten unberuehrt").
    """

    with _session(client) as session:
        target = Account(name="Zielkonto", currency="EUR", source="datei", balance_cents=1000)
        other = Account(name="Anderes Konto", currency="EUR", source="datei", balance_cents=2000)
        session.add_all([target, other])
        session.flush()

        target_booked = Transaction(
            account_id=target.id,
            status="gebucht",
            booking_date=date(2026, 1, 1),
            origin="csv",
            fingerprint="a" * 64,
            amount_cents=1000,
            note="Notiz des Zielkontos",
        )
        target_pending = Transaction(
            account_id=target.id,
            status="vorgemerkt",
            booking_date=date(2026, 1, 2),
            origin="csv",
            fingerprint="b" * 64,
            amount_cents=500,
        )
        other_first = Transaction(
            account_id=other.id,
            status="gebucht",
            booking_date=date(2026, 1, 3),
            origin="csv",
            fingerprint="c" * 64,
            amount_cents=2000,
        )
        other_second = Transaction(
            account_id=other.id,
            status="gebucht",
            booking_date=date(2026, 1, 4),
            origin="csv",
            fingerprint="d" * 64,
            amount_cents=3000,
        )
        session.add_all([target_booked, target_pending, other_first, other_second])
        session.flush()

        session.add(StandingOrder(account_id=target.id, kind="dauerauftrag"))
        session.add(StandingOrder(account_id=other.id, kind="terminauftrag"))

        target_run = ImportRun(account_id=target.id, kind="csv", status="erfolgreich")
        other_run = ImportRun(account_id=other.id, kind="csv", status="erfolgreich")
        session.add_all([target_run, other_run])
        session.flush()
        session.add(ImportRunError(run_id=target_run.id, code="E1", message="Ziel-Fehler"))
        session.add(ImportRunError(run_id=other_run.id, code="E2", message="Anderer Fehler"))

        # Dublettenentscheidungen: eine an einer Zielbuchung, eine mit einer
        # Zielbuchung als Partner, eine nur zwischen fremden Buchungen.
        session.add(DuplicateDecision(transaction_id=target_booked.id, decision="verworfen", source="ui"))
        session.add(
            DuplicateDecision(
                transaction_id=other_first.id,
                other_transaction_id=target_pending.id,
                decision="behalten",
                source="ui",
            )
        )
        session.add(
            DuplicateDecision(
                transaction_id=other_first.id,
                other_transaction_id=other_second.id,
                decision="behalten",
                source="ui",
            )
        )

        session.add(
            AuditLog(
                account_id=target.id,
                entity="transaction",
                entity_id=target_booked.id,
                action="updated",
                source="ui",
            )
        )
        session.add(AuditLog(account_id=None, entity="system", action="started", source="system"))

        session.add(ImportTemplate(name="Ziel-Vorlage", kind="csv", account_id=target.id, mapping={}))
        session.add(ImportTemplate(name="Fremde Vorlage", kind="csv", account_id=other.id, mapping={}))

        session.commit()
        return Seed(target_id=target.id, other_id=other.id)


def _fints_account(client: TestClient) -> tuple[int, int, int]:
    """Legt Bank, Bankzugang und ein FinTS-Konto an und liefert die IDs (D8)."""

    with _session(client) as session:
        bank = Bank(blz="10000000", name="Testbank")
        session.add(bank)
        session.flush()
        access = BankAccess(bank_id=bank.id, login_name="erfunden")
        session.add(access)
        session.flush()
        account = Account(name="FinTS-Konto", source="fints", bank_access_id=access.id)
        session.add(account)
        session.commit()
        return account.id, access.id, bank.id


# ------------------------------------------------------------------ F-KTO-07


def test_loeschen_entfernt_alle_kontogebundenen_zeilen(client: TestClient) -> None:
    """F-KTO-07: Loeschen entfernt Buchungen, Notizen, Vormerkungen, Protokolle und Bezuege."""

    seed = _seed_household(client)
    before = {name: _count(client, model) for name, model in MODELS.items()}

    response = client.delete(f"/api/accounts/{seed.target_id}", params={"version": 1})

    assert response.status_code == 204
    assert response.content == b""
    with _session(client) as session:
        assert session.get(Account, seed.target_id) is None
        assert _scoped_count(session, Transaction, Transaction.account_id, seed.target_id) == 0
        assert _scoped_count(session, StandingOrder, StandingOrder.account_id, seed.target_id) == 0
        assert _scoped_count(session, ImportRun, ImportRun.account_id, seed.target_id) == 0
        assert _scoped_count(session, AuditLog, AuditLog.account_id, seed.target_id) == 0
        # Keine verwaisten Fehlerzeilen des geloeschten Laufs.
        assert session.scalar(select(func.count()).select_from(ImportRunError)) == before["import_run_error"] - 1

    after = {name: _count(client, model) for name, model in MODELS.items()}
    assert after["account"] == before["account"] - 1
    assert after["transaction"] == before["transaction"] - 2  # gebucht mit Notiz + Vormerkung
    assert after["standing_order"] == before["standing_order"] - 1
    assert after["import_run"] == before["import_run"] - 1
    assert after["import_run_error"] == before["import_run_error"] - 1
    assert after["duplicate_decision"] == before["duplicate_decision"] - 2
    assert after["audit_log"] == before["audit_log"] - 1  # nur der kontogebundene Eintrag
    assert after["import_template"] == before["import_template"]  # Vorlagen bleiben


def test_andere_konten_unberuehrt(client: TestClient) -> None:
    """F-KTO-07: Konten ohne Bezug zum geloeschten Konto bleiben samt Kindern erhalten."""

    seed = _seed_household(client)
    client.delete(f"/api/accounts/{seed.target_id}", params={"version": 1})

    with _session(client) as session:
        assert session.get(Account, seed.other_id) is not None
        assert _scoped_count(session, Transaction, Transaction.account_id, seed.other_id) == 2
        assert _scoped_count(session, StandingOrder, StandingOrder.account_id, seed.other_id) == 1
        assert _scoped_count(session, ImportRun, ImportRun.account_id, seed.other_id) == 1
        # Die paarfremde Entscheidung und der globale Audit-Eintrag bleiben.
        assert session.scalar(select(func.count()).select_from(DuplicateDecision)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.account_id.is_(None))) == 1


def test_importvorlage_wird_entkoppelt_nicht_geloescht(client: TestClient) -> None:
    """F-KTO-03/F-IMP-05: Kontogebundene Vorlagen bleiben erhalten, nur ohne Zuordnung."""

    seed = _seed_household(client)
    client.delete(f"/api/accounts/{seed.target_id}", params={"version": 1})

    with _session(client) as session:
        templates = {template.name: template for template in session.scalars(select(ImportTemplate))}

    assert set(templates) == {"Ziel-Vorlage", "Fremde Vorlage"}
    assert templates["Ziel-Vorlage"].account_id is None
    assert templates["Fremde Vorlage"].account_id == seed.other_id


def test_archiviertes_konto_loeschbar(client: TestClient) -> None:
    """F-KTO-07: Auch archivierte Konten lassen sich endgueltig loeschen."""

    with _session(client) as session:
        account = Account(name="Archiv", currency="EUR", source="datei", active=False)
        session.add(account)
        session.commit()
        account_id = account.id

    assert client.delete(f"/api/accounts/{account_id}", params={"version": 1}).status_code == 204
    assert client.get(f"/api/accounts/{account_id}").status_code == 404


# --------------------------------------------------------- Bankzugang (F-KTO-07)


def test_bankzugang_bleibt_erhalten(client: TestClient) -> None:
    """F-KTO-07: Loeschen entfernt kein Zugangsdatum; der Zugang bleibt auch als letzter."""

    account_id, access_id, bank_id = _fints_account(client)

    response = client.delete(f"/api/accounts/{account_id}", params={"version": 1})

    assert response.status_code == 204
    with _session(client) as session:
        assert session.get(Account, account_id) is None
        assert session.get(BankAccess, access_id) is not None  # kein stilles Loeschen der Zugangsdaten
        assert session.get(Bank, bank_id) is not None


def test_laufender_sync_verhindert_loeschung(client: TestClient) -> None:
    """F-SYN-11/F-KTO-07: Ein laufender Sync des Zugangs liefert 409 mit Klartext."""

    account_id, access_id, _bank_id = _fints_account(client)
    registry = client.app.state.services.sync_registry
    job = registry.start_or_get(access_id, [account_id])
    try:
        response = client.delete(f"/api/accounts/{account_id}", params={"version": 1})
    finally:
        registry.release(job.id)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert "Sync" in body["message"]
    with _session(client) as session:
        assert session.get(Account, account_id) is not None
        assert session.get(BankAccess, access_id) is not None


# ------------------------------------------------------- Versionspruefung (F-CON-02)


def test_versionskonflikt_loescht_nichts(client: TestClient) -> None:
    """F-CON-02: Ein veralteter Stand liefert 409 mit dem aktuellen Stand und loescht nichts."""

    seed = _seed_household(client)

    response = client.delete(f"/api/accounts/{seed.target_id}", params={"version": 99})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "conflict"
    assert body["current"]["id"] == seed.target_id
    assert body["current"]["version"] == 1
    with _session(client) as session:
        assert session.get(Account, seed.target_id) is not None
        assert _scoped_count(session, Transaction, Transaction.account_id, seed.target_id) == 2


def test_unbekanntes_konto_404(client: TestClient) -> None:
    """F-KTO-07: Ein unbekanntes Konto liefert 404 im Fehlerformat."""

    response = client.delete("/api/accounts/9999", params={"version": 1})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_unzulaessige_oder_fehlende_version_422(client: TestClient) -> None:
    """F-CON-02: ``version`` muss eine positive Ganzzahl sein und mitgesendet werden."""

    seed = _seed_household(client)

    assert client.delete(f"/api/accounts/{seed.target_id}", params={"version": 0}).status_code == 422
    assert client.delete(f"/api/accounts/{seed.target_id}").status_code == 422
    with _session(client) as session:
        assert session.get(Account, seed.target_id) is not None


# ------------------------------------------------------------------ F-CON-04


def test_ereignis_account_deleted(client: TestClient) -> None:
    """F-CON-04: Die Loeschung veroeffentlicht ``account.deleted`` mit der Konto-ID."""

    seed = _seed_household(client)
    events: list[object] = []
    client.app.state.services.event_bus.subscribe(events.append)

    client.delete(f"/api/accounts/{seed.target_id}", params={"version": 1})

    types = [getattr(event, "type", None) for event in events]
    assert types == ["account.deleted"]
    assert getattr(events[0], "get")("account") == seed.target_id


# ------------------------------------------------------- Service-Rueckmeldung


def test_deletion_summary_zaehlt_zeilen(client: TestClient) -> None:
    """F-KTO-07: Der Service meldet die Zeilenzahlen je bereinigter Tabelle."""

    seed = _seed_household(client)

    with _session(client) as session:
        summary = delete_account(session, seed.target_id, expected_version=1)

    assert summary.transactions == 2
    assert summary.standing_orders == 1
    assert summary.import_runs == 1
    assert summary.import_run_errors == 1
    assert summary.duplicate_decisions == 2
    assert summary.audit_entries == 1
    assert summary.import_templates_unlinked == 1


def _scoped_count(session: Session, model: type, column: object, value: int) -> int:
    """Zaehlt Zeilen einer Tabelle mit ``column == value``."""

    return int(session.scalar(select(func.count()).select_from(model).where(column == value)) or 0)
