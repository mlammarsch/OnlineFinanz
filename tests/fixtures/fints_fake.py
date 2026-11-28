"""Skriptgesteuerter Fake der python-fints-Klassen fuer alle FinTS-Tests (D8).

Der Fake arbeitet auf der Ebene von :class:`fints.client.FinTS3PinTanClient`
und liefert dieselben Methodennamen und Rueckgabetypen wie die in T-060
gegen python-fints 5.0.1 belegte Bibliothek:

- ``get_sepa_accounts`` -> ``list[fints.models.SEPAAccount]``
- ``get_balance`` -> ``mt940.models.Balance``
- ``get_transactions_xml`` -> ``(booked_streams, pending_streams)``, je eine
  Liste von camt-XML-Bytestrings
- ``get_transactions`` -> ``list[mt940.models.Transaction]``, mit
  ``include_pending=True`` zusaetzlich die Vormerkungen
- ``pause_dialog``/``resume_dialog``/``deconstruct``/``send_tan`` wie bei
  TAN- und App-Freigabe-Fluessen, inklusive ``NeedTANResponse``

Der Fake oeffnet nie eine Verbindung: alle Antworten stammen aus einem
:class:`FakeScript`. Er protokolliert jeden Methodenaufruf in
:attr:`FakeFinTS3PinTanClient.method_log`, damit Tests beweisen koennen, dass
nur lesende Operationen aufgerufen werden. Schreibende Methoden
(``sepa_transfer``, ``simple_sepa_transfer``, ``sepa_debit``) existieren
bewusst nicht.

Alle Daten sind erfunden: IBANs beginnen mit ``DE00`` (ungueltige Pruefsumme),
Namen sind frei erfunden, PIN und Produkt-ID sind Platzhalter. PIN und TAN
erscheinen weder im Protokoll noch in ``repr`` oder ``deconstruct``
(N-SEC-02); offene Dialogdaten bleiben ausschliesslich im Arbeitsspeicher
(N-SEC-03).

Abgedeckte Skripte (F-SYN-01..06, F-SYN-10, F-AUF-05):

- ``happy_path``: Konten, Saldo, camt und MT940 mit Vormerkung
- ``tan_text``: textuelle TAN mit ``NeedTANResponse``
- ``tan_app``: App-Freigabe mit zwei ``NeedTANResponse``, dann Erfolg
- ``ing_90_days``: ING 90-Tage-Regel als App-Freigabe
- ``consors_9800`` und ``consors_9010``: Consorsbank-Bankcodes 9800 und 9010
- ``wrong_pin``: falsche PIN (Code 9942)
- ``locked``: gesperrter Zugang (Code 3938)
- ``balance_only``: Saldo ohne Umsaetze
- ``camt_only``: camt mit gebuchten und vorgemerkten Umsaetzen
- ``mt940_pending``: MT940 mit Vormerkungen
- ``orders_present``, ``orders_empty``, ``orders_unsupported``: lesender
  Auftragsbestand als HICDB/HICSB-Antwort (T-075)
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any, Final, Literal
from xml.sax.saxutils import escape

import mt940
from fints.client import NeedTANResponse
from fints.exceptions import (
    FinTSClientError,
    FinTSClientPINError,
    FinTSClientTemporaryAuthError,
    FinTSUnsupportedOperation,
)
from fints.models import SEPAAccount

from finue.services.fints_orders import (
    HICDB1,
    HICSB1,
    StandingOrderDetails3,
    StandingOrderTimeUnit1,
    VirtualDate,
)

__all__ = [
    "DIALOG_OPERATIONS",
    "FAKE_BANK_IDENTIFIER",
    "FAKE_BIC",
    "FAKE_IBAN_CARD",
    "FAKE_IBAN_GIRO",
    "FAKE_PIN",
    "FAKE_PRODUCT_ID",
    "FAKE_READ_OPERATIONS",
    "FAKE_SERVER",
    "FAKE_USER_ID",
    "SCRIPT_FACTORIES",
    "WRITE_OPERATIONS",
    "FakeBankError",
    "FakeCamtEntry",
    "FakeAccountInfo",
    "FakeFinTS3PinTanClient",
    "FakePin",
    "FakeScript",
    "FakeStandingOrder",
    "FakeTanChallenge",
    "FakeTanFlow",
    "FakeTanMechanism",
    "fake_accounts",
    "fake_balance",
    "fake_camt_xml",
    "fake_transaction",
    "script_balance_only",
    "script_camt_only",
    "script_consors_9010",
    "script_consors_9800",
    "script_happy_path",
    "script_ing_90_days",
    "script_locked",
    "script_mt940_pending",
    "script_orders_empty",
    "script_orders_present",
    "script_orders_unsupported",
    "script_tan_app",
    "script_tan_text",
    "script_wrong_pin",
]

FAKE_BANK_IDENTIFIER: Final = "12345678"
FAKE_USER_ID: Final = "erfundener-nutzer"
FAKE_PIN: Final = "00000"
FAKE_SERVER: Final = "https://example.invalid/fints"
FAKE_PRODUCT_ID: Final = "0000000000000000000000000"
FAKE_IBAN_GIRO: Final = "DE00100000001234567890"
FAKE_IBAN_CARD: Final = "DE00999900009876543210"
FAKE_BIC: Final = "ABCDDEFFXXX"

# Lesende python-fints-Operationen. Der Selbsttest vergleicht diese Menge mit
# der Whitelist ``READ_ONLY_OPERATIONS`` aus der Produkt-Schicht.
FAKE_READ_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "get_sepa_accounts",
        "get_balance",
        "get_transactions_xml",
        "get_transactions",
        # Anlage-Assistent (F-KTO-05a): Bank-/Kontoinformationen und TAN-Verfahren.
        "get_information",
        "get_tan_mechanisms",
    }
)

# Dialog- und TAN-Methoden, die kein Bankgeschaeft ausloesen.
DIALOG_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "pause_dialog",
        "resume_dialog",
        "send_tan",
        "deconstruct",
    }
)

# Schreibende Geschaeftsvorfaelle der Bibliothek. Der Fake bietet sie nicht an
# und protokolliert sie nie; der Selbsttest prueft beides.
WRITE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "sepa_transfer",
        "simple_sepa_transfer",
        "sepa_debit",
    }
)

OrderState = Literal["available", "empty", "unsupported"]

_DIALOG_MAGIC: Final = b"fake-dialog-state-"
_CLIENT_MAGIC: Final = b"fake-fints-client-v1:"

_CAMT_NAMESPACE: Final = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"


class FakePin:
    """Testdouble fuer ``fints.client.Pin``; gibt die PIN nie aus (N-SEC-02)."""

    def __init__(self, pin: str) -> None:
        self._pin = pin
        self.blocked = False

    def block(self) -> None:
        """Sperrt die PIN wie die Bibliothek nach einem PIN- oder Sperrfehler."""
        self.blocked = True

    def __repr__(self) -> str:
        return f"FakePin(blocked={self.blocked!r})"


@dataclass(frozen=True, slots=True)
class FakeTanChallenge:
    """Eine TAN- oder App-Freigabe-Anforderung im Skript (F-SYN-04)."""

    text: str
    decoupled: bool = False


@dataclass(frozen=True, slots=True)
class FakeTanFlow:
    """Mehrere Challenges mit anschliessendem Erfolg (D3, F-SYN-04).

    Die erste Challenge wird aus der lesenden Operation zurueckgegeben, jede
    weitere aus ``send_tan``. Erst nach der letzten Challenge liefert
    ``send_tan`` das gespeicherte Ergebnis.
    """

    challenges: tuple[FakeTanChallenge, ...]
    result: Any

    def __post_init__(self) -> None:
        if not self.challenges:
            raise ValueError("Ein TAN-Flow braucht mindestens eine Challenge")


@dataclass(frozen=True, slots=True)
class FakeBankError:
    """Ein von der Bank gemeldeter Fehler als Skript-Antwort (F-SYN-05).

    ``exception_type`` ist die echte python-fints-Klasse. Zusaetzlich traegt
    die Instanz ``bank_code`` und ``original_text``, damit Tests und Adapter
    den Bankcode pruefen koennen, ohne die Bibliotheksmeldung zu parsen.
    """

    exception_type: type[FinTSClientError]
    code: str
    text: str


@dataclass(frozen=True, slots=True)
class FakeStandingOrder:
    """Erfundener Dauer- oder Terminauftrag als reine Skript-Option (F-AUF-05)."""

    kind: Literal["dauerauftrag", "terminauftrag"]
    counterparty: str
    counterparty_iban: str
    purpose: str
    amount: str
    interval: str | None = None
    next_execution: date | None = None


@dataclass(frozen=True, slots=True)
class FakeCamtEntry:
    """Eine camt.053-Buchung mit erfundenen Daten (F-SYN-03)."""

    amount: str
    direction: Literal["CRDT", "DBIT"]
    booking_date: date
    value_date: date
    counterparty: str
    counterparty_iban: str
    purpose: str
    reference: str
    end_to_end: str


@dataclass(frozen=True, slots=True)
class FakeAccountInfo:
    """Erfundene Kontoinformation (HIUPD) fuer den Anlage-Assistenten (F-KTO-05a)."""

    iban: str
    account_type: str
    product_name: str | None = None
    owner_name: str | None = None
    currency: str = "EUR"


@dataclass(frozen=True, slots=True)
class FakeTanMechanism:
    """Erfundenes TAN-Verfahren (HITANS) fuer den Assistenten (F-KTO-05a)."""

    security_function: str
    name: str
    decoupled: bool = False


@dataclass(frozen=True, slots=True)
class FakeScript:
    """Vollstaendiges Dialogskript fuer einen Fake-Client.

    ``balance``, ``transactions`` werden pro Aufruf der Reihe nach
    verbraucht; ein Eintrag ist ein Ergebnis, ein :class:`FakeTanFlow` oder
    ein :class:`FakeBankError`. Ohne Eintrag liefert ``get_transactions`` eine
    leere Liste (Saldo ohne Umsaetze). camt-Streams und Vormerkungen sind
    direkte Antwortdaten, weil die Bibliothek sie ohne TAN liefert.

    ``bank_name``, ``account_information`` und ``tan_mechanisms`` bedienen den
    rein lesenden Anlage-Assistenten (``get_information``/``get_tan_mechanisms``,
    F-KTO-05a).
    """

    name: str
    accounts: tuple[SEPAAccount, ...]
    balance: tuple[Any, ...] = ()
    transactions: tuple[Any, ...] = ()
    transactions_pending: tuple[mt940.models.Transaction, ...] = ()
    camt_booked: tuple[bytes, ...] = ()
    camt_pending: tuple[bytes, ...] = ()
    orders_state: OrderState = "unsupported"
    orders: tuple[FakeStandingOrder, ...] = ()
    accounts_outcome: tuple[Any, ...] = ()
    bank_name: str | None = None
    account_information: tuple[FakeAccountInfo, ...] = ()
    tan_mechanisms: tuple[FakeTanMechanism, ...] = ()

    def __post_init__(self) -> None:
        if self.orders_state == "available" and not self.orders:
            raise ValueError("Status 'available' erfordert mindestens einen Auftrag")
        if self.orders_state != "available" and self.orders:
            raise ValueError("Nur Status 'available' darf Auftraege tragen")


def fake_accounts() -> tuple[SEPAAccount, ...]:
    """Zwei erfundene Konten mit ungueltiger Test-IBAN (D8)."""
    return (
        SEPAAccount(
            iban=FAKE_IBAN_GIRO,
            bic=FAKE_BIC,
            accountnumber="1234567890",
            subaccount="",
            blz="10000000",
        ),
        SEPAAccount(
            iban=FAKE_IBAN_CARD,
            bic=FAKE_BIC,
            accountnumber="0987654321",
            subaccount="",
            blz="99990000",
        ),
    )


def fake_balance(
    amount: str = "1234,56",
    *,
    status: Literal["C", "D"] = "C",
    as_of: date = date(2026, 11, 25),
) -> mt940.models.Balance:
    """Saldo im Rueckgabetyp der Bibliothek (F-SYN-03, N-DB-02)."""
    balance_date = mt940.models.Date(as_of.year, as_of.month, as_of.day)
    return mt940.models.Balance(status, amount, balance_date, currency="EUR")


def fake_transaction(
    *,
    counterparty: str,
    counterparty_iban: str,
    purpose: str,
    amount: str = "42,00",
    status: Literal["C", "D"] = "D",
    booking_date: date = date(2026, 11, 20),
    reference: str = "FAKE-REF-1",
    pending: bool = False,
) -> mt940.models.Transaction:
    """MT940-Buchung als ``mt940.models.Transaction`` (F-SYN-03).

    Vormerkungen haben wie bei MT940 ueblich keinen Buchungstag
    (``entry_date``); der Adapter leitet daraus den Status ab.
    """
    data: dict[str, Any] = {
        "amount": mt940.models.Amount(amount, status, "EUR"),
        "date": booking_date,
        "status": status,
        "purpose": purpose,
        "applicant_name": counterparty,
        "applicant_iban": counterparty_iban,
        "id": reference,
    }
    if not pending:
        data["entry_date"] = booking_date
        data["guessed_entry_date"] = booking_date
    return mt940.models.Transaction(mt940.models.Transactions(), data)


def _camt_counterparty(entry: FakeCamtEntry) -> str:
    """Gegenpartei im camt-Pfad, den der Bibliotheksparser je Richtung liest."""
    if entry.direction == "DBIT":
        tag = "Cdtr"
    else:
        tag = "Dbtr"
    return (
        f"<{tag}><Pty><Nm>{escape(entry.counterparty)}</Nm></Pty></{tag}>"
        f"<{tag}Acct><Id><IBAN>{escape(entry.counterparty_iban)}</IBAN></Id></{tag}Acct>"
    )


def fake_camt_xml(
    entries: Sequence[FakeCamtEntry],
    *,
    iban: str = FAKE_IBAN_GIRO,
) -> bytes:
    """Baut ein camt.053-XML, das ``fints.client.camt053_to_dict`` parst.

    Der Parser erwartet die pyiso20022-Form ``Rpt/Acct/Ccy`` und ``Ntry``.
    Alle Inhalte sind erfunden; ``escape`` schuetzt vor XML-Sonderzeichen.
    """
    rows = "".join(
        (
            "<Ntry>"
            f'<Amt Ccy="EUR">{escape(entry.amount)}</Amt>'
            f"<CdtDbtInd>{entry.direction}</CdtDbtInd>"
            f"<BookgDt><Dt>{entry.booking_date.isoformat()}</Dt></BookgDt>"
            f"<ValDt><Dt>{entry.value_date.isoformat()}</Dt></ValDt>"
            f"<AcctSvcrRef>{escape(entry.reference)}</AcctSvcrRef>"
            f"<BkTxCd><Prtry><Cd>{escape(entry.reference)}</Cd></Prtry></BkTxCd>"
            "<NtryDtls><TxDtls>"
            f"<Refs><EndToEndId>{escape(entry.end_to_end)}</EndToEndId></Refs>"
            "<RltdPties>"
            f"{_camt_counterparty(entry)}"
            "</RltdPties>"
            f"<RmtInf><Ustrd>{escape(entry.purpose)}</Ustrd></RmtInf>"
            "</TxDtls></NtryDtls>"
            "</Ntry>"
        )
        for entry in entries
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Document xmlns="{_CAMT_NAMESPACE}">'
        "<Rpt>"
        f"<Acct><Id><IBAN>{escape(iban)}</IBAN></Id><Ccy>EUR</Ccy></Acct>"
        f"{rows}"
        "</Rpt>"
        "</Document>"
    )
    return document.encode("utf-8")


def _camt_entry(
    *,
    amount: str,
    direction: Literal["CRDT", "DBIT"],
    purpose: str,
    counterparty: str,
    reference: str,
    booking_date: date = date(2026, 11, 20),
    value_date: date = date(2026, 11, 20),
    counterparty_iban: str = FAKE_IBAN_CARD,
) -> FakeCamtEntry:
    return FakeCamtEntry(
        amount=amount,
        direction=direction,
        booking_date=booking_date,
        value_date=value_date,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        purpose=purpose,
        reference=reference,
        end_to_end=f"{reference}-E2E",
    )


def _happy_booked_transactions() -> tuple[Any, ...]:
    return (
        [
            fake_transaction(
                counterparty="Erfundene Beispiel GmbH",
                counterparty_iban=FAKE_IBAN_CARD,
                purpose="Erfundene Rechnung 2026-001",
                amount="42,00",
                status="D",
                reference="FAKE-REF-1",
            ),
            fake_transaction(
                counterparty="Beispiel Person",
                counterparty_iban="DE00777700001111222233",
                purpose="Erfundene Erstattung",
                amount="15,50",
                status="C",
                reference="FAKE-REF-2",
                booking_date=date(2026, 11, 21),
            ),
        ],
    )


def script_happy_path() -> FakeScript:
    """Vollstaendiger Erfolgsfall mit Saldo, camt und MT940."""
    return FakeScript(
        name="happy_path",
        accounts=fake_accounts(),
        balance=(fake_balance(),),
        transactions=_happy_booked_transactions(),
        transactions_pending=(
            fake_transaction(
                counterparty="Beispiel Person",
                counterparty_iban="DE00777700001111222233",
                purpose="Erfundene Vormerkung",
                amount="9,99",
                status="D",
                reference="FAKE-PENDING-1",
                pending=True,
            ),
        ),
        camt_booked=(
            fake_camt_xml(
                (
                    _camt_entry(
                        amount="42.00",
                        direction="DBIT",
                        purpose="Erfundene Rechnung 2026-001",
                        counterparty="Erfundene Beispiel GmbH",
                        reference="FAKE-REF-1",
                    ),
                    _camt_entry(
                        amount="15.50",
                        direction="CRDT",
                        purpose="Erfundene Erstattung",
                        counterparty="Beispiel Person",
                        reference="FAKE-REF-2",
                        booking_date=date(2026, 11, 21),
                        value_date=date(2026, 11, 21),
                    ),
                )
            ),
        ),
        camt_pending=(
            fake_camt_xml(
                (
                    _camt_entry(
                        amount="9.99",
                        direction="DBIT",
                        purpose="Erfundene Vormerkung",
                        counterparty="Beispiel Person",
                        reference="FAKE-PENDING-1",
                    ),
                )
            ),
        ),
    )


def script_tan_text() -> FakeScript:
    """Textuelle TAN: erste Antwort ``NeedTANResponse``, nach TAN der Saldo."""
    return FakeScript(
        name="tan_text",
        accounts=fake_accounts(),
        balance=(
            FakeTanFlow(
                challenges=(FakeTanChallenge("Bitte TAN eingeben: 123456"),),
                result=fake_balance(),
            ),
        ),
    )


def script_tan_app() -> FakeScript:
    """App-Freigabe mit zwei Challenges, danach Erfolg (F-SYN-04, D3)."""
    return FakeScript(
        name="tan_app",
        accounts=fake_accounts(),
        balance=(
            FakeTanFlow(
                challenges=(
                    FakeTanChallenge("Bitte in der App bestaetigen (1/2)", decoupled=True),
                    FakeTanChallenge("Bitte in der App bestaetigen (2/2)", decoupled=True),
                ),
                result=fake_balance(),
            ),
        ),
    )


def script_ing_90_days() -> FakeScript:
    """ING 90-Tage-Regel: App-Freigabe vor dem Saldo (F-SYN-05)."""
    return FakeScript(
        name="ing_90_days",
        accounts=fake_accounts(),
        balance=(
            FakeTanFlow(
                challenges=(
                    FakeTanChallenge(
                        "ING: 90 Tage ohne Abruf. Bitte Freigabe in der App.",
                        decoupled=True,
                    ),
                ),
                result=fake_balance(),
            ),
        ),
    )


def _error_script(
    name: str,
    exception_type: type[FinTSClientError],
    code: str,
    text: str,
) -> FakeScript:
    return FakeScript(
        name=name,
        accounts=fake_accounts(),
        balance=(FakeBankError(exception_type, code, text),),
    )


def script_consors_9800() -> FakeScript:
    """Consorsbank/Bankcode 9800: die Bibliothek meldet eine falsche PIN."""
    return _error_script(
        "consors_9800",
        FinTSClientPINError,
        "9800",
        "9800: PIN falsch (erfundener Testfall Consorsbank)",
    )


def script_consors_9010() -> FakeScript:
    """Consorsbank/Bankcode 9010: BPD nicht abrufbar, allgemeiner Client-Fehler."""
    return _error_script(
        "consors_9010",
        FinTSClientError,
        "9010",
        "9010: BPD nicht abrufbar (erfundener Testfall Consorsbank)",
    )


def script_wrong_pin() -> FakeScript:
    """Falsche PIN im offenen Dialog (Bankcode 9942)."""
    return _error_script(
        "wrong_pin",
        FinTSClientPINError,
        "9942",
        "9942: PIN falsch (erfundener Testfall)",
    )


def script_locked() -> FakeScript:
    """Gesperrter Zugang nach zu vielen Fehlversuchen (Bankcode 3938)."""
    return _error_script(
        "locked",
        FinTSClientTemporaryAuthError,
        "3938",
        "3938: Zugang gesperrt (erfundener Testfall)",
    )


def script_balance_only() -> FakeScript:
    """Saldo ohne Umsaetze und ohne camt-Lieferung (F-SYN-03)."""
    return FakeScript(
        name="balance_only",
        accounts=fake_accounts(),
        balance=(fake_balance("0,00"),),
    )


def script_camt_only() -> FakeScript:
    """camt-Lieferung mit getrennten gebuchten und vorgemerkten Streams."""
    return FakeScript(
        name="camt_only",
        accounts=fake_accounts(),
        balance=(fake_balance("250,00"),),
        camt_booked=(
            fake_camt_xml(
                (
                    _camt_entry(
                        amount="10.00",
                        direction="DBIT",
                        purpose="Erfundene camt-Buchung",
                        counterparty="Erfundene camt GmbH",
                        reference="FAKE-CAMT-1",
                    ),
                )
            ),
        ),
        camt_pending=(
            fake_camt_xml(
                (
                    _camt_entry(
                        amount="5.00",
                        direction="DBIT",
                        purpose="Erfundene camt-Vormerkung",
                        counterparty="Erfundene camt GmbH",
                        reference="FAKE-CAMT-PENDING-1",
                    ),
                )
            ),
        ),
    )


def script_mt940_pending() -> FakeScript:
    """MT940 mit einer gebuchten und einer vorgemerkten Buchung (D2)."""
    return FakeScript(
        name="mt940_pending",
        accounts=fake_accounts(),
        balance=(fake_balance("99,00"),),
        transactions=(
            [
                fake_transaction(
                    counterparty="Erfundene MT940 GmbH",
                    counterparty_iban=FAKE_IBAN_CARD,
                    purpose="Erfundene gebuchte MT940-Buchung",
                    amount="11,00",
                    status="D",
                    reference="FAKE-MT940-1",
                ),
            ],
        ),
        transactions_pending=(
            fake_transaction(
                counterparty="Erfundene MT940 GmbH",
                counterparty_iban=FAKE_IBAN_CARD,
                purpose="Erfundene MT940-Vormerkung",
                amount="7,00",
                status="D",
                reference="FAKE-MT940-PENDING-1",
                pending=True,
            ),
        ),
    )


def _orders_script(
    name: str,
    state: OrderState,
    orders: tuple[FakeStandingOrder, ...],
) -> FakeScript:
    return FakeScript(
        name=name,
        accounts=fake_accounts(),
        balance=(fake_balance(),),
        orders_state=state,
        orders=orders,
    )


def script_orders_present() -> FakeScript:
    """Auftragsbestand vorhanden (Skript-Option, Abruf folgt in T-075)."""
    return _orders_script(
        "orders_present",
        "available",
        (
            FakeStandingOrder(
                kind="dauerauftrag",
                counterparty="Erfundene Vermietung GmbH",
                counterparty_iban=FAKE_IBAN_CARD,
                purpose="Erfundene Miete",
                amount="750,00",
                interval="monatlich",
                next_execution=date(2026, 12, 1),
            ),
            FakeStandingOrder(
                kind="terminauftrag",
                counterparty="Erfundene Versicherung AG",
                counterparty_iban="DE00777700001111222233",
                purpose="Erfundene Jahresprämie",
                amount="120,00",
                next_execution=date(2027, 1, 15),
            ),
        ),
    )


def script_orders_empty() -> FakeScript:
    """Bank liefert keinen Auftragsbestand (F-AUF-05)."""
    return _orders_script("orders_empty", "empty", ())


def script_orders_unsupported() -> FakeScript:
    """Bank unterstuetzt den Auftragsabruf nicht (F-AUF-05)."""
    return _orders_script("orders_unsupported", "unsupported", ())


SCRIPT_FACTORIES: Final[dict[str, Callable[[], FakeScript]]] = {
    "happy_path": script_happy_path,
    "tan_text": script_tan_text,
    "tan_app": script_tan_app,
    "ing_90_days": script_ing_90_days,
    "consors_9800": script_consors_9800,
    "consors_9010": script_consors_9010,
    "wrong_pin": script_wrong_pin,
    "locked": script_locked,
    "balance_only": script_balance_only,
    "camt_only": script_camt_only,
    "mt940_pending": script_mt940_pending,
    "orders_present": script_orders_present,
    "orders_empty": script_orders_empty,
    "orders_unsupported": script_orders_unsupported,
}


def _interval_parts(
    interval: str | None,
) -> tuple[StandingOrderTimeUnit1, int]:
    """Uebersetzt ein Klartext-Intervall in FinTS-Zeiteinheit und Turnus."""
    mapping: dict[str, tuple[StandingOrderTimeUnit1, int]] = {
        "monatlich": (StandingOrderTimeUnit1.MONTHLY, 1),
        "quartalsweise": (StandingOrderTimeUnit1.MONTHLY, 3),
        "halbjaehrlich": (StandingOrderTimeUnit1.MONTHLY, 6),
        "jaehrlich": (StandingOrderTimeUnit1.MONTHLY, 12),
        "woechentlich": (StandingOrderTimeUnit1.WEEKLY, 1),
    }
    return mapping.get(interval or "", (StandingOrderTimeUnit1.MONTHLY, 1))


class FakeFinTS3PinTanClient:
    """Skriptgesteuerter Ersatz fuer ``FinTS3PinTanClient`` (D8, nie Netz)."""

    def __init__(
        self,
        bank_identifier: str = FAKE_BANK_IDENTIFIER,
        user_id: str = FAKE_USER_ID,
        pin: str = FAKE_PIN,
        server: str = FAKE_SERVER,
        customer_id: str | None = None,
        tan_medium: str | None = None,
        *args: Any,
        from_data: bytes | None = None,
        script: FakeScript | None = None,
        **kwargs: Any,
    ) -> None:
        del args
        if from_data is not None and script is not None:
            raise ValueError("from_data und script schliessen sich aus")
        if from_data is not None:
            self._script = _script_from_data(from_data)
        else:
            self._script = script if script is not None else script_happy_path()

        self.bank_identifier = bank_identifier
        self.user_id = user_id
        self.server = server
        self.customer_id = customer_id
        self.tan_medium = tan_medium
        self.product_id = kwargs.get("product_id", FAKE_PRODUCT_ID)
        # Instanzattribut wie in der Bibliothek, initial None (T-060).
        self.init_tan_response: NeedTANResponse | None = None
        self.pin = FakePin(pin)
        # Der Fake liefert keine BPD; die Auftragserweiterung nutzt dann das
        # Standard-pain-Format (T-075).
        self.bpd = None

        self._calls: list[str] = []
        self._balance_index = 0
        self._transactions_index = 0
        self._accounts_index = 0
        self._pending_tan: FakeTanFlow | None = None
        self._tan_index = 0
        self._paused = False
        self._dialog_depth = 0

        self.pause_count = 0
        self.resume_count = 0
        self.deconstruct_count = 0
        self.send_tan_count = 0

    # -- Sichtbarer Zustand -------------------------------------------------

    @property
    def script(self) -> FakeScript:
        return self._script

    @property
    def method_log(self) -> tuple[str, ...]:
        """Alle aufgerufenen Methoden in Reihenfolge (Beweis: keine Schreiboperation)."""
        return tuple(self._calls)

    @property
    def orders_state(self) -> OrderState:
        return self._script.orders_state

    @property
    def orders(self) -> tuple[FakeStandingOrder, ...]:
        return self._script.orders

    def reset_log(self) -> None:
        """Leert nur das Methodenprotokoll, nicht die Zaehler."""
        self._calls.clear()

    # -- Dialoglebenszyklus -------------------------------------------------

    def __enter__(self) -> FakeFinTS3PinTanClient:
        self._dialog_depth += 1
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._dialog_depth = max(0, self._dialog_depth - 1)

    def pause_dialog(self) -> bytes:
        """Friert einen offenen TAN-Dialog ein und zaehlt den Aufruf (D3)."""
        self._log("pause_dialog")
        if self._pending_tan is None:
            raise RuntimeError("Cannot pause dialog, no standing dialog exists")
        if self._paused:
            raise RuntimeError("Dialog ist bereits pausiert")
        self.pause_count += 1
        self._paused = True
        return _DIALOG_MAGIC + str(self.pause_count).encode("ascii")

    @contextlib.contextmanager
    def resume_dialog(self, dialog_data: bytes) -> Iterator[FakeFinTS3PinTanClient]:
        """Nimmt einen pausierten Dialog wieder auf und zaehlt den Aufruf (D3)."""
        self._log("resume_dialog")
        if not isinstance(dialog_data, (bytes, bytearray)):
            raise ValueError("Unbekannter Dialogzustand")
        if not bytes(dialog_data).startswith(_DIALOG_MAGIC):
            raise ValueError("Unbekannter Dialogzustand")
        if not self._paused:
            raise RuntimeError("Kein pausierter Dialog vorhanden")
        self._paused = False
        self.resume_count += 1
        with self:
            yield self

    def deconstruct(self, including_private: bool = False) -> bytes:
        """Liefert einen geheimnisfreien Client-Zustand (N-SEC-02, D3).

        Der Blob enthaelt nur den Skriptnamen, nie PIN, TAN oder Kontodaten.
        """
        self._log("deconstruct")
        self.deconstruct_count += 1
        del including_private  # nur Signaturkompatibilitaet, keine privaten Daten
        return _CLIENT_MAGIC + self._script.name.encode("ascii")

    # -- Lesende Bankoperationen -------------------------------------------

    def get_sepa_accounts(self) -> Any:
        """Konten; optional ueber ``accounts_outcome`` skriptgesteuert (TAN/Fehler).

        Ohne ``accounts_outcome`` liefert der Aufruf direkt die Skript-Konten
        (unveraendertes Verhalten). Mit Eintraegen werden sie wie Saldo und
        Umsaetze der Reihe nach verbraucht, damit auch der Anlage-Assistent
        (F-KTO-05a) eine TAN-Pause oder einen Bankfehler testen kann.
        """
        if self._script.accounts_outcome:
            return self._next_outcome("get_sepa_accounts", self._script.accounts_outcome, "_accounts_index")
        self._log("get_sepa_accounts")
        return list(self._script.accounts)

    def get_information(self) -> dict[str, Any]:
        """Liefert Bank- und Kontoinformationen im Bibliotheksformat (F-KTO-05a)."""
        self._log("get_information")
        accounts: list[dict[str, Any]] = []
        for info in self._script.account_information:
            owners = [info.owner_name] if info.owner_name else []
            accounts.append(
                {
                    "iban": info.iban,
                    "type": info.account_type,
                    "product_name": info.product_name,
                    "owner_name": owners,
                    "currency": info.currency,
                }
            )
        bank: dict[str, Any] = {}
        if self._script.bank_name is not None:
            bank["name"] = self._script.bank_name
        return {"bank": bank, "accounts": accounts, "auth": {}}

    def get_tan_mechanisms(self) -> dict[str, Any]:
        """Liefert die angebotenen TAN-Verfahren im Bibliotheksformat (F-KTO-05a)."""
        self._log("get_tan_mechanisms")
        mechanisms: dict[str, Any] = {}
        for mechanism in self._script.tan_mechanisms:
            mechanisms[mechanism.security_function] = SimpleNamespace(
                security_function=mechanism.security_function,
                name=mechanism.name,
                decoupled=mechanism.decoupled,
            )
        return mechanisms

    def get_balance(self, account: SEPAAccount) -> Any:
        self._check_account(account)
        return self._next_outcome("get_balance", self._script.balance, "_balance_index")

    def get_transactions_xml(
        self,
        account: SEPAAccount,
        start_date: date | None = None,
        end_date: date | None = None,
        supported_camt_messages: Sequence[str] | None = None,
    ) -> tuple[list[bytes], list[bytes]]:
        self._check_account(account)
        self._log("get_transactions_xml")
        del start_date, end_date, supported_camt_messages
        return list(self._script.camt_booked), list(self._script.camt_pending)

    def get_transactions(
        self,
        account: SEPAAccount,
        start_date: date | None = None,
        end_date: date | None = None,
        include_pending: bool = False,
    ) -> Any:
        self._check_account(account)
        del start_date, end_date
        outcome = self._next_outcome("get_transactions", self._script.transactions, "_transactions_index")
        if not isinstance(outcome, list):
            # TAN-Anforderung oder Fehler: unveraendert an den Aufrufer.
            return outcome
        if include_pending:
            return [*outcome, *self._script.transactions_pending]
        return outcome

    # -- Lesende Auftragssegmente (T-075) ----------------------------------

    def _get_dialog(self) -> Any:
        """Liefert den laufenden Dialog; der Fake braucht keinen Netzaufbau."""
        return contextlib.nullcontext(self)

    def _find_highest_supported_command(self, *segment_classes: Any, **kwargs: Any) -> Any:
        """Meldet HKCDB/HKCSB als nicht unterstuetzt oder unterstuetzt."""
        del kwargs
        if self._script.orders_state == "unsupported":
            raise FinTSUnsupportedOperation(f"{segment_classes[0].TYPE} nicht unterstuetzt")
        return segment_classes[0]

    def _fetch_with_touchdowns(
        self,
        dialog: object,
        segment_factory: Callable[[str | None], object],
        response_processor: Callable[[object], object],
        *args: object,
        **kwargs: object,
    ) -> object:
        """Liefert erfundene HICDB/HICSB-Segmente aus dem Skript (D8)."""
        del dialog, args, kwargs
        request = segment_factory(None)
        kind = "dauerauftrag" if request.header.type == "HKCDB" else "terminauftrag"
        segments = [self._order_segment(order) for order in self._script.orders if order.kind == kind]
        return response_processor(segments)

    def _order_segment(self, order: FakeStandingOrder) -> Any:
        """Baut aus einem Skript-Auftrag ein lesendes Antwortsegment."""
        pain = self._order_pain(order)
        if order.kind == "dauerauftrag":
            execution = order.next_execution or date(2026, 1, 1)
            unit, interval = _interval_parts(order.interval)
            details = StandingOrderDetails3(
                first_execution_date=VirtualDate(execution.year, execution.month, execution.day),
                time_unit=unit,
                interval=interval,
                execution_day=execution.day,
            )
            return HICDB1(
                account=None,
                sepa_descriptor="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03",
                sepa_pain_message=pain,
                task_id=f"FAKE-{order.kind}",
                standing_order_details=details,
            )
        return HICSB1(
            account=None,
            sepa_descriptor="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03",
            sepa_pain_message=pain,
            task_id=f"FAKE-{order.kind}",
        )

    @staticmethod
    def _order_pain(order: FakeStandingOrder) -> bytes:
        """Baut eine minimale erfundene pain.001-Nachricht."""
        amount = order.amount.replace(",", ".")
        execution = order.next_execution or date(2026, 1, 1)
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">'
            "<CstmrCdtTrfInitn><PmtInf>"
            f"<ReqdExctnDt>{execution.isoformat()}</ReqdExctnDt>"
            "<CdtTrfTxInf><Amt>"
            f'<InstdAmt Ccy="EUR">{escape(amount)}</InstdAmt>'
            "</Amt>"
            f"<Cdtr><Nm>{escape(order.counterparty)}</Nm></Cdtr>"
            "<CdtrAcct><Id>"
            f"<IBAN>{escape(order.counterparty_iban)}</IBAN>"
            "</Id></CdtrAcct>"
            f"<RmtInf><Ustrd>{escape(order.purpose)}</Ustrd></RmtInf>"
            "</CdtTrfTxInf></PmtInf></CstmrCdtTrfInitn></Document>"
        )
        return document.encode("utf-8")

    # -- TAN-Fortsetzung ----------------------------------------------------

    def send_tan(self, challenge: NeedTANResponse, tan: str | None = None) -> Any:
        """Setzt einen TAN-Flow fort; bei App-Freigabe wird ``tan`` ignoriert."""
        self._log("send_tan")
        if self._pending_tan is None:
            raise RuntimeError("Keine offene TAN-Anforderung")
        if not isinstance(challenge, NeedTANResponse):
            raise TypeError("send_tan erwartet eine NeedTANResponse")
        if not challenge.decoupled and not tan:
            raise ValueError("TAN fehlt")
        self.send_tan_count += 1
        return self._advance_tan()

    # -- Intern -------------------------------------------------------------

    def _log(self, operation: str) -> None:
        self._calls.append(operation)

    def _check_account(self, account: SEPAAccount) -> None:
        if account not in self._script.accounts:
            raise ValueError("Konto nicht im Skript enthalten")

    def _next_outcome(self, operation: str, outcomes: Sequence[Any], index_attr: str) -> Any:
        self._log(operation)
        index = getattr(self, index_attr)
        if index >= len(outcomes):
            if operation == "get_transactions":
                return []
            raise AssertionError(f"Skript {self._script.name!r} hat keine Antwort mehr fuer {operation}")
        setattr(self, index_attr, index + 1)
        return self._resolve(outcomes[index])

    def _resolve(self, outcome: Any) -> Any:
        if isinstance(outcome, FakeBankError):
            error = outcome.exception_type(outcome.text)
            setattr(error, "bank_code", outcome.code)
            setattr(error, "original_text", outcome.text)
            if isinstance(error, (FinTSClientPINError, FinTSClientTemporaryAuthError)):
                self.pin.block()
            raise error
        if isinstance(outcome, FakeTanFlow):
            self._pending_tan = outcome
            self._tan_index = 0
            return self._advance_tan()
        return outcome

    def _advance_tan(self) -> Any:
        flow = self._pending_tan
        if flow is None:
            raise RuntimeError("Keine offene TAN-Anforderung")
        if self._tan_index < len(flow.challenges):
            challenge = flow.challenges[self._tan_index]
            self._tan_index += 1
            return self._make_tan_response(challenge)
        result = flow.result
        self._pending_tan = None
        self._tan_index = 0
        return result

    def _make_tan_response(self, challenge: FakeTanChallenge) -> NeedTANResponse:
        tan_request = SimpleNamespace(challenge=challenge.text, challenge_hhduc=None)
        return NeedTANResponse(
            None,
            tan_request,
            resume_method="send_tan",
            tan_request_structured=False,
            decoupled=challenge.decoupled,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(script={self._script.name!r}, calls={len(self._calls)})"


def _script_from_data(data: bytes) -> FakeScript:
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("Unbekannter Client-Zustand")
    raw = bytes(data)
    if not raw.startswith(_CLIENT_MAGIC):
        raise ValueError("Unbekannter Client-Zustand")
    name = raw[len(_CLIENT_MAGIC) :].decode("ascii")
    factory = SCRIPT_FACTORIES.get(name)
    if factory is None:
        raise ValueError("Unbekannter Client-Zustand")
    return factory()
