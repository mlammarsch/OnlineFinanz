"""Service-Schicht fuer Konten und Kontengruppen.

Bündelt die Fachlogik der Konten- und Gruppenverwaltung, damit REST (M4) und
MCP (M9) dieselbe Schicht nutzen (D10) und die API-Router nur noch HTTP
uebersetzen.

Referenzierte Anforderungen:

- F-KTO-01 (Kontenliste mit Saldo und letztem erfolgreichen Sync),
- F-KTO-02 (Gesamtsaldo mit Zusammensetzung; Fremdwaehrung getrennt, nicht
  addiert - Regel in ``decisions.md``),
- F-KTO-03 (Gruppen anlegen, umbenennen, sortieren, loeschen; Mitglieder,
  Kontenzahl, Summe),
- F-KTO-04 (Kontodetails pflegen: Name, Kontotyp, IBAN, Waehrung, Gruppe,
  Einfluss in den Gesamtsaldo),
- F-KTO-05b (Offline-Konto fuer den Datei-Import, optional Ankersaldo),
- F-KTO-06 (Archivieren und Wiederherstellen ueber ``active``),
- F-CON-01/F-CON-02 (optimistische Sperre ueber die Versionsspalte),
- N-DB-01 (kein rohes Daten-SQL, nur SQLAlchemy),
- N-DB-02 (Betraege in ganzen Cent, Zeitstempel in UTC).

Die Schicht kennt bewusst nur Standardfehler (``LookupError``, ``ValueError``,
``RuntimeError``); die API-Schicht uebersetzt sie in HTTP-Fehler.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Final, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.account import Account, AccountGroup
from finue.models.audit import AuditLog
from finue.models.duplicate import DuplicateDecision
from finue.models.import_run import ImportRun, ImportRunError
from finue.models.import_template import ImportTemplate
from finue.models.standing_order import StandingOrder
from finue.models.transaction import Transaction

__all__ = [
    "DEFAULT_CURRENCY",
    "SUCCESS_SYNC_STATUSES",
    "AccountNotFound",
    "AccountSyncRunning",
    "AccountValidationError",
    "AccountVersionConflict",
    "CurrencyBucket",
    "DeletionSummary",
    "GroupAccount",
    "GroupNotFound",
    "GroupSummary",
    "GroupVersionConflict",
    "IbanInvalid",
    "TotalComposition",
    "compute_total",
    "create_group",
    "create_offline_account",
    "delete_account",
    "delete_group",
    "get_account",
    "latest_sync_times",
    "list_accounts",
    "list_groups",
    "mask_iban",
    "normalize_iban",
    "rename_group",
    "reorder_groups",
    "set_archived",
    "update_account",
    "validate_iban",
]

#: Buchwaehrung, in der der Gesamtsaldo gefuehrt wird. Konten in anderen
#: Waehrungen fliessen nicht in den Gesamtsaldo ein (siehe ``decisions.md``).
DEFAULT_CURRENCY: Final = "EUR"

#: Statuswerte eines Sync-Laufs, die als "letzter erfolgreicher Sync" zaehlen
#: (F-KTO-01: erfolgreich oder teilweise).
SUCCESS_SYNC_STATUSES: Final = ("erfolgreich", "teilweise")

_IBAN_MIN: Final = 15
_IBAN_MAX: Final = 34
_IBAN_PATTERN: Final = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}")
_CURRENCY_PATTERN: Final = re.compile(r"[A-Z]{3}")
_NAME_MAX: Final = 255
_ACCOUNT_TYPE_MAX: Final = 50


class AccountNotFound(LookupError):
    """Das angeforderte Konto existiert nicht."""


class GroupNotFound(LookupError):
    """Die angeforderte Gruppe existiert nicht."""


class AccountValidationError(ValueError):
    """Fachliche Validierung eines Kontos ist fehlgeschlagen."""


class IbanInvalid(AccountValidationError):
    """Die IBAN ist formal oder per Pruefziffer ungueltig."""


class AccountVersionConflict(RuntimeError):
    """Optimistisches Sperren: die bekannte Kontoversion passt nicht (F-CON-02)."""

    def __init__(self, account_id: int, expected_version: int, current_version: int) -> None:
        self.account_id = account_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Konto {account_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


class AccountSyncRunning(RuntimeError):
    """Fuer den Bankzugang des Kontos laeuft bereits ein Sync-Job (F-SYN-11).

    Das Konto bleibt unveraendert; der Aufrufer wartet auf den Abschluss des
    laufenden Laufs oder bricht ihn vorher ab. Ein loeschendes Konto darf einen
    laufenden Sync nicht unterbrechen (F-KTO-07).
    """

    def __init__(self, bank_access_id: int) -> None:
        self.bank_access_id = bank_access_id
        super().__init__(
            f"Fuer den Bankzugang {bank_access_id} laeuft bereits ein Sync. "
            "Das Konto kann erst nach dessen Abschluss geloescht werden."
        )


class GroupVersionConflict(RuntimeError):
    """Optimistisches Sperren: die bekannte Gruppenversion passt nicht (F-CON-02)."""

    def __init__(self, group_id: int, expected_version: int, current_version: int) -> None:
        self.group_id = group_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Gruppe {group_id} wurde zwischenzeitlich geaendert "
            f"(erwartet Version {expected_version}, aktuell {current_version})."
        )


# --------------------------------------------------------------------------- IBAN


def normalize_iban(value: str) -> str:
    """Entfernt Leerzeichen und schreibt die IBAN gross (F-KTO-04)."""

    return "".join(value.split()).upper()


def _mod97(compact: str) -> int:
    """Berechnet den Modulo-97-Rest der IBAN nach ISO 7064."""

    rearranged = compact[4:] + compact[:4]
    digits: list[str] = []
    for char in rearranged:
        if char.isdigit():
            digits.append(char)
        else:
            digits.append(str(ord(char) - ord("A") + 10))
    remainder = 0
    for char in "".join(digits):
        remainder = (remainder * 10 + int(char)) % 97
    return remainder


def validate_iban(value: object) -> str:
    """Prueft eine IBAN auf Format und Pruefziffer und liefert sie normalisiert.

    Raises:
        IbanInvalid: Laenge, Zeichen, Aufbau oder Pruefziffer stimmen nicht.
    """

    if not isinstance(value, str):
        raise IbanInvalid("Die IBAN muss ein Text sein.")
    compact = normalize_iban(value)
    if not (_IBAN_MIN <= len(compact) <= _IBAN_MAX):
        raise IbanInvalid(f"Die IBAN muss zwischen {_IBAN_MIN} und {_IBAN_MAX} Zeichen lang sein.")
    if not _IBAN_PATTERN.fullmatch(compact):
        raise IbanInvalid("Die IBAN muss mit zwei Buchstaben und zwei Ziffern beginnen und alphanumerisch sein.")
    if _mod97(compact) != 1:
        raise IbanInvalid("Die Pruefziffer der IBAN ist ungueltig.")
    return compact


def mask_iban(value: str | None) -> str | None:
    """Maskiert eine IBAN fuer die Anzeige (F-KTO-01, Regel in ``docs/api.md``).

    Die ersten vier Zeichen (Laenderkennung und Pruefziffern) und die letzten
    vier Zeichen bleiben sichtbar; alle Zeichen dazwischen werden durch je ein
    ``*`` ersetzt. Kurze Werte werden bis auf die letzten vier maskiert.
    """

    if value is None:
        return None
    compact = normalize_iban(value)
    if len(compact) <= 4:
        return "*" * len(compact)
    visible = 4
    if len(compact) <= 2 * visible:
        return "*" * (len(compact) - visible) + compact[-visible:]
    return compact[:visible] + "*" * (len(compact) - 2 * visible) + compact[-visible:]


# ------------------------------------------------------------------- Datentypen


@dataclass(frozen=True, slots=True)
class CurrencyBucket:
    """Summe der einfliessenden Konten einer Nicht-Buchwaehrung (F-KTO-02)."""

    currency: str
    amount_cents: int
    account_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TotalComposition:
    """Gesamtsaldo samt Zusammensetzung fuer das Info-Symbol (F-KTO-02)."""

    currency: str
    amount_cents: int
    included_ids: tuple[int, ...]
    excluded_ids: tuple[int, ...]
    foreign: tuple[CurrencyBucket, ...]


@dataclass(frozen=True, slots=True)
class GroupAccount:
    """Mitglied einer Gruppe mit Icon-Verweis (F-KTO-03)."""

    id: int
    has_icon: bool


@dataclass(frozen=True, slots=True)
class GroupSummary:
    """Gruppe mit Mitgliedern, Kontenzahl und Summe (F-KTO-03)."""

    id: int
    name: str
    sort_order: int
    version: int
    member_ids: tuple[int, ...]
    account_count: int
    currency: str
    amount_cents: int
    foreign: tuple[CurrencyBucket, ...]
    members: tuple[GroupAccount, ...]


@dataclass(frozen=True, slots=True)
class DeletionSummary:
    """Zeilenzahlen der vollstaendigen Konto-Bereinigung (F-KTO-07).

    Jede Zahl nennt die Anzahl der tatsaechlich entfernten Zeilen der jeweiligen
    Tabelle; ``import_templates_unlinked`` zaehlt die Vorlagen, deren
    Konto-Zuordnung nur geloest wurde.
    """

    transactions: int
    standing_orders: int
    import_runs: int
    import_run_errors: int
    duplicate_decisions: int
    audit_entries: int
    import_templates_unlinked: int


# ------------------------------------------------------------------- Pruefungen


def _checked_version(value: object) -> int:
    """Prueft eine erwartete Version (positive Ganzzahl)."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("expected_version muss eine positive Ganzzahl sein.")
    return value


def _checked_name(value: object) -> str:
    """Prueft einen Namen (nicht leer, maximal 255 Zeichen)."""

    if not isinstance(value, str):
        raise AccountValidationError("Der Name muss ein Text sein.")
    name = value.strip()
    if not name:
        raise AccountValidationError("Der Name darf nicht leer sein.")
    if len(name) > _NAME_MAX:
        raise AccountValidationError(f"Der Name darf hoechstens {_NAME_MAX} Zeichen lang sein.")
    return name


def _checked_currency(value: object) -> str:
    """Prueft einen Waehrungscode (ISO 4217, drei Grossbuchstaben)."""

    if not isinstance(value, str) or not _CURRENCY_PATTERN.fullmatch(value.strip().upper()):
        raise AccountValidationError("Die Waehrung muss ein ISO-4217-Code aus drei Buchstaben sein.")
    return value.strip().upper()


def _checked_account_type(value: object) -> str | None:
    """Prueft einen optionalen Kontotyp."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise AccountValidationError("Der Kontotyp muss ein Text sein.")
    account_type = value.strip()
    if not account_type:
        return None
    if len(account_type) > _ACCOUNT_TYPE_MAX:
        raise AccountValidationError(f"Der Kontotyp darf hoechstens {_ACCOUNT_TYPE_MAX} Zeichen lang sein.")
    return account_type


def _checked_include_in_total(value: object) -> bool:
    """Prueft den Einfluss in den Gesamtsaldo (F-KTO-02)."""

    if not isinstance(value, bool):
        raise AccountValidationError("include_in_total muss wahr oder falsch sein.")
    return value


def _ensure_account_version(account: Account, expected_version: int) -> None:
    """Vergleicht die erwartete mit der gespeicherten Kontoversion (F-CON-02)."""

    if account.version != expected_version:
        raise AccountVersionConflict(account.id, expected_version, account.version)


def _ensure_group_version(group: AccountGroup, expected_version: int) -> None:
    """Vergleicht die erwartete mit der gespeicherten Gruppenversion (F-CON-02)."""

    if group.version != expected_version:
        raise GroupVersionConflict(group.id, expected_version, group.version)


def _require_group(session: Session, group_id: int) -> AccountGroup:
    """Liefert die Gruppe oder wirft ``GroupNotFound``."""

    group = session.get(AccountGroup, group_id)
    if group is None:
        raise GroupNotFound(f"Gruppe {group_id} nicht gefunden.")
    return group


# --------------------------------------------------------------------- Kontenabruf


def get_account(session: Session, account_id: int) -> Account:
    """Liefert ein Konto (auch archiviert) oder wirft ``AccountNotFound``."""

    account = session.get(Account, account_id)
    if account is None:
        raise AccountNotFound(f"Konto {account_id} nicht gefunden.")
    return account


def list_accounts(session: Session, *, archived: bool = False) -> list[Account]:
    """Listet aktive oder archivierte Konten, nach Name und ID sortiert (F-KTO-01/06)."""

    stmt = select(Account).where(Account.active.is_(not archived)).order_by(Account.name, Account.id)
    return list(session.scalars(stmt))


def latest_sync_times(session: Session, account_ids: Iterable[int]) -> dict[int, datetime]:
    """Liefert je Konto den Zeitpunkt des letzten erfolgreichen Syncs (F-KTO-01).

    Beruecksichtigt nur Laeufe mit ``kind='sync'`` und einem Erfolgsstatus
    (``erfolgreich``/``teilweise``) und wertet ``finished_at`` aus.
    """

    ids = list(account_ids)
    if not ids:
        return {}
    stmt = (
        select(ImportRun.account_id, func.max(ImportRun.finished_at))
        .where(
            ImportRun.account_id.in_(ids),
            ImportRun.kind == "sync",
            ImportRun.status.in_(SUCCESS_SYNC_STATUSES),
            ImportRun.finished_at.is_not(None),
        )
        .group_by(ImportRun.account_id)
    )
    result: dict[int, datetime] = {}
    for account_id, finished_at in session.execute(stmt).all():
        if finished_at is not None:
            result[account_id] = cast(datetime, finished_at)
    return result


def compute_total(accounts: Iterable[Account], *, default_currency: str = DEFAULT_CURRENCY) -> TotalComposition:
    """Berechnet den Gesamtsaldo und seine Zusammensetzung (F-KTO-02).

    Es fliessen nur aktive Konten ein, die ``include_in_total`` tragen. Konten
    in einer anderen als der Buchwaehrung werden getrennt als :class:`CurrencyBucket`
    ausgewiesen und **nicht** addiert (Regel in ``decisions.md``).
    """

    included: list[int] = []
    excluded: list[int] = []
    amount_cents = 0
    foreign_ids: dict[str, list[int]] = {}
    foreign_amount: dict[str, int] = {}

    for account in accounts:
        if not account.include_in_total:
            excluded.append(account.id)
            continue
        if account.currency == default_currency:
            included.append(account.id)
            amount_cents += account.balance_cents or 0
        else:
            foreign_ids.setdefault(account.currency, []).append(account.id)
            foreign_amount[account.currency] = foreign_amount.get(account.currency, 0) + (account.balance_cents or 0)

    foreign = tuple(
        CurrencyBucket(currency=currency, amount_cents=foreign_amount[currency], account_ids=tuple(ids))
        for currency, ids in sorted(foreign_ids.items())
    )
    return TotalComposition(
        currency=default_currency,
        amount_cents=amount_cents,
        included_ids=tuple(included),
        excluded_ids=tuple(excluded),
        foreign=foreign,
    )


# ---------------------------------------------------------------------- Aendern


def create_offline_account(
    session: Session,
    *,
    name: object,
    currency: object = DEFAULT_CURRENCY,
    account_type: object = None,
    iban: object = None,
    opening_balance_cents: object = None,
    opening_balance_date: object = None,
    group_id: int | None = None,
    include_in_total: object = True,
) -> Account:
    """Legt ein Offline-Konto fuer den Datei-Import an (F-KTO-05b).

    ``source`` ist fest ``datei``. Ein optionaler Ankersaldo braucht Datum und
    Betrag gemeinsam; er wird als ``opening_balance_cents`` und als aktueller
    Saldo zum Datum gespeichert.

    Raises:
        AccountValidationError: Ein Feld ist ungueltig oder der Ankersaldo ist
            unvollstaendig.
        IbanInvalid: Die IBAN ist ungueltig.
        GroupNotFound: Die angegebene Gruppe existiert nicht.
    """

    checked_name = _checked_name(name)
    checked_currency = _checked_currency(currency)
    checked_type = _checked_account_type(account_type)
    checked_include = _checked_include_in_total(include_in_total)

    checked_iban: str | None = None
    if iban is not None and iban != "":
        checked_iban = validate_iban(iban)

    if opening_balance_cents is None and opening_balance_date is None:
        opening_cents: int | None = None
    elif opening_balance_cents is None or opening_balance_date is None:
        raise AccountValidationError("Ankersaldo und Ankerdatum muessen gemeinsam angegeben werden.")
    else:
        if not isinstance(opening_balance_cents, int) or isinstance(opening_balance_cents, bool):
            raise AccountValidationError("Der Ankersaldo muss in ganzen Cent angegeben werden.")
        if not isinstance(opening_balance_date, date):
            raise AccountValidationError("Das Ankerdatum muss ein Datum sein.")
        opening_cents = opening_balance_cents

    checked_group = None if group_id is None else _require_group(session, group_id)

    account = Account(
        name=checked_name,
        currency=checked_currency,
        source="datei",
        account_type=checked_type,
        iban=checked_iban,
        include_in_total=checked_include,
        group_id=checked_group.id if checked_group is not None else None,
        opening_balance_cents=opening_cents or 0,
    )
    if opening_cents is not None and opening_balance_date is not None:
        account.balance_cents = opening_cents
        # ``balance_at`` ist als ``UtcDateTime`` typisiert; wie in ``sync_persist``
        # per ``setattr`` setzen, damit mypy den Datetime-Wert akzeptiert.
        setattr(account, "balance_at", datetime.combine(opening_balance_date, time(0, 0), tzinfo=timezone.utc))

    session.add(account)
    session.commit()
    session.refresh(account)
    return account


_ACCOUNT_UPDATE_FIELDS: Final = frozenset({"name", "account_type", "iban", "currency", "group_id", "include_in_total"})


def update_account(
    session: Session,
    account_id: int,
    *,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Account:
    """Aendert Kontodetails mit Versionspruefung (F-KTO-04, F-CON-02).

    Nur die Schluessel aus :data:`_ACCOUNT_UPDATE_FIELDS` werden uebernommen;
    ein explizites ``None`` bei ``account_type``, ``iban`` oder ``group_id``
    loescht den Wert.

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        AccountVersionConflict: ``expected_version`` ist veraltet.
        AccountValidationError, IbanInvalid, GroupNotFound: Ein Wert ist ungueltig.
    """

    checked_version = _checked_version(expected_version)
    account = get_account(session, account_id)
    _ensure_account_version(account, checked_version)

    unknown = set(changes) - _ACCOUNT_UPDATE_FIELDS
    if unknown:
        raise AccountValidationError(f"Unbekannte Felder: {', '.join(sorted(unknown))}.")

    # Erst alle Werte pruefen (Lookups koennen Autoflush ausloesen), dann setzen,
    # damit genau ein UPDATE erfolgt und die Version nur um 1 steigt.
    new_values: dict[str, Any] = {}
    if "name" in changes:
        new_values["name"] = _checked_name(changes["name"])
    if "account_type" in changes:
        new_values["account_type"] = _checked_account_type(changes["account_type"])
    if "iban" in changes:
        raw_iban = changes["iban"]
        new_values["iban"] = None if raw_iban in (None, "") else validate_iban(raw_iban)
    if "currency" in changes:
        new_values["currency"] = _checked_currency(changes["currency"])
    if "group_id" in changes:
        raw_group = changes["group_id"]
        new_values["group_id"] = None if raw_group is None else _require_group(session, int(raw_group)).id
    if "include_in_total" in changes:
        new_values["include_in_total"] = _checked_include_in_total(changes["include_in_total"])

    for field_name, value in new_values.items():
        setattr(account, field_name, value)

    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise AccountVersionConflict(account_id, checked_version, _current_version(session, account_id)) from exc

    session.refresh(account)
    return account


def set_archived(session: Session, account_id: int, *, archived: bool, expected_version: int) -> Account:
    """Archiviert ein Konto oder stellt es wieder her (F-KTO-06, F-CON-02).

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        AccountVersionConflict: ``expected_version`` ist veraltet.
    """

    checked_version = _checked_version(expected_version)
    account = get_account(session, account_id)
    _ensure_account_version(account, checked_version)

    account.active = not archived
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise AccountVersionConflict(account_id, checked_version, _current_version(session, account_id)) from exc

    session.refresh(account)
    return account


def _current_version(session: Session, account_id: int) -> int:
    """Liest die aktuelle Kontoversion nach einem Rollback (fuer den 409-Stand)."""

    current = session.get(Account, account_id)
    return current.version if current is not None else -1


# --------------------------------------------------------------------- Loeschen


def _affected_rows(session: Session, statement: Any) -> int:
    """Fuehrt eine aendernde Anweisung aus und liefert die betroffenen Zeilen (N-DB-01).

    ``Session.execute`` liefert fuer DELETE/UPDATE zur Laufzeit ein
    ``CursorResult``; der Typ wird hier praezise gefasst, damit ``rowcount``
    unter mypy strict verfuegbar ist.
    """

    return int(cast(CursorResult[Any], session.execute(statement)).rowcount or 0)


def _purge_account_data(session: Session, account_id: int) -> DeletionSummary:
    """Entfernt die kontogebundenen Zeilen und liefert die Zeilenzahlen (F-KTO-07).

    Reihenfolge nach den Fremdschluesseln: zuerst die Dublettenentscheidungen
    (sie verweisen auf Buchungen), dann Buchungen, Auftraege, Import-Fehler,
    Import-Laeufe und kontogebundene Audit-Eintraege. Kontogebundene
    Importvorlagen werden nicht geloescht, sondern nur entkoppelt.
    """

    transaction_ids = list(session.scalars(select(Transaction.id).where(Transaction.account_id == account_id)))
    run_ids = list(session.scalars(select(ImportRun.id).where(ImportRun.account_id == account_id)))

    duplicate_decisions = 0
    if transaction_ids:
        duplicate_decisions = _affected_rows(
            session,
            delete(DuplicateDecision).where(
                (DuplicateDecision.transaction_id.in_(transaction_ids))
                | (DuplicateDecision.other_transaction_id.in_(transaction_ids))
            ),
        )

    transactions = _affected_rows(session, delete(Transaction).where(Transaction.account_id == account_id))
    standing_orders = _affected_rows(session, delete(StandingOrder).where(StandingOrder.account_id == account_id))

    import_run_errors = 0
    if run_ids:
        import_run_errors = _affected_rows(session, delete(ImportRunError).where(ImportRunError.run_id.in_(run_ids)))
    import_runs = _affected_rows(session, delete(ImportRun).where(ImportRun.account_id == account_id))

    # F-KTO-07: "Loeschen entfernt ... Protokolle des Kontos". Kontogebundene
    # Audit-Eintraege werden entfernt; globale Eintraege (account_id NULL)
    # bleiben erhalten (Entscheidung in decisions.md).
    audit_entries = _affected_rows(session, delete(AuditLog).where(AuditLog.account_id == account_id))

    # F-IMP-05/F-KTO-07: Vorlagen bleiben erhalten, nur die Zuordnung entfaellt.
    import_templates_unlinked = _affected_rows(
        session,
        update(ImportTemplate).where(ImportTemplate.account_id == account_id).values(account_id=None),
    )

    return DeletionSummary(
        transactions=transactions,
        standing_orders=standing_orders,
        import_runs=import_runs,
        import_run_errors=import_run_errors,
        duplicate_decisions=duplicate_decisions,
        audit_entries=audit_entries,
        import_templates_unlinked=import_templates_unlinked,
    )


def delete_account(
    session: Session,
    account_id: int,
    *,
    expected_version: int,
    sync_running: Callable[[int], bool] | None = None,
) -> DeletionSummary:
    """Loescht ein Konto mit vollstaendiger Bereinigung (F-KTO-07, F-CON-02).

    Entfernt in einer Transaktion die Buchungen samt Notizen und Vormerkungen,
    die Import-Laeufe mit ihren Fehlern, die Auftraege, die
    Dublettenentscheidungen des Kontos sowie die kontogebundenen
    Audit-Eintraege. Kontogebundene Importvorlagen bleiben erhalten und werden
    nur entkoppelt (``account_id`` NULL). Der Bankzugang bleibt bestehen, auch
    wenn er danach kein Konto mehr hat: Zugangsdaten werden nie still
    geloescht (Entscheidung in ``decisions.md``).

    Args:
        session: Offene SQLAlchemy-Session.
        account_id: ID des zu loeschenden Kontos.
        expected_version: Bekannte Kontoversion (F-CON-02).
        sync_running: Optionale Pruefung, ob fuer den Bankzugang des Kontos
            gerade ein Sync laeuft. Liefert sie ``True``, bricht die Loeschung
            mit :class:`AccountSyncRunning` ab, ohne etwas zu aendern.

    Returns:
        Die Zeilenzahlen je bereinigter Tabelle (:class:`DeletionSummary`).

    Raises:
        AccountNotFound: Das Konto existiert nicht.
        AccountVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
        AccountSyncRunning: Fuer den Bankzugang laeuft bereits ein Sync.
    """

    checked_version = _checked_version(expected_version)
    account = get_account(session, account_id)
    _ensure_account_version(account, checked_version)

    access_id = account.bank_access_id
    if access_id is not None and sync_running is not None and sync_running(access_id):
        raise AccountSyncRunning(access_id)

    summary = _purge_account_data(session, account_id)
    # Das Konto ueber das ORM loeschen: Die Versionsspalte wirkt als zusaetzliche
    # WHERE-Bedingung, ein paralleler Schreibvorgang loest daher StaleDataError
    # aus und rollt die gesamte Bereinigung zurueck (F-CON-02).
    session.delete(account)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise AccountVersionConflict(account_id, checked_version, _current_version(session, account_id)) from exc

    return summary


# --------------------------------------------------------------------- Gruppen


def _group_summary(group: AccountGroup, members: list[Account], default_currency: str) -> GroupSummary:
    """Baut die Gruppenansicht mit Summe nach der Fremdwaehrungsregel (F-KTO-02/03)."""

    amount_cents = 0
    foreign_ids: dict[str, list[int]] = {}
    foreign_amount: dict[str, int] = {}
    member_ids: list[int] = []
    member_views: list[GroupAccount] = []
    for account in members:
        member_ids.append(account.id)
        member_views.append(GroupAccount(id=account.id, has_icon=account.icon is not None))
        if account.currency == default_currency:
            amount_cents += account.balance_cents or 0
        else:
            foreign_ids.setdefault(account.currency, []).append(account.id)
            foreign_amount[account.currency] = foreign_amount.get(account.currency, 0) + (account.balance_cents or 0)

    foreign = tuple(
        CurrencyBucket(currency=currency, amount_cents=foreign_amount[currency], account_ids=tuple(ids))
        for currency, ids in sorted(foreign_ids.items())
    )
    return GroupSummary(
        id=group.id,
        name=group.name,
        sort_order=group.sort_order,
        version=group.version,
        member_ids=tuple(member_ids),
        account_count=len(member_ids),
        currency=default_currency,
        amount_cents=amount_cents,
        foreign=foreign,
        members=tuple(member_views),
    )


def list_groups(session: Session, *, default_currency: str = DEFAULT_CURRENCY) -> list[GroupSummary]:
    """Listet Gruppen samt aktiven Mitgliedern, Kontenzahl und Summe (F-KTO-03)."""

    groups = list(session.scalars(select(AccountGroup).order_by(AccountGroup.sort_order, AccountGroup.id)))
    members_by_group: dict[int, list[Account]] = {}
    for account in list_accounts(session, archived=False):
        if account.group_id is not None:
            members_by_group.setdefault(account.group_id, []).append(account)
    return [_group_summary(group, members_by_group.get(group.id, []), default_currency) for group in groups]


def create_group(session: Session, *, name: object, sort_order: object = 0) -> AccountGroup:
    """Legt eine Gruppe an (F-KTO-03).

    Raises:
        AccountValidationError: Name oder Sortierwert sind ungueltig.
    """

    checked_name = _checked_name(name)
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise AccountValidationError("sort_order muss eine ganze Zahl sein.")
    group = AccountGroup(name=checked_name, sort_order=sort_order)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


def rename_group(session: Session, group_id: int, *, expected_version: int, name: object) -> AccountGroup:
    """Benennt eine Gruppe um mit Versionspruefung (F-KTO-03, F-CON-02).

    Raises:
        GroupNotFound: Die Gruppe existiert nicht.
        GroupVersionConflict: ``expected_version`` ist veraltet.
        AccountValidationError: Der Name ist ungueltig.
    """

    checked_version = _checked_version(expected_version)
    group = _require_group(session, group_id)
    _ensure_group_version(group, checked_version)

    group.name = _checked_name(name)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise GroupVersionConflict(group_id, checked_version, _group_current_version(session, group_id)) from exc

    session.refresh(group)
    return group


def reorder_groups(session: Session, ordered_ids: Iterable[int]) -> list[AccountGroup]:
    """Sortiert Gruppen nach einer ID-Liste (F-KTO-03).

    Die Position in der Liste bestimmt den ``sort_order`` (0-basiert). Alle IDs
    muessen existieren und eindeutig sein.

    Raises:
        AccountValidationError: Die Liste enthaelt doppelte oder falsche IDs.
        GroupNotFound: Eine ID verweist auf keine Gruppe.
    """

    ids = list(ordered_ids)
    if len(ids) != len(set(ids)):
        raise AccountValidationError("Die Reihenfolge enthaelt doppelte Gruppen-IDs.")
    # Erst alle Gruppen laden, dann setzen, damit Autoflush nicht zwischen den
    # Lookups ein unfertiges UPDATE schreibt.
    groups: list[AccountGroup] = []
    for group_id in ids:
        if not isinstance(group_id, int) or isinstance(group_id, bool):
            raise AccountValidationError("Die Reihenfolge muss Gruppen-IDs enthalten.")
        groups.append(_require_group(session, group_id))
    for position, group in enumerate(groups):
        group.sort_order = position
    session.commit()
    for group in groups:
        session.refresh(group)
    return groups


def delete_group(session: Session, group_id: int, *, expected_version: int) -> None:
    """Loescht eine Gruppe; Konten bleiben erhalten (``group_id`` wird NULL).

    Raises:
        GroupNotFound: Die Gruppe existiert nicht.
        GroupVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
    """

    checked_version = _checked_version(expected_version)
    group = _require_group(session, group_id)
    _ensure_group_version(group, checked_version)
    session.delete(group)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise GroupVersionConflict(group_id, checked_version, _group_current_version(session, group_id)) from exc


def _group_current_version(session: Session, group_id: int) -> int:
    """Liest die aktuelle Gruppenversion nach einem Rollback (fuer den 409-Stand)."""

    current = session.get(AccountGroup, group_id)
    return current.version if current is not None else -1
