"""Mapping-Vorlagen fuer den Datei-Import speichern, auflisten und vorschlagen.

Implementiert F-IMP-05 (Mapping-Vorlagen speichern und pro Konto vorschlagen)
auf Basis des Modells :class:`finue.models.import_template.ImportTemplate`.
Die Vorlagen tragen das Mapping fuer F-IMP-03 sowie die CSV-Formatoptionen aus
F-IMP-02. Alle Datenzugriffe laufen ueber SQLAlchemy (N-DB-01); die Funktionen
arbeiten ausschliesslich mit dem ORM und schreiben kein rohes SQL.

Kontospezifisch und kontounabhaengig
------------------------------------
Eine Vorlage mit ``account_id`` gilt nur fuer dieses Konto, eine Vorlage mit
``account_id = NULL`` ist kontounabhaengig. :func:`list_templates` liefert fuer
ein Konto beide Arten zusammen; :func:`suggest_templates` sortiert sie nach
Passgenauigkeit: zuerst Treffer mit genau dieser ``account_id``, danach die
kontounabhaengigen Vorlagen.

Mitgelieferte Standardvorlagen
------------------------------
:func:`seed_default_templates` legt je eine Vorlage fuer N26, Easybank und
PayPal an. Diese drei Formate sind **UNBELEGT, NACH BESTEM WISSEN** aus
oeffentlich dokumentierten bzw. verbreiteten Exportbeschreibungen abgeleitet;
echte, anonymisierte Beispieldateien des Owners fehlen weiterhin (SPEC
Abschnitt 7, ``handoff.md``). Die Spaltennamen und Formatannahmen sind deshalb
bewusst an einer Stelle (:data:`_DEFAULT_TEMPLATE_SEEDS`) gebuendelt, damit sie
nach Eintreffen der Beispieldateien ohne Umbau korrigiert werden koennen.

Beleglage je Anbieter (Stand 2026-11-26):

- N26: oeffentlich mehrfach dokumentierter CSV-Export mit englischen
  Kopfzeilen ``"Booking Date"``, ``"Value Date"``, ``"Partner Name"``,
  ``"Partner Iban"``, ``"Payment Reference"``, ``"Amount (EUR)"``;
  Komma als Trennzeichen, ISO-Datum (``JJJJ-MM-TT``), Punkt als
  Dezimaltrennzeichen. Nicht gegen eine Owner-Datei geprueft.
- Easybank (AT, Kreditkarte): Die verbreitete Community-Konfiguration von
  ``bank2ynab`` beschreibt eine headerlose ``EASYBANK_Umsatzliste_*``-Datei mit
  Semikolon und ``TT.MM.JJJJ``. Da das Vorlagenmodell nur namensbasiertes
  Mapping kennt (keine Spaltenpositionen), nimmt der Seed hier vereinfachend
  eine Kopfzeile mit ``Buchungsdatum``/``Beschreibung``/``Betrag``/``Waehrung``
  an und dreht das Vorzeichen (Kreditkarte). **Unbelegt**; mit der
  Owner-Beispieldatei zu korrigieren.
- PayPal: Der Aktivitaetsbericht ist laut PayPal-Entwicklerdokumentation ein
  CSV mit u. a. ``Date``, ``Name``, ``Currency``, ``Net`` und
  ``Transaction ID``. Datums- und Zahlenformat folgen der Landeseinstellung
  (hier ``MM/TT/JJJJ`` und Punkt angenommen). Die deutschen Exporte tragen
  uebersetzte Kopfzeilen und ``TT.MM.JJJJ``; **unbelegt** bis zur
  Owner-Beispieldatei. Der optionale PayPal-Zeilenfilter aus F-IMP-08 wird
  nicht im Mapping gespeichert (das wuerde die Mapping-Engine ablehnen),
  sondern ueber :func:`template_pre_filter` an die Vorlage gebunden.

Der eigene CSV-Export wird hier bewusst **nicht** geseedet: Das
Exportformat/-profil entsteht erst in M4 nach F-EXP-03.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from finue.models.import_template import ImportTemplate

__all__ = [
    "DEFAULT_TEMPLATE_NAMES",
    "EASYBANK_TEMPLATE_NAME",
    "N26_TEMPLATE_NAME",
    "PAYPAL_PRE_FILTER",
    "PAYPAL_TEMPLATE_NAME",
    "create_template",
    "delete_template",
    "get_template",
    "list_templates",
    "seed_default_templates",
    "suggest_templates",
    "template_pre_filter",
    "update_template",
]

#: Anzeigenamen der mitgelieferten Standardvorlagen (F-IMP-05).
N26_TEMPLATE_NAME: Final = "N26 CSV"
EASYBANK_TEMPLATE_NAME: Final = "Easybank CSV"
PAYPAL_TEMPLATE_NAME: Final = "PayPal CSV"

#: Namen, die :func:`seed_default_templates` genau einmal anlegt.
DEFAULT_TEMPLATE_NAMES: Final[tuple[str, ...]] = (
    N26_TEMPLATE_NAME,
    EASYBANK_TEMPLATE_NAME,
    PAYPAL_TEMPLATE_NAME,
)

#: Kennung des PayPal-Vorverarbeitungsschritts (F-IMP-08, Umsetzung in T-057).
PAYPAL_PRE_FILTER: Final = "paypal"

_KINDS: Final[tuple[str, ...]] = ("csv", "json")
_UNSET: Final = object()


@dataclass(frozen=True, slots=True)
class _TemplateSeed:
    """Bauplan einer mitgelieferten Standardvorlage."""

    name: str
    mapping: dict[str, Any]
    delimiter: str
    encoding: str
    date_format: str
    decimal_separator: str


# UNBELEGT, NACH BESTEM WISSEN -- siehe Modul-Docstring. Aenderungen hier nach
# Eintreffen der Owner-Beispieldateien, nicht verstreut im Code.
_DEFAULT_TEMPLATE_SEEDS: Final[tuple[_TemplateSeed, ...]] = (
    _TemplateSeed(
        name=N26_TEMPLATE_NAME,
        mapping={
            "fields": {
                "booking_date": "Booking Date",
                "value_date": "Value Date",
                "amount_cents": "Amount (EUR)",
                "counterparty": "Partner Name",
                "counterparty_iban": "Partner Iban",
                "purpose": "Payment Reference",
            },
            "kind": "csv",
            "currency_default": "EUR",
        },
        delimiter=",",
        encoding="utf-8",
        date_format="JJJJ-MM-TT",
        decimal_separator="point",
    ),
    _TemplateSeed(
        name=EASYBANK_TEMPLATE_NAME,
        mapping={
            "fields": {
                "booking_date": "Buchungsdatum",
                "amount_cents": "Betrag",
                "currency": "Waehrung",
                "counterparty": "Beschreibung",
            },
            "kind": "csv",
            # Kreditkartenabrechnung: positive Betraege sind Belastungen.
            "invert_sign": True,
        },
        delimiter=";",
        encoding="utf-8",
        date_format="TT.MM.JJJJ",
        decimal_separator="comma",
    ),
    _TemplateSeed(
        name=PAYPAL_TEMPLATE_NAME,
        mapping={
            "fields": {
                "booking_date": "Date",
                "amount_cents": "Net",
                "currency": "Currency",
                "counterparty": "Name",
                "purpose": "Transaction ID",
            },
            "kind": "csv",
        },
        delimiter=",",
        encoding="utf-8",
        date_format="MM/TT/JJJJ",
        decimal_separator="point",
    ),
)

#: Vorlagenname -> Vorverarbeitungsschritt (F-IMP-08, T-057).
_DEFAULT_TEMPLATE_PRE_FILTERS: Final[dict[str, str]] = {
    PAYPAL_TEMPLATE_NAME: PAYPAL_PRE_FILTER,
}


def create_template(
    session: Session,
    *,
    name: str,
    kind: str,
    mapping: Mapping[str, Any],
    account_id: int | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    date_format: str | None = None,
    decimal_separator: str | None = None,
) -> ImportTemplate:
    """Legt eine Import-Vorlage an und committet sie (F-IMP-05).

    Args:
        session: SQLAlchemy-Session.
        name: Anzeigename der Vorlage, nicht leer.
        kind: ``"csv"`` oder ``"json"`` (F-IMP-01).
        mapping: Mapping Quellfeld -> Buchungsfeld (F-IMP-03).
        account_id: Konto-Zuordnung oder ``None`` fuer kontounabhaengig.
        delimiter: CSV-Trennzeichen (F-IMP-02), bei JSON ``None``.
        encoding: CSV-Kodierung (F-IMP-02), bei JSON ``None``.
        date_format: Datumsformat (F-IMP-02), bei JSON ``None``.
        decimal_separator: ``"comma"`` oder ``"point"`` (F-IMP-02).

    Returns:
        Die persistierte Vorlage mit gesetzter ``id``.

    Raises:
        ValueError: Bei leerem Namen, unbekanntem ``kind``, keinem
            Mapping-Objekt oder ungueltiger ``account_id``.
    """
    template = ImportTemplate(
        name=_checked_name(name),
        kind=_checked_kind(kind),
        account_id=_checked_account_id(account_id),
        mapping=_checked_mapping(mapping),
        delimiter=_optional_text(delimiter, "delimiter"),
        encoding=_optional_text(encoding, "encoding"),
        date_format=_optional_text(date_format, "date_format"),
        decimal_separator=_optional_text(decimal_separator, "decimal_separator"),
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def get_template(session: Session, template_id: int) -> ImportTemplate | None:
    """Liest eine Vorlage per ID; ``None``, wenn sie nicht existiert."""
    return session.get(ImportTemplate, _checked_template_id(template_id))


def list_templates(session: Session, account_id: int | None = None) -> list[ImportTemplate]:
    """Listet Vorlagen, fuer ein Konto inklusive der kontounabhaengigen.

    Args:
        session: SQLAlchemy-Session.
        account_id: Konto-ID oder ``None``. Mit Konto-ID werden
            kontospezifische **und** kontounabhaengige Vorlagen zusammen
            geliefert, kontospezifische zuerst. Ohne Konto-ID werden alle
            Vorlagen geliefert.

    Returns:
        Vorlagen in stabiler Reihenfolge (Passgenauigkeit, Name, ID).
    """
    if account_id is None:
        stmt = select(ImportTemplate).order_by(ImportTemplate.name, ImportTemplate.id)
        return list(session.scalars(stmt))

    checked_account_id = _checked_account_id(account_id)
    assert checked_account_id is not None  # nur zur Typ-Einengung; oben ausgeschlossen
    return _templates_for_account(session, checked_account_id)


def suggest_templates(session: Session, account_id: int) -> list[ImportTemplate]:
    """Schlaegt Vorlagen fuer ein Konto nach Passgenauigkeit vor (F-IMP-05).

    Reihenfolge: zuerst Vorlagen mit genau dieser ``account_id``, danach die
    kontounabhaengigen Vorlagen; innerhalb einer Stufe nach Name und ID.
    Vorlagen anderer Konten werden nie vorgeschlagen.

    Raises:
        ValueError: Wenn ``account_id`` fehlt oder ungueltig ist.
    """
    checked_account_id = _checked_account_id(account_id)
    if checked_account_id is None:
        raise ValueError("suggest_templates braucht eine account_id.")
    return _templates_for_account(session, checked_account_id)


def update_template(
    session: Session,
    template_id: int,
    *,
    name: str | None = None,
    kind: str | None = None,
    mapping: Mapping[str, Any] | None = None,
    account_id: object = _UNSET,
    delimiter: object = _UNSET,
    encoding: object = _UNSET,
    date_format: object = _UNSET,
    decimal_separator: object = _UNSET,
) -> ImportTemplate:
    """Aendert eine Vorlage und committet die Aenderung (F-IMP-05).

    Nicht uebergebene Felder bleiben unveraendert. Bei ``account_id``,
    ``delimiter``, ``encoding``, ``date_format`` und ``decimal_separator``
    setzt ein explizites ``None`` den Wert auf NULL; die Felder werden daher
    ueber ein internes Sentinel von "nicht uebergeben" unterschieden.

    Raises:
        LookupError: Wenn die Vorlage nicht existiert.
        ValueError: Bei ungueltigen Werten.
    """
    template = get_template(session, template_id)
    if template is None:
        raise LookupError(f"Import-Vorlage {template_id} nicht gefunden.")

    if name is not None:
        template.name = _checked_name(name)
    if kind is not None:
        template.kind = _checked_kind(kind)
    if mapping is not None:
        template.mapping = _checked_mapping(mapping)
    if account_id is not _UNSET:
        template.account_id = _checked_account_id(account_id)
    if delimiter is not _UNSET:
        template.delimiter = _optional_text(delimiter, "delimiter")
    if encoding is not _UNSET:
        template.encoding = _optional_text(encoding, "encoding")
    if date_format is not _UNSET:
        template.date_format = _optional_text(date_format, "date_format")
    if decimal_separator is not _UNSET:
        template.decimal_separator = _optional_text(decimal_separator, "decimal_separator")

    session.commit()
    session.refresh(template)
    return template


def delete_template(session: Session, template_id: int) -> bool:
    """Loescht eine Vorlage; ``False``, wenn sie nicht existiert."""
    template = get_template(session, template_id)
    if template is None:
        return False
    session.delete(template)
    session.commit()
    return True


def seed_default_templates(session: Session) -> list[ImportTemplate]:
    """Legt die mitgelieferten Standardvorlagen idempotent an (F-IMP-05).

    Legt genau drei kontounabhaengige CSV-Vorlagen an: N26, Easybank und
    PayPal. Der eigene CSV-Export wird bewusst nicht geseedet (M4/F-EXP-03).
    Vorhandene Vorlagen mit demselben Namen (kontounabhaengig) werden nicht
    angefasst, sodass ein zweiter Aufruf nichts doppelt anlegt.

    Returns:
        Nur die in diesem Aufruf neu angelegten Vorlagen; beim zweiten Aufruf
        eine leere Liste.

    Hinweis:
        Spalten und Formate sind **unbelegt, nach bestem Wissen**; siehe
        Modul-Docstring. Nach den Owner-Beispieldateien hier korrigieren.
    """
    existing_names = set(
        session.scalars(
            select(ImportTemplate.name).where(
                ImportTemplate.account_id.is_(None),
                ImportTemplate.name.in_(DEFAULT_TEMPLATE_NAMES),
            )
        )
    )

    created: list[ImportTemplate] = []
    for seed in _DEFAULT_TEMPLATE_SEEDS:
        if seed.name in existing_names:
            continue
        template = ImportTemplate(
            name=seed.name,
            kind="csv",
            account_id=None,
            mapping=copy.deepcopy(seed.mapping),
            delimiter=seed.delimiter,
            encoding=seed.encoding,
            date_format=seed.date_format,
            decimal_separator=seed.decimal_separator,
        )
        session.add(template)
        created.append(template)

    if created:
        session.commit()
        for template in created:
            session.refresh(template)
    return created


def template_pre_filter(template: ImportTemplate) -> str | None:
    """Nennt den Vorverarbeitungsschritt einer Standardvorlage (F-IMP-08).

    Die PayPal-Vorlage liefert ``"paypal"``; T-057 bindet daran
    :func:`finue.services.paypal_filter.filter_paypal_rows` an. Andere
    Vorlagen liefern ``None``. Der Wert steht nicht im Mapping, damit die
    Vorlage unveraendert an :func:`finue.services.mapping.apply_mapping`
    uebergeben werden kann.
    """
    return _DEFAULT_TEMPLATE_PRE_FILTERS.get(template.name)


def _templates_for_account(session: Session, account_id: int) -> list[ImportTemplate]:
    """Liefert kontospezifische und globale Vorlagen, spezifische zuerst."""
    specific_first = case((ImportTemplate.account_id == account_id, 0), else_=1)
    stmt = (
        select(ImportTemplate)
        .where(
            or_(
                ImportTemplate.account_id == account_id,
                ImportTemplate.account_id.is_(None),
            )
        )
        .order_by(specific_first, ImportTemplate.name, ImportTemplate.id)
    )
    return list(session.scalars(stmt))


def _checked_name(value: object) -> str:
    """Prueft einen nicht leeren Vorlagennamen."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Vorlagenname darf nicht leer sein.")
    return value.strip()


def _checked_kind(value: object) -> str:
    """Prueft das Vorlagenformat (csv oder json)."""
    if not isinstance(value, str) or value not in _KINDS:
        raise ValueError(f"Ungueltiges Vorlagenformat {value!r}; erlaubt: {', '.join(_KINDS)}.")
    return value


def _checked_mapping(value: object) -> dict[str, Any]:
    """Prueft das Mapping als JSON-faehiges Objekt mit String-Schluesseln."""
    if not isinstance(value, Mapping):
        raise ValueError("mapping muss ein Objekt (Mapping) sein.")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("mapping-Schluessel muessen nicht leere Strings sein.")
        result[key] = item
    return result


def _checked_account_id(value: object) -> int | None:
    """Prueft eine optionale Konto-ID."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("account_id muss eine positive Ganzzahl oder None sein.")
    return value


def _checked_template_id(value: object) -> int:
    """Prueft eine Vorlagen-ID."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("template_id muss eine positive Ganzzahl sein.")
    return value


def _optional_text(value: object, field: str) -> str | None:
    """Prueft einen optionalen Textwert; ``None`` bleibt ``None``."""
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{field} muss ein nicht leerer String oder None sein.")
    return value
