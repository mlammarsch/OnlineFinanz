"""Typsichere Ergebnis- und Fehlerobjekte fuer den lesenden FinTS-Zugriff.

Die Typen entkoppeln die Sync-Schicht von python-fints (D2). Sie enthalten
keine Logik gegen die Bibliothek und fuehren keine Bankzuege aus; die
Adapter-Schicht (T-073) uebersetzt die Rueckgaben der Bibliothek hierher.

Abbildung der Anforderungen:

- F-SYN-03: Saldo, gebuchte und vorgemerkte Umsaetze sowie aktive Termin-
  und Dauerauftraege werden als eigene, immutable Ergebnisobjekte gefuehrt.
- F-SYN-05: :class:`FinTSBankError` traegt den Bankcode und den Originaltext
  der Bank. Die Fehler-Mapping-Schicht liest diese Attribute; ``str`` und
  ``repr`` geben nur eine feste, geheimnisfreie Meldung aus.
- F-AUF-05: :class:`OrdersResult` unterscheidet ``available``, ``empty`` und
  ``unsupported``, damit "Keine Auftragsdaten von der Bank verfuegbar" ohne
  Sync-Fehler dargestellt werden kann.
- N-SEC-02: PIN und TAN tauchen in keiner Fehlermeldung und keinem ``repr``
  auf. Die Fehlerklassen speichern maximal Bankcode und Originaltext und
  geben beides nicht ueber ``str``/``repr``/``args`` aus.
- N-DB-02: Betraege stehen in ganzen Cent (``amount_cents``), nie als Float.
- D2: :data:`READ_ONLY_OPERATIONS` ist die Whitelist der erlaubten lesenden
  python-fints-Operationen. Schreibende Geschaeftsvorfaelle sind nie Teil
  davon.

Alle Dataclasses sind ``frozen`` und nutzen Tupel statt Listen, damit ein
Ergebnis nach der Uebergabe nicht mehr veraendert werden kann.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

__all__ = [
    "READ_ONLY_OPERATIONS",
    "AccessLockedError",
    "BalanceResult",
    "FetchedStandingOrder",
    "FetchedTransaction",
    "FinTSBankError",
    "OrderAvailability",
    "OrderKind",
    "OrdersResult",
    "PinWrongError",
    "SyncAccountData",
    "SyncCancelledError",
    "TanChallenge",
    "TanKind",
    "TanTimeoutError",
    "TransactionStatus",
]

# Whitelist der lesenden python-fints-Operationen (D2). Die Namen entsprechen
# den oeffentlichen Client-Methoden; die FinTS-Geschaeftsvorfaelle stehen nur
# als Kommentar dahinter. Schreibende Operationen (z. B. sepa_transfer,
# simple_sepa_transfer, sepa_debit, simple_sepa_debit) sind bewusst nicht
# enthalten. Eigene lesende HKCDB-/HKCSB-Segmente (T-075) sind keine
# python-fints-Client-Methoden und werden dort gesondert geprueft.
READ_ONLY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "get_sepa_accounts",  # HKSPA: Konten abrufen
        "get_balance",  # HKSAL: Saldo abrufen
        "get_transactions_xml",  # HKCAZ: camt-Umsaetze abrufen
        "get_transactions",  # HKKAZ: MT940-Umsaetze abrufen
        # Anlage-Assistent (F-KTO-05a): Bank- und Kontoinformationen (HIBPA/
        # HIUPD) sowie die angebotenen TAN-Verfahren (HITANS) rein lesend.
        "get_information",  # HIBPA/HIUPD: Bank- und Kontoinformationen
        "get_tan_mechanisms",  # HITANS: angebotene TAN-Verfahren
    }
)

TransactionStatus = Literal["gebucht", "vorgemerkt"]
OrderKind = Literal["dauerauftrag", "terminauftrag"]
OrderAvailability = Literal["available", "empty", "unsupported"]
TanKind = Literal["text", "app"]

_TRANSACTION_STATUSES: Final[frozenset[str]] = frozenset({"gebucht", "vorgemerkt"})
_ORDER_KINDS: Final[frozenset[str]] = frozenset({"dauerauftrag", "terminauftrag"})
_ORDER_AVAILABILITIES: Final[frozenset[str]] = frozenset({"available", "empty", "unsupported"})
_TAN_KINDS: Final[frozenset[str]] = frozenset({"text", "app"})


@dataclass(frozen=True, slots=True)
class BalanceResult:
    """Saldo eines Kontos in ganzen Cent (F-SYN-03, N-DB-02).

    ``as_of`` ist der von der Bank gelieferte Stand, sofern vorhanden.
    """

    amount_cents: int
    currency: str
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class FetchedTransaction:
    """Eine gebuchte oder vorgemerkte Buchung aus dem FinTS-Abruf (F-SYN-03).

    ``status`` ist ``gebucht`` oder ``vorgemerkt``. ``raw_reference`` nimmt
    die von der Bank gelieferte Referenz auf, ohne sie zu interpretieren.
    Betraege stehen in ganzen Cent (N-DB-02).
    """

    booking_date: date
    amount_cents: int
    currency: str
    status: TransactionStatus = "gebucht"
    value_date: date | None = None
    counterparty: str | None = None
    counterparty_iban: str | None = None
    purpose: str | None = None
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _TRANSACTION_STATUSES:
            raise ValueError("Unbekannter Buchungsstatus")


@dataclass(frozen=True, slots=True)
class FetchedStandingOrder:
    """Ein aktiver Termin- oder Dauerauftrag, rein lesend (F-AUF-02, F-AUF-04).

    ``kind`` unterscheidet ``dauerauftrag`` (wiederkehrend, ``interval``) und
    ``terminauftrag`` (einmalig). Die Ausfuehrungsdaten sind optional, weil
    nicht jede Bank alle drei Termine liefert.
    """

    kind: OrderKind
    counterparty: str | None = None
    counterparty_iban: str | None = None
    purpose: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    interval: str | None = None
    next_execution: date | None = None
    first_execution: date | None = None
    last_execution: date | None = None

    def __post_init__(self) -> None:
        if self.kind not in _ORDER_KINDS:
            raise ValueError("Unbekannte Auftragsart")


@dataclass(frozen=True, slots=True)
class OrdersResult:
    """Ergebnis eines Auftragsabrufs (F-AUF-05).

    ``available`` enthaelt mindestens einen Auftrag, ``empty`` eine leere
    Antwort der Bank und ``unsupported`` eine Bank ohne Auftragsabruf. Nur
    ``available`` darf Auftraege tragen, damit sich die drei Zustaende nicht
    widersprechen.
    """

    status: OrderAvailability
    orders: tuple[FetchedStandingOrder, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _ORDER_AVAILABILITIES:
            raise ValueError("Unbekannter Auftragsstatus")
        if self.status == "available" and not self.orders:
            raise ValueError("Status 'available' erfordert mindestens einen Auftrag")
        if self.status != "available" and self.orders:
            raise ValueError("Nur Status 'available' darf Auftraege tragen")


@dataclass(frozen=True, slots=True)
class TanChallenge:
    """TAN-Aufforderung der Bank (F-SYN-04, D3).

    ``kind`` ist ``text`` fuer eine einzugebende TAN oder ``app`` fuer die
    Freigabe in der Banking-App. ``challenge`` ist der anzuzeigende Text.
    """

    kind: TanKind
    challenge: str

    def __post_init__(self) -> None:
        if self.kind not in _TAN_KINDS:
            raise ValueError("Unbekannte TAN-Art")


@dataclass(frozen=True, slots=True)
class SyncAccountData:
    """Alle lesend abgerufenen Daten eines Kontos fuer einen Sync (F-SYN-03).

    ``account_ref`` ist die von der Bank gelieferte Kontoreferenz (z. B.
    IBAN). ``orders`` ist ``None``, wenn der Auftragsabruf nicht Teil des
    Laufs war; andernfalls traegt es den Zustand nach F-AUF-05.
    """

    account_ref: str
    balance: BalanceResult | None = None
    transactions: tuple[FetchedTransaction, ...] = ()
    orders: OrdersResult | None = None


class FinTSBankError(Exception):
    """Von der Bank gemeldeter Fehler mit Code und Originaltext (F-SYN-05).

    Code und Originaltext bleiben als Attribute fuer die Fehler-Mapping-
    Schicht (T-066) erhalten. ``str``, ``repr`` und ``args`` enthalten sie
    bewusst nicht, damit PIN oder TAN auch bei fehlerhafter Befuellung nicht
    in Logs oder Fehlermeldungen gelangen (N-SEC-02).
    """

    _safe_message = "Die Bank hat den FinTS-Auftrag abgelehnt."

    def __init__(self, code: str, original_text: str) -> None:
        self.code = code
        self.original_text = original_text
        super().__init__(self._safe_message)

    def __str__(self) -> str:
        return self._safe_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class PinWrongError(FinTSBankError):
    """Die Bank hat die PIN abgelehnt (F-SYN-05, N-SEC-02)."""

    _safe_message = "Die Bank hat die PIN abgelehnt."


class AccessLockedError(FinTSBankError):
    """Der Bankzugang ist gesperrt (F-SYN-05, N-SEC-02)."""

    _safe_message = "Der Bankzugang ist gesperrt."


class TanTimeoutError(Exception):
    """Die TAN-Freigabe kam nicht rechtzeitig (F-SYN-04, N-SEC-02)."""

    _safe_message = "Die TAN-Freigabe ist nicht rechtzeitig erfolgt."

    def __init__(self) -> None:
        super().__init__(self._safe_message)

    def __str__(self) -> str:
        return self._safe_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class SyncCancelledError(Exception):
    """Der Sync wurde durch den Nutzer abgebrochen (F-SYN-10, N-SEC-02)."""

    _safe_message = "Der Sync wurde abgebrochen."

    def __init__(self) -> None:
        super().__init__(self._safe_message)

    def __str__(self) -> str:
        return self._safe_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"
