"""Tests fuer die FinTS-Ergebnis- und Fehlerobjekte aus ``fints_types``.

Referenziert F-SYN-03 (Saldo, gebuchte/vorgemerkte Umsaetze, Auftraege),
F-SYN-05 (Bankcode plus Originaltext), F-AUF-05 (available/empty/unsupported),
F-AUF-02/F-AUF-04 (Auftragsfelder und Ersetzen), N-SEC-02 (keine PIN/TAN in
Fehlertexten), N-DB-02 (Betraege in ganzen Cent) und D2 (nur lesende
Operationen). Alle Testdaten sind erfunden (D8).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from typing import Any, cast

import pytest

from finue.services.fints_types import (
    READ_ONLY_OPERATIONS,
    AccessLockedError,
    BalanceResult,
    FetchedStandingOrder,
    FetchedTransaction,
    FinTSBankError,
    OrdersResult,
    PinWrongError,
    SyncAccountData,
    SyncCancelledError,
    TanChallenge,
    TanTimeoutError,
)

# Nur Testwerte; sie duerfen in keinem Fehlertext auftauchen (N-SEC-02).
SECRET_PIN = "987654"
SECRET_TAN = "432100"

FAKE_IBAN = "DE02120300000000202051"
BOOKING_DATE = date(2026, 1, 15)


def _booked_transaction() -> FetchedTransaction:
    """Gebuchte Beispielbuchung mit allen Feldern."""
    return FetchedTransaction(
        booking_date=BOOKING_DATE,
        amount_cents=-4883,
        currency="EUR",
        status="gebucht",
        value_date=date(2026, 1, 16),
        counterparty="Beispiel Markt",
        counterparty_iban=FAKE_IBAN,
        purpose="Einkauf Beispiel",
        raw_reference="REF-4711",
    )


def _standing_order() -> FetchedStandingOrder:
    """Beispiel-Dauerauftrag mit allen Terminen."""
    return FetchedStandingOrder(
        kind="dauerauftrag",
        counterparty="Beispiel Vermieter",
        counterparty_iban=FAKE_IBAN,
        purpose="Miete",
        amount_cents=95000,
        currency="EUR",
        interval="monatlich",
        next_execution=date(2026, 2, 1),
        first_execution=date(2025, 1, 1),
        last_execution=None,
    )


def test_balance_result_ist_unveraenderlich() -> None:
    """F-SYN-03, N-DB-02: Saldo traegt Cent-Betrag, Waehrung und optionalen Stand."""
    balance = BalanceResult(amount_cents=123456, currency="EUR", as_of=date(2026, 1, 31))

    assert balance.amount_cents == 123456
    assert balance.currency == "EUR"
    assert balance.as_of == date(2026, 1, 31)

    with pytest.raises(FrozenInstanceError):
        balance.amount_cents = 0


def test_fetched_transaction_bildet_alle_felder_ab() -> None:
    """F-SYN-03: Buchungstag, Wertstellung, Cent-Betrag, Waehrung, Gegenpartei, IBAN, Zweck, Status, Rohreferenz."""
    transaction = _booked_transaction()

    assert transaction.booking_date == BOOKING_DATE
    assert transaction.value_date == date(2026, 1, 16)
    assert transaction.amount_cents == -4883
    assert transaction.currency == "EUR"
    assert transaction.counterparty == "Beispiel Markt"
    assert transaction.counterparty_iban == FAKE_IBAN
    assert transaction.purpose == "Einkauf Beispiel"
    assert transaction.status == "gebucht"
    assert transaction.raw_reference == "REF-4711"


def test_fetched_transaction_ist_unveraenderlich() -> None:
    """F-SYN-03: Ergebnisobjekte duerfen nach der Uebergabe nicht mehr kippen."""
    transaction = _booked_transaction()

    with pytest.raises(FrozenInstanceError):
        transaction.purpose = "geaendert"


def test_fetched_transaction_lehnt_unbekannten_status_ab() -> None:
    """F-SYN-03: Nur gebucht oder vorgemerkt sind zulaessig."""
    with pytest.raises(ValueError, match="Buchungsstatus"):
        FetchedTransaction(
            booking_date=BOOKING_DATE,
            amount_cents=100,
            currency="EUR",
            status=cast(Any, "offen"),
        )


@pytest.mark.parametrize("status", ["gebucht", "vorgemerkt"])
def test_fetched_transaction_akzeptiert_beide_statuswerte(status: Any) -> None:
    """F-SYN-03: Gebuchte und vorgemerkte Umsaetze werden getrennt gekennzeichnet."""
    transaction = FetchedTransaction(
        booking_date=BOOKING_DATE,
        amount_cents=100,
        currency="EUR",
        status=status,
    )

    assert transaction.status == status


def test_fetched_standing_order_bildet_auftragsfelder_ab() -> None:
    """F-AUF-02, F-AUF-04: Auftrag traegt Empfaenger, IBAN, Zweck, Betrag, Art, Intervall und Termine."""
    order = _standing_order()

    assert order.kind == "dauerauftrag"
    assert order.counterparty == "Beispiel Vermieter"
    assert order.counterparty_iban == FAKE_IBAN
    assert order.purpose == "Miete"
    assert order.amount_cents == 95000
    assert order.currency == "EUR"
    assert order.interval == "monatlich"
    assert order.next_execution == date(2026, 2, 1)
    assert order.first_execution == date(2025, 1, 1)
    assert order.last_execution is None

    with pytest.raises(FrozenInstanceError):
        order.interval = "jaehrlich"


def test_fetched_standing_order_lehnt_unbekannte_art_ab() -> None:
    """F-AUF-02: Nur Dauer- und Terminauftrag sind zulaessig."""
    with pytest.raises(ValueError, match="Auftragsart"):
        FetchedStandingOrder(kind=cast(Any, "ueberweisung"))


def test_orders_result_traegt_auftraege_und_tupel() -> None:
    """F-AUF-05: available liefert die Auftraege als unveraenderliches Tupel."""
    order = _standing_order()
    result = OrdersResult(status="available", orders=(order,))

    assert result.status == "available"
    assert result.orders == (order,)
    assert isinstance(result.orders, tuple)

    with pytest.raises(FrozenInstanceError):
        result.status = "empty"


def test_orders_result_bildet_leer_und_nicht_unterstuetzt_ab() -> None:
    """F-AUF-05: Leere Antwort und fehlende Unterstuetzung sind erfolgreiche Zustaende."""
    assert OrdersResult(status="empty").orders == ()
    assert OrdersResult(status="unsupported").orders == ()


def test_orders_result_erzwingt_konsistente_zustaende() -> None:
    """F-AUF-05: Nur available darf Auftraege tragen, available braucht mindestens einen."""
    order = _standing_order()

    with pytest.raises(ValueError, match="Auftragsstatus"):
        OrdersResult(status=cast(Any, "teilweise"))

    with pytest.raises(ValueError, match="available"):
        OrdersResult(status="available")

    with pytest.raises(ValueError, match="available"):
        OrdersResult(status="empty", orders=(order,))

    with pytest.raises(ValueError, match="available"):
        OrdersResult(status="unsupported", orders=(order,))


@pytest.mark.parametrize("kind", ["text", "app"])
def test_tan_challenge_bildet_text_und_app_ab(kind: Any) -> None:
    """F-SYN-04, D3: TAN-Art text/app mit anzuzeigendem Challenge-Text."""
    challenge = TanChallenge(kind=kind, challenge="Bitte in der App bestaetigen")

    assert challenge.kind == kind
    assert challenge.challenge == "Bitte in der App bestaetigen"

    with pytest.raises(FrozenInstanceError):
        challenge.challenge = "geaendert"


def test_tan_challenge_lehnt_unbekannte_art_ab() -> None:
    """F-SYN-04: Nur text und app sind zulaessig."""
    with pytest.raises(ValueError, match="TAN-Art"):
        TanChallenge(kind=cast(Any, "sms"), challenge="Code eingeben")


def test_sync_account_data_ist_tief_unveraenderlich() -> None:
    """F-SYN-03: Kontodaten buendeln Saldo, Umsaetze und Auftraege ohne veraenderbare Listen."""
    transaction = _booked_transaction()
    orders = OrdersResult(status="available", orders=(_standing_order(),))
    data = SyncAccountData(
        account_ref=FAKE_IBAN,
        balance=BalanceResult(amount_cents=250000, currency="EUR"),
        transactions=(transaction,),
        orders=orders,
    )

    assert data.account_ref == FAKE_IBAN
    assert data.balance is not None
    assert data.balance.amount_cents == 250000
    assert data.transactions == (transaction,)
    assert isinstance(data.transactions, tuple)
    assert data.orders is orders
    assert isinstance(data.orders.orders, tuple)

    with pytest.raises(FrozenInstanceError):
        data.account_ref = "DE00000000000000000000"

    with pytest.raises(AttributeError):
        data.transactions.append(transaction)


def test_read_only_operations_whitelist_enthaelt_nur_lesende_operationen() -> None:
    """D2: Whitelist enthaelt genau die lesenden Abrufe und keine Schreiboperation."""
    expected = {
        "get_sepa_accounts",
        "get_balance",
        "get_transactions_xml",
        "get_transactions",
        "get_information",
        "get_tan_mechanisms",
    }
    forbidden = {"sepa_transfer", "simple_sepa_transfer", "sepa_debit", "simple_sepa_debit", "send_tan"}

    assert isinstance(READ_ONLY_OPERATIONS, frozenset)
    assert READ_ONLY_OPERATIONS == expected
    assert not READ_ONLY_OPERATIONS & forbidden

    with pytest.raises(AttributeError):
        READ_ONLY_OPERATIONS.add("sepa_transfer")


@pytest.mark.parametrize(
    "error",
    [
        FinTSBankError("9999", "Originaltext der Bank"),
        PinWrongError("9340", "PIN falsch"),
        AccessLockedError("3938", "Zugang gesperrt"),
    ],
)
def test_bankfehler_haelt_code_und_originaltext_fuer_das_mapping_bereit(error: FinTSBankError) -> None:
    """F-SYN-05: Code und Originaltext bleiben als Attribute fuer die Mapping-Schicht erhalten."""
    assert isinstance(error, FinTSBankError)
    assert isinstance(error, Exception)
    assert error.code in {"9999", "9340", "3938"}
    assert error.original_text in {"Originaltext der Bank", "PIN falsch", "Zugang gesperrt"}


def test_pin_und_sperre_sind_spezialisierte_bankfehler() -> None:
    """F-SYN-05: Falsche PIN und gesperrter Zugang erben vom allgemeinen Bankfehler."""
    assert issubclass(PinWrongError, FinTSBankError)
    assert issubclass(AccessLockedError, FinTSBankError)
    assert "PIN" in str(PinWrongError("9340", "egal"))
    assert "gesperrt" in str(AccessLockedError("3938", "egal"))


def test_tan_timeout_und_abbruch_sind_klartextfehler() -> None:
    """F-SYN-04, F-SYN-10: Timeout und Nutzerabbruch haben eigene, klare Fehler."""
    assert not issubclass(TanTimeoutError, FinTSBankError)
    assert not issubclass(SyncCancelledError, FinTSBankError)
    assert "TAN" in str(TanTimeoutError())
    assert "abgebrochen" in str(SyncCancelledError())


@pytest.mark.parametrize(
    "error",
    [
        FinTSBankError(SECRET_PIN, f"TAN {SECRET_TAN} abgelehnt"),
        PinWrongError(SECRET_PIN, f"TAN {SECRET_TAN} abgelehnt"),
        AccessLockedError(SECRET_PIN, f"TAN {SECRET_TAN} abgelehnt"),
        TanTimeoutError(),
        SyncCancelledError(),
    ],
)
def test_fehlertexte_spiegeln_keine_eingabedaten(error: Exception) -> None:
    """N-SEC-02: str, repr und args enthalten keine PIN und keine TAN."""
    rendered = f"{error!s} {error!r} {error.args!r}"

    assert SECRET_PIN not in rendered
    assert SECRET_TAN not in rendered
    assert str(error)
    assert repr(error).startswith(type(error).__name__)


def test_bankfehler_repr_enthaelt_nur_klassennamen() -> None:
    """N-SEC-02: Auch ein als Code uebergebenes Geheimnis bleibt aus repr heraus."""
    error = FinTSBankError(SECRET_PIN, f"TAN {SECRET_TAN}")

    assert repr(error) == "FinTSBankError()"
    assert str(error) == "Die Bank hat den FinTS-Auftrag abgelehnt."
