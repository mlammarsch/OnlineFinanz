"""REST-Router des FinTS-Anlage-Assistenten (F-KTO-05a).

Der Router uebersetzt nur HTTP; die Fachlogik liegt in
:mod:`finue.services.fints_wizard` und den bestehenden Diensten
(:mod:`finue.services.bank_access` fuer den Zugang, :mod:`finue.services.sync_runner`
fuer den Kontenabruf als Job).

- ``GET /api/banks/lookup?blz=``: Banken-Stammdaten zur BLZ samt Kennzeichen,
  ob ein FinTS-Endpunkt hinterlegt ist (kein Vorbelegen, F-KTO-10).
- ``POST /api/bank-accesses``: legt einen Bankzugang an
  (:func:`finue.services.bank_access.create_access`); die PIN wird nur bei
  ``store_pin=true`` Fernet-verschluesselt gespeichert und erscheint nie in
  Antwort oder Log (N-SEC-01/02).
- ``PATCH /api/bank-accesses/{id}``: waehlt das TAN-Verfahren mit
  Versionspruefung (F-KTO-05a, F-CON-02).
- ``POST /api/bank-accesses/{id}/discover``: startet den Kontenabruf als Job
  ueber die :class:`~finue.services.sync_registry.SyncRegistry` (Sperre je
  Zugang, TAN-Pause/Resume ueber die Job-Endpunkte aus T-099); das Ergebnis
  steht im Job-Feld ``discovery``.
- ``POST /api/bank-accesses/{id}/accounts``: legt die gewaehlten Konten
  (``source="fints"``) an, ohne dieselbe IBAN doppelt zu fuehren.

Der Router bringt ``prefix="/api"`` und die Auth-Naht ``require_user`` selbst
mit (T-088-Entscheidung). Es werden ausschliesslich lesende FinTS-Operationen
ausgeloest (D2); die PIN bleibt im Arbeitsspeicher (N-SEC-02/03).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from finue.api.container import AppServices
from finue.api.conventions import VersionedRequest
from finue.api.deps import get_event_bus, get_services, get_session, require_user
from finue.api.errors import ConflictError, DomainError, NotFoundError
from finue.api.sync import SyncJobView, job_view
from finue.models.account import Account
from finue.models.bank import BankAccess
from finue.services import bank_access as access_service
from finue.services.accounts import AccountValidationError, mask_iban
from finue.services.events import EventBus
from finue.services.fints_wizard import (
    AccountCreationResult,
    AccountSelection,
    BankAccessNotFound,
    BankLookup,
    WizardValidationError,
    create_accounts,
    lookup_bank,
)
from finue.services.pin_crypto import PinCipher

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["bank-accesses"], dependencies=[Depends(require_user)])


# ------------------------------------------------------------------- DTOs


class BankLookupView(BaseModel):
    """Banken-Stammdaten zur BLZ samt Endpunkt-Kennzeichen (F-KTO-05a/10)."""

    found: bool
    blz: str
    bank_id: int | None = None
    name: str | None = None
    website: str | None = None
    fints_url: str | None = None
    fints_configured: bool


class BankAccessCreateRequest(BaseModel):
    """Anlage eines Bankzugangs (F-KTO-05a, N-SEC-01).

    ``pin`` ist nur bei ``store_pin=true`` noetig und wird ausschliesslich
    verschluesselt gespeichert; bei ``store_pin=false`` kommt sie je Sync aus
    dem Aufruf (F-SYN-08).
    """

    blz: str = Field(min_length=8, max_length=8)
    login_name: str = Field(min_length=1, max_length=255)
    store_pin: bool = True
    pin: str | None = Field(default=None, min_length=1, max_length=64)
    tan_method: str | None = Field(default=None, max_length=255)
    bank_name: str | None = Field(default=None, max_length=255)


class BankAccessView(BaseModel):
    """Ein Bankzugang ohne Geheimnisse (F-KTO-05a, N-SEC-02)."""

    id: int
    bank_id: int
    blz: str
    login_name: str
    tan_method: str | None = None
    store_pin: bool
    version: int


class BankAccessUpdateRequest(VersionedRequest):
    """Wahl des TAN-Verfahrens mit Versionspruefung (F-KTO-05a, F-CON-02)."""

    tan_method: str | None = Field(default=None, max_length=255)


class DiscoverRequest(BaseModel):
    """Anstoss des Kontenabrufs; die PIN nur im Arbeitsspeicher (N-SEC-02)."""

    pin: str | None = Field(default=None, min_length=1, max_length=64)


class AccountSelectionRequest(BaseModel):
    """Auswahl eines anzulegenden Kontos (F-KTO-05a)."""

    iban: str = Field(min_length=1, max_length=34)
    name: str | None = Field(default=None, max_length=255)
    account_type: str | None = Field(default=None, max_length=50)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    include_in_total: bool = True


class AccountsCreateRequest(BaseModel):
    """Anlage der im Assistenten gewaehlten Konten (F-KTO-05a)."""

    accounts: list[AccountSelectionRequest] = Field(min_length=1)


class CreatedAccountView(BaseModel):
    """Ein neu angelegtes FinTS-Konto ohne vollstaendige IBAN (F-KTO-01)."""

    id: int
    name: str
    iban_masked: str | None
    currency: str
    account_type: str | None
    source: str
    version: int


class AccountsCreateResponse(BaseModel):
    """Ergebnis der Konto-Anlage; uebersprungene IBANs sind bereits vorhanden."""

    created: list[CreatedAccountView]
    skipped: list[str]


# ---------------------------------------------------------------- Uebersetzung


def _lookup_dto(result: BankLookup) -> BankLookupView:
    """Uebersetzt das Lookup-Ergebnis in das API-DTO (F-KTO-05a)."""

    return BankLookupView(
        found=result.found,
        blz=result.blz,
        bank_id=result.bank_id,
        name=result.name,
        website=result.website,
        fints_url=result.fints_url,
        fints_configured=result.fints_configured,
    )


def _access_dto(access: BankAccess) -> BankAccessView:
    """Baut die Zugangsansicht (ohne PIN, N-SEC-02)."""

    return BankAccessView(
        id=access.id,
        bank_id=access.bank_id,
        blz=access.bank.blz,
        login_name=access.login_name,
        tan_method=access.tan_method,
        store_pin=access.store_pin,
        version=access.version,
    )


def _created_dto(account: Account) -> CreatedAccountView:
    """Baut die Ansicht eines neu angelegten Kontos."""

    return CreatedAccountView(
        id=account.id,
        name=account.name,
        iban_masked=mask_iban(account.iban),
        currency=account.currency,
        account_type=account.account_type,
        source=account.source,
        version=account.version,
    )


def _require_access(session: Session, access_id: int) -> BankAccess:
    """Liefert den Bankzugang oder wirft 404."""

    access = access_service.get_access(session, access_id)
    if access is None:
        raise NotFoundError(f"Der Bankzugang {access_id} wurde nicht gefunden.")
    return access


def _require_pin(access: BankAccess, pin: str | None) -> None:
    """Verlangt bei nicht gespeicherter PIN eine Eingabe (F-SYN-08)."""

    if not access.store_pin and pin is None:
        raise DomainError("PIN bitte eingeben.", details={"field": "pin"})


@contextmanager
def _access_errors(session: Session, access_id: int) -> Iterator[None]:
    """Uebersetzt Fehler der Zugangs-Schicht in HTTP-Fehler (F-CON-02)."""

    try:
        yield
    except LookupError as exc:
        raise NotFoundError(f"Der Bankzugang {access_id} wurde nicht gefunden.") from exc
    except access_service.BankAccessVersionConflict as exc:
        current = _access_dto(_require_access(session, access_id))
        raise ConflictError(current=current) from exc
    except access_service.PinRequiredError as exc:
        raise DomainError("PIN bitte eingeben.", details={"field": "pin"}) from exc
    except ValueError as exc:
        raise DomainError(str(exc)) from exc


# ------------------------------------------------------------------- Routen


@router.get("/banks/lookup", response_model=BankLookupView)
def lookup_bank_route(
    blz: str,
    session: Session = Depends(get_session),
) -> BankLookupView:
    """Liefert die Banken-Stammdaten zur BLZ samt Endpunkt-Kennzeichen (F-KTO-05a)."""

    try:
        result = lookup_bank(session, blz)
    except WizardValidationError as exc:
        raise DomainError(str(exc)) from exc
    return _lookup_dto(result)


@router.post("/bank-accesses", response_model=BankAccessView, status_code=status.HTTP_201_CREATED)
def create_bank_access_route(
    payload: BankAccessCreateRequest,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
    event_bus: EventBus = Depends(get_event_bus),
) -> BankAccessView:
    """Legt einen Bankzugang an (F-KTO-05a); die PIN wird nie zurueckgegeben.

    Bei ``store_pin=true`` ist ``pin`` Pflicht und wird Fernet-verschluesselt
    gespeichert (N-SEC-01). Bei ``store_pin=false`` wird kein Chiffretext
    abgelegt; die PIN kommt dann je Sync aus dem Aufruf (F-SYN-08).
    """

    try:
        cipher = _cipher_for(payload.store_pin, services)
        access = access_service.create_access(
            session,
            blz=payload.blz,
            login_name=payload.login_name,
            cipher=cipher,
            tan_method=payload.tan_method,
            store_pin=payload.store_pin,
            pin=payload.pin,
            bank_name=payload.bank_name,
        )
    except access_service.PinRequiredError as exc:
        raise DomainError("PIN bitte eingeben.", details={"field": "pin"}) from exc
    except ValueError as exc:
        raise DomainError(str(exc)) from exc

    event_bus.emit("bank_access.changed", bank_access=access.id, bank=access.bank_id)
    return _access_dto(access)


@router.patch("/bank-accesses/{access_id}", response_model=BankAccessView)
def update_bank_access_route(
    access_id: int,
    payload: BankAccessUpdateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> BankAccessView:
    """Waehlt das TAN-Verfahren des Zugangs mit Versionspruefung (F-KTO-05a)."""

    with _access_errors(session, access_id):
        access = access_service.update_access(
            session,
            access_id,
            payload.version,
            tan_method=payload.tan_method,
        )
    event_bus.emit("bank_access.changed", bank_access=access.id, bank=access.bank_id)
    return _access_dto(access)


@router.post("/bank-accesses/{access_id}/discover", response_model=SyncJobView, status_code=status.HTTP_202_ACCEPTED)
def discover_bank_access_route(
    access_id: int,
    payload: DiscoverRequest,
    session: Session = Depends(get_session),
    services: AppServices = Depends(get_services),
) -> SyncJobView:
    """Startet den Kontenabruf als Job (F-KTO-05a, F-SYN-11).

    Der Abruf nutzt die Zugangssperre der Sync-Registry und laeuft im
    Hintergrund. Verlangt die Bank eine TAN, steht der Job auf
    ``wartet_auf_tan``; die Fortsetzung laeuft ueber
    ``POST /api/sync/jobs/{id}/tan`` bzw. ``/approve-poll``.
    """

    access = _require_access(session, access_id)
    _require_pin(access, payload.pin)
    state = services.sync_runner.start_discovery(access.id, pin=payload.pin)
    return job_view(state)


@router.post("/bank-accesses/{access_id}/accounts", response_model=AccountsCreateResponse)
def create_bank_access_accounts_route(
    access_id: int,
    payload: AccountsCreateRequest,
    session: Session = Depends(get_session),
    event_bus: EventBus = Depends(get_event_bus),
) -> AccountsCreateResponse:
    """Legt die gewaehlten Konten an, ohne dieselbe IBAN doppelt zu fuehren (F-KTO-05a)."""

    selections = [
        AccountSelection(
            iban=item.iban,
            name=item.name,
            account_type=item.account_type,
            currency=item.currency,
            include_in_total=item.include_in_total,
        )
        for item in payload.accounts
    ]
    try:
        result = create_accounts(session, access_id, selections)
    except BankAccessNotFound as exc:
        raise NotFoundError(f"Der Bankzugang {access_id} wurde nicht gefunden.") from exc
    except (AccountValidationError, WizardValidationError) as exc:
        raise DomainError(str(exc)) from exc

    _emit_created(event_bus, result)
    return AccountsCreateResponse(
        created=[_created_dto(account) for account in result.created],
        skipped=list(result.skipped_ibans),
    )


def _cipher_for(store_pin: bool, services: AppServices) -> PinCipher | None:
    """Baut den PIN-Cipher nur, wenn eine PIN gespeichert werden soll (N-SEC-01).

    Bei ``store_pin=false`` bleibt der Wert ``None``: Es wird kein Schluessel
    gelesen und kein Chiffretext erzeugt.
    """

    if not store_pin:
        return None
    return PinCipher(services.settings)


def _emit_created(event_bus: EventBus, result: AccountCreationResult) -> None:
    """Meldet je neu angelegtem Konto das Ereignis ``account.changed`` (F-CON-04)."""

    for account in result.created:
        event_bus.emit("account.changed", account=account.id)
