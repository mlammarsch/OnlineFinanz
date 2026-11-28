"""Fachlogik des FinTS-Anlage-Assistenten (F-KTO-05a).

Der Assistent begleitet den Anlagefluss im Backend, ohne einen echten
Bankzugriff auszuloesen (D8). Er buendelt:

- :func:`lookup_bank` (Banken-Stammdaten zur BLZ, mit Kennzeichen, ob ein
  FinTS-Endpunkt hinterlegt ist),
- :func:`build_discovery_result` (Uebersetzung der rein lesenden FinTS-Antworten
  "SEPA-Konten", "Kontoinformationen" und "angebotene TAN-Verfahren" in die
  Anzeigeliste des Assistenten, inklusive Kennzeichen "bereits angelegt"),
- :func:`create_accounts` (Anlage der gewaehlten Konten mit ``source='fints'``
  und ohne Doppelanlage derselben IBAN).

Referenzierte Anforderungen:

- F-KTO-05a (Bank waehlen oder BLZ eingeben, Zugangsdaten, TAN-Verfahren,
  Konten des Zugangs auswaehlen),
- F-KTO-10 (Banken-Stammdaten aus der Datenbank, kein Vorbelegen von
  Endpunkten),
- F-SYN-05 (Banktexte bleiben Klartext, aber nur ueber die Fehlerklassen der
  Adapter-Schicht),
- N-SEC-01/N-SEC-02 (keine PIN in Datentypen, Antworten oder Meldungen),
- N-DB-01 (kein rohes SQL, nur SQLAlchemy), N-DB-02 (Betraege in Cent - hier
  nur Metadaten, keine Betraege).

Die Schicht kennt bewusst nur Standardfehler (``LookupError``, ``ValueError``);
die API-Schicht uebersetzt sie in HTTP-Fehler. Die IO-freien Uebersetzungen
(:func:`build_discovery_result`) sind von der Adapter-Schicht entkoppelt und
leicht testbar.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Any, Final

from fints.models import SEPAAccount  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
from sqlalchemy import select
from sqlalchemy.orm import Session

from finue.models.account import Account
from finue.models.bank import Bank, BankAccess
from finue.services.accounts import DEFAULT_CURRENCY, mask_iban, validate_iban

__all__ = [
    "AccountCreationResult",
    "AccountSelection",
    "BankAccessNotFound",
    "BankLookup",
    "DiscoveredAccount",
    "DiscoveryResult",
    "TanMethod",
    "WizardValidationError",
    "build_discovery_result",
    "create_accounts",
    "existing_ibans",
    "lookup_bank",
]

_BLZ_PATTERN: Final = re.compile(r"[0-9]{8}")
_NAME_MAX: Final = 255
_ACCOUNT_TYPE_MAX: Final = 50
_CURRENCY_PATTERN: Final = re.compile(r"[A-Z]{3}")


class BankAccessNotFound(LookupError):
    """Der angegebene Bankzugang existiert nicht."""


class WizardValidationError(ValueError):
    """Fachliche Validierung des Assistenten ist fehlgeschlagen."""


@dataclass(frozen=True, slots=True)
class TanMethod:
    """Ein von der Bank angebotenes TAN-Verfahren (F-KTO-05a).

    ``code`` ist die Sicherheitsfunktion (z. B. ``"900"``), ``name`` der
    Klartext der Bank oder ein neutraler Ersatztext. Der Code ist der Wert, den
    ``PATCH /api/bank-accesses/{id}`` als ``tan_method`` erwartet.
    """

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class DiscoveredAccount:
    """Ein Konto des Zugangs im Anlage-Assistenten (F-KTO-05a).

    ``already_exists`` ist ``True``, wenn bereits ein Konto mit derselben IBAN
    existiert; dann darf es nicht erneut angelegt werden (kein Doppelanlegen).
    """

    iban: str | None
    bic: str | None
    account_number: str | None
    name: str | None
    account_type: str | None
    already_exists: bool


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Ergebnis des Kontenabrufs im Assistenten (F-KTO-05a).

    Enthaelt den Banknamen, die gefundenen Konten und die angebotenen
    TAN-Verfahren. Es werden keine Kontodaten gespeichert; die Liste ist eine
    reine Anzeige fuer die Auswahl.
    """

    bank_name: str | None
    accounts: tuple[DiscoveredAccount, ...]
    tan_methods: tuple[TanMethod, ...]


@dataclass(frozen=True, slots=True)
class BankLookup:
    """Banken-Stammdaten zur BLZ samt Endpunkt-Kennzeichen (F-KTO-05a/10).

    ``found`` ist ``False``, wenn zur BLZ keine Bank gefuehrt wird; dann sind
    alle uebrigen Felder leer. ``fints_configured`` meldet, ob ein
    FinTS-Endpunkt hinterlegt ist - ohne ihn ist kein Bankzugriff moeglich.
    """

    found: bool
    blz: str
    bank_id: int | None
    name: str | None
    website: str | None
    fints_url: str | None
    fints_configured: bool


@dataclass(frozen=True, slots=True)
class AccountSelection:
    """Auswahl eines anzulegenden Kontos (F-KTO-05a)."""

    iban: object
    name: object = None
    account_type: object = None
    currency: object = DEFAULT_CURRENCY
    include_in_total: object = True


@dataclass(frozen=True, slots=True)
class AccountCreationResult:
    """Ergebnis der Konto-Anlage (F-KTO-05a).

    ``created`` sind die neu angelegten Konten, ``skipped_ibans`` die
    uebersprungenen IBANs (bereits vorhanden oder im Aufruf doppelt). Es wird
    niemals dieselbe IBAN doppelt angelegt.
    """

    created: tuple[Account, ...]
    skipped_ibans: tuple[str, ...]


# ------------------------------------------------------------------- Pruefungen


def _checked_blz(value: object) -> str:
    """Prueft eine Bankleitzahl (genau acht Ziffern, F-KTO-10)."""

    if not isinstance(value, str):
        raise WizardValidationError("Die BLZ muss ein Text aus acht Ziffern sein.")
    blz = value.strip()
    if not _BLZ_PATTERN.fullmatch(blz):
        raise WizardValidationError("Die BLZ muss aus genau acht Ziffern bestehen.")
    return blz


def _checked_name(value: object) -> str:
    """Prueft einen Kontonamen (nicht leer, maximal 255 Zeichen)."""

    if not isinstance(value, str):
        raise WizardValidationError("Der Name muss ein Text sein.")
    name = value.strip()
    if not name:
        raise WizardValidationError("Der Name darf nicht leer sein.")
    if len(name) > _NAME_MAX:
        raise WizardValidationError(f"Der Name darf hoechstens {_NAME_MAX} Zeichen lang sein.")
    return name


def _checked_currency(value: object) -> str:
    """Prueft einen Waehrungscode (ISO 4217, drei Grossbuchstaben)."""

    if not isinstance(value, str) or not _CURRENCY_PATTERN.fullmatch(value.strip().upper()):
        raise WizardValidationError("Die Waehrung muss ein ISO-4217-Code aus drei Buchstaben sein.")
    return value.strip().upper()


def _checked_account_type(value: object) -> str | None:
    """Prueft einen optionalen Kontotyp (maximal 50 Zeichen)."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise WizardValidationError("Der Kontotyp muss ein Text sein.")
    account_type = value.strip()
    if not account_type:
        return None
    if len(account_type) > _ACCOUNT_TYPE_MAX:
        raise WizardValidationError(f"Der Kontotyp darf hoechstens {_ACCOUNT_TYPE_MAX} Zeichen lang sein.")
    return account_type


def _checked_bool(value: object, field: str) -> bool:
    """Prueft einen echten Boolean."""

    if not isinstance(value, bool):
        raise WizardValidationError(f"{field} muss wahr oder falsch sein.")
    return value


# ------------------------------------------------------------------- Bank-Lookup


def lookup_bank(session: Session, blz: object) -> BankLookup:
    """Liefert die Banken-Stammdaten zur BLZ samt Endpunkt-Kennzeichen (F-KTO-05a).

    Es wird keine Bank vorbelegt oder angelegt; fehlt die BLZ, ist das
    Ergebnis ``found=False`` mit leerem Endpunkt (F-KTO-10).

    Raises:
        WizardValidationError: Die BLZ ist nicht achtstellig.
    """

    checked = _checked_blz(blz)
    stmt = select(Bank).where(Bank.blz == checked).order_by(Bank.id).limit(1)
    bank = session.scalars(stmt).first()
    if bank is None:
        return BankLookup(
            found=False,
            blz=checked,
            bank_id=None,
            name=None,
            website=None,
            fints_url=None,
            fints_configured=False,
        )
    return BankLookup(
        found=True,
        blz=bank.blz,
        bank_id=bank.id,
        name=bank.name,
        website=bank.website,
        fints_url=bank.fints_url,
        fints_configured=bool(bank.fints_url),
    )


# ------------------------------------------------------------------ Uebersetzung


def _clean_text(value: object) -> str | None:
    """Trimmt einen Text und macht leere Werte zu ``None``."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _information_by_iban(information: object) -> dict[str, Mapping[str, Any]]:
    """Indexiert die Kontoinformationen (HIUPD) nach normalisierter IBAN."""

    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(information, Mapping):
        return result
    accounts = information.get("accounts")
    if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)):
        return result
    for entry in accounts:
        if not isinstance(entry, Mapping):
            continue
        iban = _clean_text(entry.get("iban"))
        if iban is not None:
            result[_normalize_iban(iban)] = entry
    return result


def _account_name(info: Mapping[str, Any] | None) -> str | None:
    """Leitet den Kontonamen aus Produkt- oder Inhabername ab (F-KTO-05a)."""

    if info is None:
        return None
    product = _clean_text(info.get("product_name"))
    if product is not None:
        return product
    owners = info.get("owner_name")
    if isinstance(owners, Sequence) and not isinstance(owners, (str, bytes)) and owners:
        return _clean_text(owners[0])
    return None


def _account_type(info: Mapping[str, Any] | None) -> str | None:
    """Liest den Kontotyp aus den Kontoinformationen (F-KTO-05a)."""

    if info is None:
        return None
    return _clean_text(info.get("type"))


def _bank_name(information: object) -> str | None:
    """Liest den Banknamen aus den Kontoinformationen (F-KTO-05a)."""

    if not isinstance(information, Mapping):
        return None
    bank = information.get("bank")
    if isinstance(bank, Mapping):
        return _clean_text(bank.get("name"))
    return None


def _tan_methods(mechanisms: object) -> tuple[TanMethod, ...]:
    """Uebersetzt die angebotenen TAN-Verfahren in Anzeigeeintraege (F-KTO-05a)."""

    if not isinstance(mechanisms, Mapping):
        return ()
    methods: list[TanMethod] = []
    for code, parameter in mechanisms.items():
        if not isinstance(code, str) or not code.strip():
            continue
        name = _clean_text(getattr(parameter, "name", None))
        methods.append(TanMethod(code=code.strip(), name=name or f"Verfahren {code.strip()}"))
    methods.sort(key=lambda method: method.code)
    return tuple(methods)


def _normalize_iban(value: str) -> str:
    """Normalisiert eine IBAN fuer den Abgleich mit dem Bestand."""

    return "".join(value.split()).upper()


def build_discovery_result(
    *,
    sepa_accounts: Iterable[SEPAAccount],
    information: object,
    mechanisms: object,
    known_ibans: Set[str],
) -> DiscoveryResult:
    """Baut das Anzeigeergebnis des Kontenabrufs (F-KTO-05a).

    Die rein lesenden FinTS-Antworten (SEPA-Konten, Kontoinformationen,
    TAN-Verfahren) werden ohne IO in die Auswahlliste uebersetzt. Ein Konto
    traegt ``already_exists=True``, wenn seine normalisierte IBAN in
    ``known_ibans`` liegt (kein Doppelanlegen).
    """

    by_iban = _information_by_iban(information)
    accounts: list[DiscoveredAccount] = []
    for sepa in sepa_accounts:
        raw_iban = _clean_text(getattr(sepa, "iban", None))
        iban = _normalize_iban(raw_iban) if raw_iban is not None else None
        info = by_iban.get(iban) if iban is not None else None
        accounts.append(
            DiscoveredAccount(
                iban=iban,
                bic=_clean_text(getattr(sepa, "bic", None)),
                account_number=_clean_text(getattr(sepa, "accountnumber", None)),
                name=_account_name(info),
                account_type=_account_type(info),
                already_exists=bool(iban is not None and iban in known_ibans),
            )
        )
    return DiscoveryResult(
        bank_name=_bank_name(information),
        accounts=tuple(accounts),
        tan_methods=_tan_methods(mechanisms),
    )


# ------------------------------------------------------------------ Bestand/Konten


def existing_ibans(session: Session) -> set[str]:
    """Liefert die normalisierten IBANs aller Konten (Kennzeichen F-KTO-05a)."""

    stmt = select(Account.iban).where(Account.iban.is_not(None))
    return {_normalize_iban(value) for value in session.scalars(stmt) if isinstance(value, str) and value.strip()}


def create_accounts(
    session: Session,
    bank_access_id: object,
    selections: Iterable[AccountSelection],
) -> AccountCreationResult:
    """Legt die gewaehlten Konten an, ohne dieselbe IBAN doppelt zu fuehren (F-KTO-05a).

    ``source`` ist fest ``fints`` und die Konten werden dem uebergebenen
    Bankzugang zugeordnet. Eine IBAN, die bereits als Konto besteht (auch als
    Offline-Konto) oder im selben Aufruf doppelt vorkommt, wird uebersprungen.

    Raises:
        BankAccessNotFound: Der Bankzugang existiert nicht.
        IbanInvalid (aus :mod:`finue.services.accounts`): Eine IBAN ist ungueltig.
        WizardValidationError: Ein Feld ist ungueltig oder es wurde kein Konto
            ausgewaehlt.
    """

    access = _require_access(session, bank_access_id)
    known = existing_ibans(session)
    seen: set[str] = set()
    created: list[Account] = []
    skipped: list[str] = []

    for selection in selections:
        iban = validate_iban(selection.iban)
        if iban in seen or iban in known:
            skipped.append(iban)
            continue
        seen.add(iban)
        name = _checked_name(selection.name) if selection.name is not None else f"Konto {mask_iban(iban)}"
        account = Account(
            name=name,
            iban=iban,
            currency=_checked_currency(selection.currency),
            source="fints",
            account_type=_checked_account_type(selection.account_type),
            include_in_total=_checked_bool(selection.include_in_total, "include_in_total"),
            bank_access_id=access.id,
        )
        session.add(account)
        created.append(account)

    if not created:
        raise WizardValidationError("Es wurde kein neues Konto ausgewaehlt.")

    session.commit()
    for account in created:
        session.refresh(account)
    return AccountCreationResult(created=tuple(created), skipped_ibans=tuple(skipped))


def _require_access(session: Session, bank_access_id: object) -> BankAccess:
    """Laedt den Bankzugang oder wirft :class:`BankAccessNotFound`."""

    if isinstance(bank_access_id, bool) or not isinstance(bank_access_id, int) or bank_access_id < 1:
        raise WizardValidationError("bank_access_id muss eine positive Ganzzahl sein.")
    access = session.get(BankAccess, bank_access_id)
    if access is None:
        raise BankAccessNotFound(f"Bankzugang {bank_access_id} nicht gefunden.")
    return access
