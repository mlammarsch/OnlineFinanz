"""Lesender FinTS-Adapter: kapselt :class:`fints.client.FinTS3PinTanClient`.

Der Adapter ist die einzige Stelle der Anwendung, die python-fints aufruft.
Er setzt D2 und D3 um und haelt die Sync-Schicht frei von Bibliotheksdetails.

Abbildung der Anforderungen:

- F-SYN-03: Konten (``get_sepa_accounts``), Saldo (``get_balance``) sowie
  gebuchte und vorgemerkte Umsaetze. camt wird bevorzugt
  (``get_transactions_xml``, gebuchte und vorgemerkte Streams getrennt), bei
  fehlender Unterstuetzung folgt der MT940-Fallback
  (``get_transactions(..., include_pending=True)``). Zeitraeume werden
  unveraendert an die Bibliothek durchgereicht.
- F-SYN-04, D3: Eine ``NeedTANResponse`` wird in :class:`TanChallenge`
  uebersetzt. Der Dialog wird mit ``pause_dialog``, Ende des Dialogs und
  ``deconstruct`` eingefroren und ueber :class:`TanHandoff` an die Registry
  uebergeben. Die Fortsetzung erfolgt mit ``from_data``, ``resume_dialog`` und
  ``send_tan``; bei App-Freigabe fragt :meth:`FinTSGateway.poll_app_approval`
  erneut ab.
- F-SYN-05: Bankfehler werden in :class:`~finue.services.fints_types.FinTSBankError`
  mit Code und Originaltext uebersetzt; PIN- und Sperrfehler erhalten die
  spezialisierten Klassen.
- F-AUF-05, F-AUF-07: :meth:`FinTSGateway.fetch_standing_orders` liest
  Dauer- (HKCDB) und Terminauftraege (HKCSB) ueber die rein lesende
  Erweiterung in :mod:`finue.services.fints_orders`. Banken ohne
  Unterstuetzung liefern ``OrdersResult(status="unsupported")`` ohne
  Sync-Fehler.
- N-SEC-02: PIN und TAN werden nie protokolliert und tauchen weder in
  ``repr`` noch in Fehlermeldungen auf. ``TanRequired`` nennt nur den
  Banktext der Freigabe.
- N-SEC-03: Dialog- und Clientzustand bleiben als Bytes im Arbeitsspeicher und
  werden nicht auf Platte geschrieben.
- N-SEC-05: Der Endpunkt stammt ausschliesslich aus ``Bank.fints_url``. Ein
  fehlender Endpunkt oder eine fehlende ``FINTS_PRODUCT_ID`` bricht vor jedem
  Bankzugriff mit einer klaren Meldung ab.
- D2: Die Bankoperationen sind auf :data:`READ_ONLY_OPERATIONS` beschraenkt.
  Schreibende Geschaeftsvorfaelle ruft der Adapter nie auf.

MT940-Statusvertrag (T-068-Risiko): MT940 liefert eine gemeinsame Liste.
Vormerkungen erkennt der Adapter am Kennzeichen ``pending=True`` oder
``status`` ``PDNG``/``PENDING``. Fehlt der Buchungstag (``entry_date``), gilt
die Buchung als vorgemerkt; das ist die dokumentierte Regel der
Mapping-Schicht und wird als Warnung mit Anzahl protokolliert, nie still als
gebucht gespeichert. Eine gebuchte Buchung mit Buchungstag wird nie als
vorgemerkt abgelegt.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Generic, NoReturn, Protocol, TypeVar, cast

# python-fints liefert keine Typstubs (D2); die Namen werden zur Laufzeit
# geprueft und ueber das Protocol gekapselt.
from fints.client import (  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
    FinTS3PinTanClient,
    NeedTANResponse,
)
from fints.exceptions import (  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)
    FinTSClientPINError,
    FinTSClientTemporaryAuthError,
    FinTSError,
    FinTSUnsupportedOperation,
)
from fints.models import SEPAAccount  # type: ignore[import-untyped]  # python-fints liefert keine Typstubs (D2)

from finue.config import Settings, get_settings
from finue.models.bank import Bank
from finue.services.duplicate import TransactionData
from finue.services.fints_mapping import (
    FinTSMappingError,
    map_balance,
    map_camt_transactions,
    map_mt940_transactions,
)
from finue.services.fints_orders import (
    OrderFetchClient,
    build_orders_result,
    fetch_order_segments,
    orders_from_segments,
)
from finue.services.fints_types import (
    READ_ONLY_OPERATIONS,
    AccessLockedError,
    BalanceResult,
    FetchedStandingOrder,
    FinTSBankError,
    OrderKind,
    OrdersResult,
    PinWrongError,
    TanChallenge,
)

__all__ = [
    "FinTSClientProtocol",
    "FinTSGateway",
    "FinTSGatewayConfigError",
    "MissingBankIdentifierError",
    "MissingEndpointError",
    "MissingProductIdError",
    "TanHandoff",
    "TanRequired",
    "TransactionFetch",
]

_logger = logging.getLogger(__name__)

#: PIN-Fehlercodes der Banken (T-060, F-SYN-05).
_PIN_CODES: Final[frozenset[str]] = frozenset({"9800", "9340", "9910", "9930", "9931", "9942"})
#: Codes fuer einen gesperrten oder voruebergehend gesperrten Zugang.
_LOCKED_CODES: Final[frozenset[str]] = frozenset({"3938"})
#: MT940-Statuswerte, die eine Vormerkung kennzeichnen.
_PENDING_MARKERS: Final[frozenset[str]] = frozenset({"PDNG", "PENDING"})
_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{4})(?!\d)")

ResultT = TypeVar("ResultT")
Mapper = Callable[[object], ResultT]


class FinTSClientProtocol(Protocol):
    """Ausschnitt des python-fints-Clients, den der Adapter benoetigt.

    Die Methoden liefern absichtlich ``Any``, weil die Bibliothek je nach
    TAN-Fall ein Ergebnis oder eine ``NeedTANResponse`` zurueckgibt. Der
    Adapter prueft den Rueckgabewert zur Laufzeit.
    """

    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    def pause_dialog(self) -> bytes: ...

    def resume_dialog(self, dialog_data: bytes) -> AbstractContextManager[object]: ...

    def deconstruct(self, including_private: bool = False) -> bytes: ...

    def send_tan(self, challenge: NeedTANResponse, tan: str | None = None) -> Any: ...

    def get_sepa_accounts(self) -> Any: ...

    def get_information(self) -> Any: ...

    def get_tan_mechanisms(self) -> Any: ...

    def get_balance(self, account: SEPAAccount) -> Any: ...

    def get_transactions_xml(
        self,
        account: SEPAAccount,
        start_date: date | None = None,
        end_date: date | None = None,
        supported_camt_messages: Sequence[str] | None = None,
    ) -> Any: ...

    def get_transactions(
        self,
        account: SEPAAccount,
        start_date: date | None = None,
        end_date: date | None = None,
        include_pending: bool = False,
    ) -> Any: ...


ClientFactory = Callable[..., FinTSClientProtocol]


class FinTSGatewayConfigError(RuntimeError):
    """Der Adapter ist nicht vollstaendig konfiguriert (N-SEC-05, D2)."""


class MissingProductIdError(FinTSGatewayConfigError):
    """``FINTS_PRODUCT_ID`` fehlt; ein Bankzugriff ist nicht erlaubt."""


class MissingEndpointError(FinTSGatewayConfigError):
    """``Bank.fints_url`` fehlt; ein Bankzugriff ist nicht erlaubt (N-SEC-05)."""


class MissingBankIdentifierError(FinTSGatewayConfigError):
    """Die Bankleitzahl fehlt; python-fints kann den Client nicht aufbauen."""


@dataclass(frozen=True, slots=True)
class TransactionFetch:
    """Getrennt gelieferte gebuchte und vorgemerkte Umsaetze (F-SYN-03).

    ``booked`` und ``pending`` sind bewusst separate Tupel: Die
    Sync-Persistenz ersetzt Vormerkungen vollstaendig und darf sie nicht mit
    gebuchten Buchungen vermischen (F-DUP-04). ``all`` liefert gebuchte
    Buchungen zuerst, danach Vormerkungen.
    """

    booked: tuple[TransactionData, ...] = ()
    pending: tuple[TransactionData, ...] = ()

    @property
    def all(self) -> tuple[TransactionData, ...]:
        """Gebuchte und vorgemerkte Buchungen in fester Reihenfolge."""
        return self.booked + self.pending

    @property
    def total(self) -> int:
        """Anzahl aller gelieferten Buchungen."""
        return len(self.booked) + len(self.pending)


@dataclass(frozen=True, slots=True)
class TanHandoff(Generic[ResultT]):
    """Eingefrorener TAN-Dialog fuer die Uebergabe an die Registry (D3).

    ``dialog_state`` und ``client_state`` bleiben ausschliesslich im
    Arbeitsspeicher (N-SEC-03). ``response`` ist das lebende
    ``NeedTANResponse``-Objekt; es wird nicht in ``repr`` ausgegeben.
    ``mapper`` uebersetzt das Ergebnis nach der Freigabe in den passenden
    Anwendungstyp.
    """

    challenge: TanChallenge
    dialog_state: bytes = field(repr=False, compare=False)
    client_state: bytes = field(repr=False, compare=False)
    response: NeedTANResponse = field(repr=False, compare=False)
    mapper: Mapper[ResultT] = field(repr=False, compare=False)

    @property
    def is_app_approval(self) -> bool:
        """True, wenn die Bank eine Freigabe in der App verlangt (F-SYN-04)."""
        return self.challenge.kind == "app"


class TanRequired(Exception, Generic[ResultT]):
    """Die Bank verlangt eine TAN oder eine App-Freigabe (F-SYN-04, D3).

    Die Meldung enthaelt nur den festen Klartext. Der Handoff traegt die
    Dialogdaten fuer die Wiederaufnahme; PIN und TAN sind nicht Teil davon.
    """

    _safe_message = "Die Bank verlangt eine TAN- oder App-Freigabe."

    def __init__(self, handoff: TanHandoff[ResultT]) -> None:
        self.handoff = handoff
        super().__init__(self._safe_message)

    def __str__(self) -> str:
        return self._safe_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class FinTSGateway:
    """Lesender FinTS-Zugriff auf genau einen Bankzugang.

    Der Gateway oeffnet den Dialog ueber den Kontextmanager::

        with FinTSGateway(bank, login_name=..., pin=..., settings=...) as gw:
            accounts = gw.get_accounts()
            balance = gw.get_balance(accounts[0])

    Verlangt die Bank eine TAN, hebt die Operation :class:`TanRequired` mit
    einem :class:`TanHandoff`. Nach ``with`` beendet der Aufrufer den Dialog;
    die Fortsetzung laeuft ueber :meth:`resume_tan` oder
    :meth:`poll_app_approval`.
    """

    def __init__(
        self,
        bank: Bank,
        *,
        login_name: str,
        pin: str,
        settings: Settings | None = None,
        tan_medium: str | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._bank = bank
        self._login_name = _normalized_text(login_name)
        self._pin = pin
        self._settings = settings if settings is not None else get_settings()
        self._product_id = _normalized_text(self._settings.fints_product_id)
        # N-SEC-05: Der Endpunkt kommt ausschliesslich aus Bank.fints_url.
        self._endpoint = _normalized_text(bank.fints_url)
        self._blz = _normalized_text(bank.blz)
        self._tan_medium = _normalized_text(tan_medium)
        self._client_factory: ClientFactory = client_factory if client_factory is not None else _default_client_factory
        self._client: FinTSClientProtocol | None = None
        self._ensure_configured()

    # -- Konfiguration ------------------------------------------------------

    def _ensure_configured(self) -> None:
        """Prueft Produkt-ID und Endpunkt vor jedem Bankzugriff (D2, N-SEC-05)."""
        if self._product_id is None:
            raise MissingProductIdError(
                "FINTS_PRODUCT_ID fehlt. Ohne FinTS-Produkt-ID ist kein "
                "Bankzugriff erlaubt; bitte die Umgebungsvariable setzen."
            )
        if self._endpoint is None:
            raise MissingEndpointError(
                "Bank.fints_url fehlt. Ohne konfigurierten FinTS-Endpunkt ist kein Bankzugriff erlaubt (N-SEC-05)."
            )
        if self._blz is None:
            raise MissingBankIdentifierError(
                "Bank.blz fehlt. Ohne Bankleitzahl kann kein FinTS-Client aufgebaut werden."
            )
        if self._login_name is None:
            raise FinTSGatewayConfigError("login_name fehlt. Ohne Anmeldename ist kein Bankzugriff moeglich.")

    def _build_client(self, *, from_data: bytes | None = None) -> FinTSClientProtocol:
        """Baut den Bibliotheksclient; der Endpunkt ist immer ``Bank.fints_url``."""
        self._ensure_configured()
        kwargs: dict[str, Any] = {
            "bank_identifier": self._blz,
            "user_id": self._login_name,
            "pin": self._pin,
            "server": self._endpoint,
            "product_id": self._product_id,
        }
        if self._tan_medium is not None:
            kwargs["tan_medium"] = self._tan_medium
        if from_data is not None:
            kwargs["from_data"] = from_data
        return self._client_factory(**kwargs)

    # -- Dialoglebenszyklus -------------------------------------------------

    def __enter__(self) -> FinTSGateway:
        if self._client is not None:
            raise RuntimeError("FinTSGateway ist bereits geoeffnet.")
        client = self._build_client()
        client.__enter__()
        self._client = client
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.__exit__(exc_type, exc, tb)

    def _require_client(self) -> FinTSClientProtocol:
        client = self._client
        if client is None:
            raise RuntimeError("FinTSGateway muss fuer Bankzugriffe mit 'with' geoeffnet werden.")
        return client

    # -- Lesende Operationen ------------------------------------------------

    def get_accounts(self) -> tuple[SEPAAccount, ...]:
        """Liefert die Konten des Zugangs (F-SYN-01, ``get_sepa_accounts``)."""
        client = self._require_client()
        result = self._call("get_sepa_accounts")
        return self._finish(client, result, _as_accounts)

    def get_information(self) -> dict[str, Any]:
        """Liefert Bank- und Kontoinformationen (F-KTO-05a, ``get_information``).

        Die Bibliothek fasst HIBPA (Bank) und HIUPD (Konten) zusammen; die
        Antwort wird unveraendert als verschachteltes Mapping weitergereicht,
        damit die Wizard-Schicht Name und Typ je Konto ableiten kann. Ein
        eventueller TAN-Dialog wird wie bei den uebrigen Leseoperationen
        eingefroren (F-SYN-04, D3).
        """
        client = self._require_client()
        result = self._call("get_information")
        return self._finish(client, result, _as_mapping)

    def get_tan_mechanisms(self) -> dict[str, Any]:
        """Liefert die von der Bank angebotenen TAN-Verfahren (F-KTO-05a).

        Ergebnis ist ein Mapping ``Sicherheitsfunktion -> Parameterobjekt`` der
        Bibliothek; die Wizard-Schicht liest daraus Code und Namen. Der Abruf
        ist rein lesend (HITANS) und loest keinen schreibenden Vorgang aus.
        """
        client = self._require_client()
        result = self._call("get_tan_mechanisms")
        return self._finish(client, result, _as_mapping)

    def get_balance(self, account: SEPAAccount) -> BalanceResult:
        """Liefert den Saldo eines Kontos in ganzen Cent (F-SYN-03, N-DB-02)."""
        client = self._require_client()
        result = self._call("get_balance", account)
        return self._finish(client, result, map_balance)

    def get_transactions(
        self,
        account: SEPAAccount,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> TransactionFetch:
        """Liefert gebuchte und vorgemerkte Umsaetze getrennt (F-SYN-03, D2).

        Bevorzugt camt (``get_transactions_xml``). Fehlt die Unterstuetzung
        oder liefert die Bank keinen einzigen Stream, folgt der MT940-Fallback
        mit ``include_pending=True``. Der Zeitraum wird unveraendert
        durchgereicht.
        """
        _validate_range(start_date, end_date)
        client = self._require_client()
        try:
            result = self._call(
                "get_transactions_xml",
                account,
                start_date=start_date,
                end_date=end_date,
                allow_unsupported=True,
            )
        except FinTSUnsupportedOperation:
            return self._get_transactions_mt940(account, start_date, end_date, required=True)

        if isinstance(result, NeedTANResponse):
            self._raise_tan_required(client, result, _map_camt_result)

        booked_streams, pending_streams = _stream_pair(result)
        if not booked_streams and not pending_streams:
            # camt ist unterstuetzt, lieferte im Zeitraum aber nichts. Ein
            # MT940-Versuch kann trotzdem Daten liefern; fehlt auch HKKAZ,
            # bleibt das gueltige leere camt-Ergebnis bestehen.
            return self._get_transactions_mt940(account, start_date, end_date, required=False)
        return _map_camt_result(result)

    def _get_transactions_mt940(
        self,
        account: SEPAAccount,
        start_date: date | None,
        end_date: date | None,
        *,
        required: bool,
    ) -> TransactionFetch:
        """MT940-Fallback mit gebuchten und vorgemerkten Buchungen (D2)."""
        client = self._require_client()
        try:
            result = self._call(
                "get_transactions",
                account,
                start_date=start_date,
                end_date=end_date,
                include_pending=True,
                allow_unsupported=True,
            )
        except FinTSUnsupportedOperation as exc:
            if required:
                raise FinTSBankError("unbekannt", "Die Bank unterstuetzt keinen Umsatzabruf.") from exc
            return TransactionFetch()
        return self._finish(client, result, _map_mt940_result)

    def fetch_standing_orders(self, account: SEPAAccount | None = None) -> OrdersResult:
        """Liest aktive Dauer- und Terminauftraege rein lesend (F-AUF-04/07).

        Es werden ausschliesslich HKCDB und HKCSB gesendet; schreibende
        Segmente existieren in der Erweiterung nicht. Fehlt die Unterstuetzung
        fuer beide Arten, ist das Ergebnis ``unsupported`` ohne Sync-Fehler
        (F-AUF-05). Ohne ``account`` werden alle Konten gelesen und die
        Auftraege ohne Kontozuordnung zusammengefasst; die Sync-Orchestrierung
        uebergibt ein einzelnes Konto.

        Verlangt die Bank eine TAN, wird :class:`TanRequired` mit einem
        :class:`TanHandoff` gehoben. Der Mapper kombiniert die bereits
        gelesenen Auftraege mit der Antwort nach der Freigabe.
        """
        client = self._require_client()
        orders_client = cast(OrderFetchClient, client)
        accounts = (account,) if account is not None else self.get_accounts()
        collected: list[FetchedStandingOrder] = []
        any_supported = False
        for current in accounts:
            try:
                dauer = fetch_order_segments(orders_client, current, "dauerauftrag")
                if dauer.tan is not None:
                    self._raise_tan_required(
                        client,
                        cast(NeedTANResponse, dauer.tan),
                        _orders_mapper("dauerauftrag", tuple(collected)),
                    )
                if dauer.supported:
                    any_supported = True
                    collected.extend(orders_from_segments(dauer.segments, "dauerauftrag"))

                termin = fetch_order_segments(orders_client, current, "terminauftrag")
                if termin.tan is not None:
                    self._raise_tan_required(
                        client,
                        cast(NeedTANResponse, termin.tan),
                        _orders_mapper("terminauftrag", tuple(collected)),
                    )
                if termin.supported:
                    any_supported = True
                    collected.extend(orders_from_segments(termin.segments, "terminauftrag"))
            except FinTSUnsupportedOperation:
                # Die Erweiterung behandelt fehlende Unterstuetzung bereits als
                # Zustand; dieser Zweig sichert zusaetzlich einen spaeten
                # Bibliotheksfehler ab, ohne den Sync zu brechen (F-AUF-05).
                continue
            except FinTSError as exc:
                raise _translate_bank_error(exc) from exc

        return build_orders_result(tuple(collected), any_supported=any_supported)

    # -- TAN-Fortsetzung ----------------------------------------------------

    def resume_tan(self, handoff: TanHandoff[ResultT], tan: str | None = None) -> ResultT:
        """Setzt einen eingefrorenen TAN-Dialog fort (F-SYN-04, D3).

        Text-TAN: ``tan`` ist Pflicht. App-Freigabe: ``tan`` wird ignoriert;
        liefert die Bank eine weitere Freigabe, hebt die Methode erneut
        :class:`TanRequired` mit einem neuen Handoff.
        """
        if handoff.challenge.kind == "text" and not tan:
            raise ValueError("Fuer die TAN-Eingabe fehlt die TAN.")
        client = self._build_client(from_data=handoff.client_state)
        dialog = client.resume_dialog(handoff.dialog_state)
        dialog.__enter__()
        try:
            value: object = client.send_tan(
                handoff.response,
                "" if handoff.challenge.kind == "app" else (tan or ""),
            )
        except FinTSError as exc:
            dialog.__exit__(type(exc), exc, exc.__traceback__)
            raise _translate_bank_error(exc) from exc
        except BaseException as exc:
            dialog.__exit__(type(exc), exc, exc.__traceback__)
            raise

        if isinstance(value, NeedTANResponse):
            return self._raise_tan_after_resume(client, dialog, value, handoff.mapper)

        dialog.__exit__(None, None, None)
        return handoff.mapper(value)

    def poll_app_approval(self, handoff: TanHandoff[ResultT]) -> ResultT:
        """Fragt eine App-Freigabe erneut ab (F-SYN-04, D3)."""
        if handoff.challenge.kind != "app":
            raise ValueError("Es ist keine App-Freigabe offen.")
        return self.resume_tan(handoff, None)

    # -- Intern -------------------------------------------------------------

    def _call(
        self,
        operation: str,
        *args: object,
        allow_unsupported: bool = False,
        **kwargs: object,
    ) -> object:
        """Ruft ausschliesslich Operationen der Read-only-Whitelist auf (D2)."""
        if operation not in READ_ONLY_OPERATIONS:
            raise RuntimeError(f"Operation {operation!r} ist nicht als lesend freigegeben (D2).")
        client = self._require_client()
        method = getattr(client, operation)
        try:
            result: object = method(*args, **kwargs)
        except FinTSUnsupportedOperation as exc:
            if allow_unsupported:
                raise
            raise _translate_bank_error(exc) from exc
        except FinTSError as exc:
            raise _translate_bank_error(exc) from exc
        return result

    def _finish(
        self,
        client: FinTSClientProtocol,
        value: object,
        mapper: Mapper[ResultT],
    ) -> ResultT:
        """Uebersetzt ein Ergebnis oder friert den TAN-Dialog ein."""
        if isinstance(value, NeedTANResponse):
            self._raise_tan_required(client, value, mapper)
        return mapper(value)

    def _raise_tan_required(
        self,
        client: FinTSClientProtocol,
        response: NeedTANResponse,
        mapper: Mapper[ResultT],
    ) -> NoReturn:
        """Friert den Dialog ein und hebt :class:`TanRequired` (D3)."""
        dialog_state = client.pause_dialog()
        # Reihenfolge wie in der Bibliotheksdokumentation: erst den Dialog
        # beenden, dann deconstruct(). Der Gateway gilt danach als geschlossen.
        self._client = None
        client.__exit__(None, None, None)
        client_state = client.deconstruct()
        raise TanRequired(
            TanHandoff(
                challenge=_to_challenge(response),
                dialog_state=dialog_state,
                client_state=client_state,
                response=response,
                mapper=mapper,
            )
        )

    def _raise_tan_after_resume(
        self,
        client: FinTSClientProtocol,
        dialog: AbstractContextManager[object],
        response: NeedTANResponse,
        mapper: Mapper[ResultT],
    ) -> NoReturn:
        """Friert eine weitere App-Freigabe nach ``send_tan`` ein (F-SYN-04)."""
        dialog_state = client.pause_dialog()
        dialog.__exit__(None, None, None)
        client_state = client.deconstruct()
        raise TanRequired(
            TanHandoff(
                challenge=_to_challenge(response),
                dialog_state=dialog_state,
                client_state=client_state,
                response=response,
                mapper=mapper,
            )
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(blz={self._blz!r}, endpoint={self._endpoint!r})"


def _orders_mapper(
    kind: OrderKind,
    already: tuple[FetchedStandingOrder, ...],
) -> Mapper[OrdersResult]:
    """Baut den TAN-Mapper, der bereits gelesene Auftraege behaelt (F-AUF-04).

    Nach der Freigabe liefert die Bibliothek nur die Antwort der Art, die die
    TAN ausgeloest hat. Die zuvor gelesenen Auftraege bleiben erhalten, damit
    die Freigabe keine bereits ermittelten Daten verwirft.
    """

    def mapper(value: object) -> OrdersResult:
        if not isinstance(value, (list, tuple)):
            raise FinTSMappingError("Auftragsabruf lieferte nach TAN keine Segmentliste.")
        fetched = orders_from_segments(tuple(value), kind)
        return build_orders_result(already + fetched, any_supported=True)

    return mapper


def _default_client_factory(**kwargs: Any) -> FinTSClientProtocol:
    """Baut den echten python-fints-Client (nie in Tests verwendet, D8)."""
    return cast(FinTSClientProtocol, FinTS3PinTanClient(**kwargs))


def _as_accounts(value: object) -> tuple[SEPAAccount, ...]:
    """Prueft die Kontenliste der Bibliothek (F-SYN-01)."""
    if not isinstance(value, (list, tuple)):
        raise FinTSMappingError("Kontenabruf lieferte keine Liste.")
    accounts: list[SEPAAccount] = []
    for account in value:
        if not isinstance(account, SEPAAccount):
            raise FinTSMappingError("Kontenabruf lieferte ein unbekanntes Konto.")
        accounts.append(account)
    return tuple(accounts)


def _as_mapping(value: object) -> dict[str, Any]:
    """Prueft ein Ergebnis der Wizard-Abrufe als Mapping (F-KTO-05a)."""
    if not isinstance(value, Mapping):
        raise FinTSMappingError("Der Abruf lieferte kein Mapping.")
    return dict(value)


def _stream_pair(value: object) -> tuple[list[bytes | bytearray | str], list[bytes | bytearray | str]]:
    """Prueft das camt-Ergebnis ``(booked_streams, pending_streams)``."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise FinTSMappingError("camt-Abruf lieferte kein Paar aus Streams.")
    return _stream_list(value[0]), _stream_list(value[1])


def _stream_list(value: object) -> list[bytes | bytearray | str]:
    """Prueft eine camt-Streamliste der Bibliothek."""
    if not isinstance(value, (list, tuple)):
        raise FinTSMappingError("camt-Abruf lieferte keine Streamliste.")
    streams: list[bytes | bytearray | str] = []
    for stream in value:
        if not isinstance(stream, (bytes, bytearray, str)):
            raise FinTSMappingError("camt-Abruf lieferte keinen XML-Stream.")
        streams.append(stream)
    return streams


def _map_camt_result(value: object) -> TransactionFetch:
    """Bildet camt-Streams mit getrennter Statuszuordnung ab (F-SYN-03)."""
    booked_streams, pending_streams = _stream_pair(value)
    rows = map_camt_transactions(booked_streams, pending_streams)
    return _split_rows(rows)


def _map_mt940_result(value: object) -> TransactionFetch:
    """Bildet MT940-Buchungen mit getrenntem Status ab (F-SYN-03, D2)."""
    if not isinstance(value, (list, tuple)):
        raise FinTSMappingError("MT940-Abruf lieferte keine Liste.")
    transactions = list(value)
    booked, pending, assumed_pending = _split_mt940(transactions)
    if assumed_pending:
        # T-068-Risiko: Der fehlende Buchungstag wird nicht still als gebucht
        # gewertet. Die Anzahl macht die Annahme im Log sichtbar, ohne
        # Kontodaten oder Banktexte auszugeben (N-SEC-02).
        _logger.warning(
            "MT940: %d Buchung(en) ohne Buchungstag gelten als vorgemerkt.",
            assumed_pending,
        )
    booked_rows = map_mt940_transactions(booked, status="gebucht")
    pending_rows = map_mt940_transactions(pending, status="vorgemerkt")
    return TransactionFetch(booked=tuple(booked_rows), pending=tuple(pending_rows))


def _split_mt940(
    transactions: Sequence[object],
) -> tuple[list[Any], list[Any], int]:
    """Trennt MT940-Buchungen in gebucht und vorgemerkt (T-068-Risiko).

    Reihenfolge der Pruefung:

    1. explizites Vormerk-Kennzeichen ``pending``/``PDNG`` -> vorgemerkt,
    2. vorhandener Buchungstag -> gebucht,
    3. sonst vorgemerkt, aber als Annahme gezaehlt und protokolliert.

    Eine gebuchte Buchung mit Buchungstag wird nie als vorgemerkt abgelegt.
    """
    booked: list[Any] = []
    pending: list[Any] = []
    assumed_pending = 0
    for transaction in transactions:
        data = _transaction_data(transaction)
        if _has_pending_marker(data):
            pending.append(transaction)
        elif _has_entry_date(data):
            booked.append(transaction)
        else:
            pending.append(transaction)
            assumed_pending += 1
    return booked, pending, assumed_pending


def _transaction_data(transaction: object) -> Mapping[str, object]:
    """Liest das ``data``-Mapping einer MT940-Transaktion."""
    if isinstance(transaction, Mapping):
        return transaction
    data = getattr(transaction, "data", None)
    if isinstance(data, Mapping):
        return data
    raise FinTSMappingError("MT940-Transaktion ohne Datenfeld")


def _has_pending_marker(data: Mapping[str, object]) -> bool:
    """True bei explizitem Vormerk-Kennzeichen der Bank (F-DUP-04)."""
    if data.get("pending") is True:
        return True
    status = data.get("status")
    return isinstance(status, str) and status.strip().upper() in _PENDING_MARKERS


def _has_entry_date(data: Mapping[str, object]) -> bool:
    """True, wenn die Transaktion einen Buchungstag traegt."""
    entry_date = data.get("entry_date")
    if isinstance(entry_date, date):
        return True
    return isinstance(entry_date, str) and bool(entry_date.strip())


def _split_rows(rows: Sequence[TransactionData]) -> TransactionFetch:
    """Gruppiert abgebildete Buchungen nach ihrem Status (F-DUP-04)."""
    booked: list[TransactionData] = []
    pending: list[TransactionData] = []
    for row in rows:
        if row.status == "gebucht":
            booked.append(row)
        elif row.status == "vorgemerkt":
            pending.append(row)
        else:
            raise FinTSMappingError("Unbekannter Buchungsstatus")
    return TransactionFetch(booked=tuple(booked), pending=tuple(pending))


def _to_challenge(response: NeedTANResponse) -> TanChallenge:
    """Uebersetzt ``NeedTANResponse`` in eine Anzeige-Challenge (F-SYN-04)."""
    decoupled = bool(getattr(response, "decoupled", False))
    text = getattr(response, "challenge", None)
    challenge_text = (
        text.strip() if isinstance(text, str) and text.strip() else "Die Bank verlangt eine TAN- oder App-Freigabe."
    )
    return TanChallenge(
        kind="app" if decoupled else "text",
        challenge=challenge_text,
    )


def _validate_range(start_date: date | None, end_date: date | None) -> None:
    """Prueft den optionalen Zeitraum (F-SYN-03)."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date darf nicht nach end_date liegen.")


def _normalized_text(value: object) -> str | None:
    """Trimmt Text und macht leere Werte zu ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _translate_bank_error(error: FinTSError) -> FinTSBankError:
    """Uebersetzt einen Bibliotheksfehler mit Code und Originaltext (F-SYN-05)."""
    code = _bank_code(error)
    text = _original_text(error)
    if isinstance(error, FinTSClientPINError) or code in _PIN_CODES:
        return PinWrongError(code, text)
    if isinstance(error, FinTSClientTemporaryAuthError) or code in _LOCKED_CODES:
        return AccessLockedError(code, text)
    return FinTSBankError(code, text)


def _bank_code(error: FinTSError) -> str:
    """Liest den Bankcode aus dem Fehler oder aus seinem Text."""
    attribute = getattr(error, "bank_code", None)
    if isinstance(attribute, str) and attribute.strip():
        return attribute.strip()
    match = _CODE_PATTERN.search(str(error))
    return match.group(1) if match is not None else "unbekannt"


def _original_text(error: FinTSError) -> str:
    """Liest den Originaltext der Bank, sonst die Bibliotheksmeldung."""
    attribute = getattr(error, "original_text", None)
    if isinstance(attribute, str) and attribute.strip():
        return attribute.strip()
    return str(error)
