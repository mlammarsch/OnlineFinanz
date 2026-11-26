"""Einheitliches Fehlerformat und Ausnahmebehandlung der API (F-CON-02).

Jede Fehlerantwort hat die Form ``{code, message, details}`` mit deutschem
Klartext. Ein Konflikt (HTTP 409) traegt zusaetzlich ``current`` mit dem
aktuellen Stand als DTO (F-CON-02). ``code`` ist eine stabile englische
Kennung, ``message`` ist deutscher Klartext, ``details`` ist ein Objekt mit
maschinenlesbaren Zusatzangaben.

Behandelt werden die eigenen :class:`ApiError`-Faelle, Validierungsfehler
(Pydantic/FastAPI, deutsch uebersetzt) und alle uebrigen HTTP-Fehler von
Starlette (404, 405). Unerwartete Ausnahmen werden als generischer
500-Fehler ohne interne Details gemeldet (N-SEC-02).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "ApiError",
    "ConflictError",
    "DomainError",
    "NotFoundError",
    "PayloadTooLargeError",
    "UnauthorizedError",
    "ValidationApiError",
    "error_body",
    "localize_validation_errors",
    "register_exception_handlers",
]

_LOGGER = logging.getLogger(__name__)

# Stabile Fehlerkennungen (englisch, Code-Bezeichner) - die Meldung ist deutsch.
CODE_NOT_FOUND: Final = "not_found"
CODE_VALIDATION: Final = "validation_error"
CODE_CONFLICT: Final = "conflict"
CODE_PAYLOAD_TOO_LARGE: Final = "payload_too_large"
CODE_UNAUTHORIZED: Final = "unauthorized"
CODE_METHOD_NOT_ALLOWED: Final = "method_not_allowed"
CODE_INTERNAL: Final = "internal_error"
CODE_DOMAIN: Final = "domain_error"

# Deutsche Klartexte fuer die haeufigsten HTTP-Status ohne eigene ApiError-Klasse.
_HTTP_STATUS_MESSAGES: Final[dict[int, tuple[str, str]]] = {
    404: (CODE_NOT_FOUND, "Die angeforderte Ressource wurde nicht gefunden."),
    405: (CODE_METHOD_NOT_ALLOWED, "Diese Methode ist fuer die Route nicht zulaessig."),
    415: (CODE_DOMAIN, "Der Inhaltstyp der Anfrage wird nicht unterstuetzt."),
}


class ApiError(Exception):
    """Basisfehler der API mit deutschem Klartext und maschinenlesbarem Code."""

    status_code: int = 500
    code: str = CODE_INTERNAL
    default_message: str = "Interner Serverfehler."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        current: BaseModel | Mapping[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message if message is not None else self.default_message
        self.details: dict[str, Any] = dict(details) if details else {}
        self.current = current
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class NotFoundError(ApiError):
    """Ressource nicht gefunden (HTTP 404)."""

    status_code = 404
    code = CODE_NOT_FOUND
    default_message = "Die angeforderte Ressource wurde nicht gefunden."


class ValidationApiError(ApiError):
    """Ungueltige Anfrage (HTTP 422)."""

    status_code = 422
    code = CODE_VALIDATION
    default_message = "Die Anfrage ist ungueltig."


class ConflictError(ApiError):
    """Optimistisches Sperren: bekannte Version passt nicht mehr (F-CON-02).

    ``current`` ist der aktuelle Stand als DTO und wird mit dem Fehler
    ausgeliefert, damit die UI beide Staende zeigen kann.
    """

    status_code = 409
    code = CODE_CONFLICT
    default_message = "Der Datensatz wurde zwischenzeitlich auf einem anderen Geraet geaendert."

    def __init__(
        self,
        message: str | None = None,
        *,
        current: BaseModel | Mapping[str, Any],
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details, current=current, code=self.code, status_code=self.status_code)


class PayloadTooLargeError(ApiError):
    """Datei oder Nutzlast ueberschreitet die konfigurierte Grenze (HTTP 413)."""

    status_code = 413
    code = CODE_PAYLOAD_TOO_LARGE
    default_message = "Die Datei ist zu gross."


class UnauthorizedError(ApiError):
    """Fehlende Anmeldung (HTTP 401); wirksam ab N-SEC-04/M10."""

    status_code = 401
    code = CODE_UNAUTHORIZED
    default_message = "Anmeldung erforderlich."


class DomainError(ApiError):
    """Fachfehler aus der Service-Schicht (HTTP 422).

    Folge-Tasks vergeben einen sprechenden ``message`` und optional einen
    eigenen ``code``; die Klasse stellt nur das einheitliche Fehlerformat.
    """

    status_code = 422
    code = CODE_DOMAIN
    default_message = "Die Aktion ist im aktuellen Zustand nicht moeglich."


def _to_jsonable(value: Any) -> Any:
    """Wandelt einen DTO-Wert in eine JSON-taugliche Darstellung."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def error_body(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    current: BaseModel | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Baut den einheitlichen Fehlerkoerper ``{code, message, details}``.

    ``current`` wird nur bei Konflikten ergaenzt (F-CON-02).
    """

    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "details": _to_jsonable(details) if details else {},
    }
    if current is not None:
        body["current"] = _to_jsonable(current)
    return body


# Deutsche Klartexte je Pydantic-Fehlertyp. Unbekannte Typen werden generisch
# gemeldet, damit kein englischer Pydantic-Text nach aussen gelangt.
_VALIDATION_MESSAGES: Final[dict[str, str]] = {
    "missing": "Feld ist erforderlich",
    "extra_forbidden": "Unbekanntes Feld",
    "int_type": "Erwartet wird eine ganze Zahl",
    "int_parsing": "Erwartet wird eine ganze Zahl",
    "float_type": "Erwartet wird eine Zahl",
    "float_parsing": "Erwartet wird eine Zahl",
    "decimal_type": "Erwartet wird eine Zahl",
    "bool_type": "Erwartet wird wahr oder falsch",
    "bool_parsing": "Erwartet wird wahr oder falsch",
    "string_type": "Erwartet wird eine Zeichenkette",
    "string_too_short": "Der Wert ist zu kurz",
    "string_too_long": "Der Wert ist zu lang",
    "string_pattern_mismatch": "Das Format ist ungueltig",
    "greater_than": "Der Wert ist zu klein",
    "greater_than_equal": "Der Wert ist zu klein",
    "less_than": "Der Wert ist zu gross",
    "less_than_equal": "Der Wert ist zu gross",
    "list_type": "Erwartet wird eine Liste",
    "dict_type": "Erwartet wird ein Objekt",
    "literal_error": "Unzulaessiger Wert",
    "enum": "Unzulaessiger Wert",
    "json_invalid": "Die Anfrage enthaelt ungueltiges JSON",
    "date_parsing": "Ungueltiges Datum",
    "date_from_datetime_parsing": "Ungueltiges Datum",
    "datetime_parsing": "Ungueltiger Zeitstempel",
    "value_error": "Ungueltiger Wert",
}

_LOCATION_PREFIXES: Final[frozenset[str]] = frozenset({"body", "query", "path", "header", "cookie"})


def _field_name(location: Sequence[Any]) -> str:
    """Baut den Feldpfad ohne Ortspraefix (``body``, ``query``, ...)."""

    parts = [str(part) for part in location]
    if parts and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    if not parts:
        return "(Koerper)"
    return ".".join(parts)


def localize_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Uebersetzt Pydantic-Validierungsfehler in deutsche Feldmeldungen.

    Ein eigener deutscher ``value_error``-Text aus einem Modell-Validator
    gewinnt gegen die generische Zuordnung.
    """

    localized: list[dict[str, str]] = []
    for error in errors:
        error_type = str(error.get("type", ""))
        message = _VALIDATION_MESSAGES.get(error_type, "Ungueltiger Wert")
        if error_type == "value_error":
            context = error.get("ctx")
            if isinstance(context, Mapping) and context.get("error"):
                message = str(context["error"])
        location = error.get("loc")
        field = _field_name(location) if isinstance(location, (list, tuple)) else "(Koerper)"
        localized.append({"field": field, "message": message})
    return localized


def register_exception_handlers(app: FastAPI) -> None:
    """Registriert die Fehlerbehandlung an der App (F-CON-02, N-SEC-02)."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details, exc.current),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body(
                CODE_VALIDATION,
                "Die Anfrage ist ungueltig.",
                {"errors": localize_validation_errors(exc.errors())},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_STATUS_MESSAGES.get(
            exc.status_code,
            (CODE_DOMAIN, "Die Anfrage konnte nicht verarbeitet werden."),
        )
        details: dict[str, Any] = {}
        if isinstance(exc.detail, str) and exc.detail and exc.detail not in {"Not Found", "Method Not Allowed"}:
            details["detail"] = exc.detail
        return JSONResponse(status_code=exc.status_code, content=error_body(code, message, details))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        _LOGGER.exception("Unerwarteter Fehler bei der Anfrageverarbeitung.")
        return JSONResponse(status_code=500, content=error_body(CODE_INTERNAL, "Interner Serverfehler.", {}))
