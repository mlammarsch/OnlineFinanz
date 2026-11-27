"""Exportprofile speichern, auflisten, aendern und loeschen (F-EXP-03, F-CON-02).

Ein Exportprofil buendelt die CSV-Optionen (Trennzeichen, Dezimalformat,
Datumsformat, Kodierung, Formel-Schutz) und die Spaltenauswahl/-reihenfolge des
CSV-Exports (F-EXP-03) auf Basis des Modells
:class:`finue.models.export_profile.ExportProfile`. Alle Datenzugriffe laufen
ueber SQLAlchemy (N-DB-01).

Standardprofil
--------------
:func:`seed_default_profile` legt genau ein Standardprofil "Finanzblick-
angelehnt" an. Die Spaltenauswahl ist **UNBELEGT, NACH BESTEM WISSEN** an den
Finanzblick-Export angelehnt; eine echte Beispieldatei des Owners fehlt
weiterhin (SPEC Abschnitt 7, ``handoff.md``). Die Werte buendeln sich in
:data:`DEFAULT_PROFILE_OPTIONS` und :data:`DEFAULT_PROFILE_COLUMNS`, damit sie
nach Eintreffen der Beispieldatei an einer Stelle korrigiert werden koennen.

Versionspruefung
----------------
Aenderungen und Loeschungen tragen die bekannte Version (F-CON-02). Passt sie
nicht, wirft der Service :class:`ExportProfileVersionConflict`; die API
uebersetzt das in HTTP 409 mit dem aktuellen Stand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from finue.models.export_profile import ExportProfile
from finue.services.export import (
    DEFAULT_CSV_COLUMNS,
    CsvExportOptions,
    validate_csv_columns,
    validate_csv_options,
)

__all__ = [
    "DEFAULT_PROFILE_COLUMNS",
    "DEFAULT_PROFILE_NAME",
    "DEFAULT_PROFILE_OPTIONS",
    "ExportProfileVersionConflict",
    "create_profile",
    "csv_options_from_profile",
    "delete_profile",
    "get_default_profile",
    "get_profile",
    "list_profiles",
    "seed_default_profile",
    "update_profile",
]


class ExportProfileVersionConflict(RuntimeError):
    """Die erwartete Profilversion passt nicht zum gespeicherten Stand (F-CON-02).

    Die API-Schicht uebersetzt den Fehler in HTTP 409 mit dem aktuellen Stand.
    """

    def __init__(self, profile_id: int, expected_version: int, current_version: int) -> None:
        super().__init__(f"Profil {profile_id}: erwartete Version {expected_version}, aktuell {current_version}.")
        self.profile_id = profile_id
        self.expected_version = expected_version
        self.current_version = current_version


#: Anzeigename des mitgelieferten Standardprofils (F-EXP-03).
DEFAULT_PROFILE_NAME: Final = "Finanzblick-angelehnt"

#: Formatoptionen des Standardprofils (unbelegt, siehe Modul-Docstring).
DEFAULT_PROFILE_OPTIONS: Final[dict[str, Any]] = {
    "delimiter": ";",
    "decimal_separator": "comma",
    "date_format": "TT.MM.JJJJ",
    "encoding": "utf-8",
    "guard_formulas": True,
}

#: Spalten des Standardprofils (unbelegt, siehe Modul-Docstring).
DEFAULT_PROFILE_COLUMNS: Final[tuple[str, ...]] = DEFAULT_CSV_COLUMNS

#: Sentinel fuer "nicht uebergeben" bei optionalen Aenderungsfeldern.
_UNSET: Final = object()

_OPTION_KEYS: Final[tuple[str, ...]] = ("delimiter", "decimal_separator", "date_format", "encoding", "guard_formulas")


def create_profile(
    session: Session,
    *,
    name: str,
    options: Mapping[str, Any] | None = None,
    columns: Sequence[object] | None = None,
    is_default: bool = False,
) -> ExportProfile:
    """Legt ein Exportprofil an und committet es (F-EXP-03).

    Args:
        session: SQLAlchemy-Session.
        name: Anzeigename des Profils, nicht leer und eindeutig.
        options: Formatoptionen; ``None`` setzt die Standardwerte.
        columns: Spaltenauswahl und -reihenfolge; ``None`` setzt das
            Standardprofil.
        is_default: Kennzeichnet das Profil als Standard. Es kann hoechstens
            ein Standardprofil geben; ein gesetztes Flag loescht das Flag aller
            anderen Profile.

    Returns:
        Das persistierte Profil mit gesetzter ``id``.

    Raises:
        ValueError: Bei leerem oder vergebenem Namen oder ungueltigen Optionen.
    """

    checked_name = _checked_name(name)
    profile = ExportProfile(
        name=checked_name,
        is_default=bool(is_default),
        options=_options_payload(options),
        columns=list(_columns_payload(columns)),
    )
    if profile.is_default:
        _clear_default(session)
    session.add(profile)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError(f"Der Profilname {checked_name!r} ist bereits vergeben.") from exc
    session.refresh(profile)
    return profile


def get_profile(session: Session, profile_id: int) -> ExportProfile | None:
    """Liest ein Profil per ID; ``None``, wenn es nicht existiert."""

    return session.get(ExportProfile, _checked_profile_id(profile_id))


def list_profiles(session: Session) -> list[ExportProfile]:
    """Listet alle Profile, Standard zuerst, dann nach Name und ID (F-EXP-03)."""

    stmt = select(ExportProfile).order_by(ExportProfile.is_default.desc(), ExportProfile.name, ExportProfile.id)
    return list(session.scalars(stmt))


def get_default_profile(session: Session) -> ExportProfile | None:
    """Liefert das Standardprofil oder ``None`` (F-EXP-03)."""

    stmt = select(ExportProfile).where(ExportProfile.is_default.is_(True)).order_by(ExportProfile.id).limit(1)
    return session.scalars(stmt).first()


def update_profile(
    session: Session,
    profile_id: int,
    *,
    name: str | None = None,
    options: object = _UNSET,
    columns: object = _UNSET,
    is_default: object = _UNSET,
    expected_version: int | None = None,
) -> ExportProfile:
    """Aendert ein Exportprofil und committet die Aenderung (F-EXP-03, F-CON-02).

    Nicht uebergebene Felder bleiben unveraendert. ``options`` und ``columns``
    ersetzen den jeweiligen Stand vollstaendig (Setz-Semantik).

    Raises:
        LookupError: Wenn das Profil nicht existiert.
        ExportProfileVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
        ValueError: Bei ungueltigen Werten oder vergebenem Namen.
    """

    profile = get_profile(session, profile_id)
    if profile is None:
        raise LookupError(f"Exportprofil {profile_id} nicht gefunden.")

    checked_version = _checked_expected_version(expected_version)
    if checked_version is not None and profile.version != checked_version:
        raise ExportProfileVersionConflict(profile_id, checked_version, profile.version)

    if name is not None:
        profile.name = _checked_name(name)
    if options is not _UNSET:
        profile.options = _options_payload(options)
    if columns is not _UNSET:
        profile.columns = list(_columns_payload(columns))
    if is_default is not _UNSET:
        if not isinstance(is_default, bool):
            raise ValueError("is_default muss true oder false sein.")
        if is_default:
            _clear_default(session, keep_id=profile_id)
        profile.is_default = is_default

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        current = session.get(ExportProfile, profile_id)
        current_version = current.version if current is not None else -1
        if _is_name_conflict(exc):
            raise ValueError(f"Der Profilname {profile.name!r} ist bereits vergeben.") from exc
        raise ExportProfileVersionConflict(profile_id, checked_version or -1, current_version) from exc
    except StaleDataError as exc:
        session.rollback()
        current = session.get(ExportProfile, profile_id)
        current_version = current.version if current is not None else -1
        raise ExportProfileVersionConflict(profile_id, checked_version or -1, current_version) from exc

    session.refresh(profile)
    return profile


def delete_profile(session: Session, profile_id: int, *, expected_version: int | None = None) -> bool:
    """Loescht ein Exportprofil; ``False``, wenn es nicht existiert (F-EXP-03, F-CON-02).

    Raises:
        ExportProfileVersionConflict: ``expected_version`` ist veraltet (F-CON-02).
    """

    profile = get_profile(session, profile_id)
    if profile is None:
        return False

    checked_version = _checked_expected_version(expected_version)
    if checked_version is not None and profile.version != checked_version:
        raise ExportProfileVersionConflict(profile_id, checked_version, profile.version)

    session.delete(profile)
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        current = session.get(ExportProfile, profile_id)
        current_version = current.version if current is not None else -1
        raise ExportProfileVersionConflict(profile_id, checked_version or -1, current_version) from exc
    return True


def seed_default_profile(session: Session) -> ExportProfile:
    """Legt das Standardprofil idempotent an (F-EXP-03).

    Gibt es bereits ein Standardprofil, wird es zurueckgegeben. Sonst wird ein
    Profil mit :data:`DEFAULT_PROFILE_NAME` erzeugt (oder als Standard
    gekennzeichnet, falls der Name schon ohne Flag existiert). So gibt es
    hoechstens ein Standardprofil.
    """

    existing = get_default_profile(session)
    if existing is not None:
        return existing

    by_name = session.scalars(select(ExportProfile).where(ExportProfile.name == DEFAULT_PROFILE_NAME)).first()
    if by_name is not None:
        by_name.is_default = True
        session.commit()
        session.refresh(by_name)
        return by_name

    return create_profile(
        session,
        name=DEFAULT_PROFILE_NAME,
        options=DEFAULT_PROFILE_OPTIONS,
        columns=DEFAULT_PROFILE_COLUMNS,
        is_default=True,
    )


def csv_options_from_profile(profile: ExportProfile) -> CsvExportOptions:
    """Uebersetzt ein Profil in validierte CSV-Optionen (F-EXP-03).

    Raises:
        ExportValidationError: Wenn die gespeicherten Werte ungueltig sind
            (kann bei geprueften Schreibzugriffen nicht eintreten).
    """

    options: dict[str, Any] = dict(profile.options or {})
    options["columns"] = list(profile.columns or list(DEFAULT_PROFILE_COLUMNS))
    return validate_csv_options(options)


def _options_payload(options: object) -> dict[str, Any]:
    """Validiert die Formatoptionen und liefert das Speicherformat (F-EXP-03)."""

    if options is _UNSET or options is None:
        checked = validate_csv_options(None)
    else:
        if not isinstance(options, Mapping):
            raise ValueError("options muss ein Objekt sein.")
        without_columns = {key: value for key, value in options.items() if key != "columns"}
        checked = validate_csv_options(without_columns)
    return {key: getattr(checked, key) for key in _OPTION_KEYS}


def _columns_payload(columns: object) -> tuple[str, ...]:
    """Validiert die Spaltenauswahl (F-EXP-03)."""

    if columns is _UNSET or columns is None:
        return validate_csv_columns(DEFAULT_PROFILE_COLUMNS)
    return validate_csv_columns(columns)


def _clear_default(session: Session, *, keep_id: int | None = None) -> None:
    """Entfernt das Standardkennzeichen aller anderen Profile (F-EXP-03)."""

    stmt = update(ExportProfile).where(ExportProfile.is_default.is_(True))
    if keep_id is not None:
        stmt = stmt.where(ExportProfile.id != keep_id)
    session.execute(stmt.values(is_default=False))


def _is_name_conflict(exc: IntegrityError) -> bool:
    """True, wenn der Integritaetsfehler den eindeutigen Profilnamen betrifft."""

    return "uq_export_profile_name" in str(exc.orig) or "UNIQUE" in str(exc.orig).upper()


def _checked_name(value: object) -> str:
    """Prueft einen nicht leeren Profilnamen."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("Profilname darf nicht leer sein.")
    return value.strip()


def _checked_profile_id(value: object) -> int:
    """Prueft eine Profil-ID."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("profile_id muss eine positive Ganzzahl sein.")
    return value


def _checked_expected_version(value: int | None) -> int | None:
    """Prueft eine optionale erwartete Version (F-CON-02)."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_version muss eine positive Ganzzahl oder None sein.")
    return value
