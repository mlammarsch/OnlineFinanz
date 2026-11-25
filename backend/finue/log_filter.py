"""Log-Filter für die Maskierung von Geheimnissen.

Implementiert N-SEC-02 (keine PINs, TANs, Schlüssel, Passwörter, Token oder
Session-Daten in Logs) und N-OPS-05. Statt einer Kette von Regex-Mustern
arbeitet ``mask_text`` mit einem kleinen linearen Scanner: Er sucht Wörter, prüft
per Wortteil-Zerlegung, ob ein Wort ein sensibler Schlüssel ist, liest dahinter
Trenner und Wert und ersetzt den Wert durch ``***``.

Laufzeit: Ein Durchlauf über den Text, jede Position wird höchstens konstant oft
besucht (Zeilenenden werden zwischengespeichert).
"""

import logging
import re
from collections.abc import ItemsView, KeysView, Mapping, ValuesView
from functools import lru_cache
from typing import Any

from finue.log_constants import STANDARD_FIELDS

# Maskierungswert
MASK_VALUE = "***"

# Platzhalter, wenn ein Objekt nicht dargestellt werden kann
UNREPRESENTABLE = "[nicht darstellbar]"

# Platzhalter bei zu tiefer Verschachtelung (auch bei Zyklen)
TOO_DEEP = "[zu tief verschachtelt]"

# Sensible Wortteile (kleingeschrieben)
SENSITIVE_KEYWORDS = frozenset(
    {
        "pin",
        "tan",
        "password",
        "passwort",
        "passwd",
        "pwd",
        "pass",
        "token",
        "secret",
        "key",
        "cookie",
        "authorization",
        "session",
        "sessionid",
        "sid",
        "apikey",
    }
)

# Zusammensetzungen, die auch ohne Trenner (Substring) sensibel sind
_COMPOUNDS = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "secretkey",
    "privatekey",
    "accesskey",
    "passwd",
    "pwd",
)

# Schlüssel, deren Wert bis zum Zeilenende reicht (Schema plus Parameter)
_LINE_VALUE_KEYS = frozenset({"authorization", "cookie"})

# Authentifizierungsschemata
_SCHEMES = frozenset({"bearer", "basic", "digest", "negotiate", "ntlm"})
_BARE_SCHEMES = frozenset({"bearer", "basic"})

_MAX_DEPTH = 16

_WORD_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_EOL_RE = re.compile(r"[\r\n]")
_QUOTES = "'\""
_VALUE_STOPS = ",;)}]"
_OPENERS = "[({"
_CLOSERS = "])}"
_WS_CHARS = " \t\r\n\xa0"


@lru_cache(maxsize=4096)
def _key_parts(key: str) -> tuple[str, ...]:
    """Zerlege einen Schlüssel an Nicht-Alphanumerik- und camelCase-Grenzen."""
    return tuple(part.lower() for part in _PART_RE.findall(key))


@lru_cache(maxsize=4096)
def _is_sensitive_key(key: str) -> bool:
    """Prüfe, ob ein Schlüsselname ein Geheimnis bezeichnet.

    Sensibel ist ein Schlüssel, wenn einer seiner Wortteile in der Liste steht
    (``UserPassword``, ``X-Api-Key``, ``secretKey``) oder die ohne Trenner
    zusammengesetzte Form eine bekannte Zusammensetzung enthält
    (``accesstoken``). ``monkey``, ``spin`` oder ``sidebar`` sind es nicht.
    """
    parts = _key_parts(key)
    if any(part in SENSITIVE_KEYWORDS for part in parts):
        return True
    joined = "".join(parts)
    return any(compound in joined for compound in _COMPOUNDS)


class _Scanner:
    """Hilfsklasse mit Positionsarithmetik für ``mask_text``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self._eol = -1

    def line_end(self, i: int) -> int:
        """Index des ersten Zeilenumbruchs ab ``i`` (oder Textende), gecacht."""
        if self._eol < i:
            match = _EOL_RE.search(self.text, i)
            self._eol = match.start() if match else self.n
        return self._eol

    def skip_blanks(self, i: int) -> int:
        """Überspringe Leerzeichen und Tabs."""
        text, n = self.text, self.n
        while i < n and text[i] in " \t":
            i += 1
        return i

    def quoted_end(self, start: int) -> int:
        """Ende eines Werts in Anführungszeichen (Backslash-Escapes)."""
        text, n = self.text, self.n
        quote = text[start]
        i = start + 1
        while i < n:
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == quote:
                return i + 1
            if char in "\r\n":
                return i
            i += 1
        return n

    def escaped_quoted_end(self, start: int) -> int:
        """Ende eines Werts, der mit ``\\"`` beginnt (JSON in JSON)."""
        marker = "\\" + self.text[start + 1]
        limit = self.line_end(start)
        found = self.text.find(marker, start + 2, limit)
        return limit if found == -1 else found + 2

    def skip_ws(self, i: int) -> int:
        """Überspringe Leerzeichen, Tabs, Zeilenumbrüche und NBSP."""
        text, n = self.text, self.n
        while i < n and text[i] in _WS_CHARS:
            i += 1
        return i

    def container_end(self, start: int) -> int:
        """Ende eines Containers ab ``[``, ``(`` oder ``{`` (ganze Zeile als Grenze).

        Zählt die Klammertiefe, überspringt Strings in ``'``/``"`` samt
        Backslash-Escapes und gibt den Index hinter der passenden schließenden
        Klammer zurück. Fehlt sie, ist die Tiefe über 16 oder ein String
        unbalanciert, wird bis zum Zeilenende maskiert. Ein Durchlauf, linear.
        """
        text = self.text
        limit = self.line_end(start)
        depth = 0
        i = start
        while i < limit:
            char = text[i]
            if char in _OPENERS:
                depth += 1
                if depth > _MAX_DEPTH:
                    return limit
            elif char in _CLOSERS:
                depth -= 1
                if depth == 0:
                    return i + 1
            elif char in _QUOTES:
                j = i + 1
                while j < limit and text[j] != char:
                    j += 2 if text[j] == "\\" else 1
                if j >= limit:
                    return limit
                i = j
            i += 1
        return limit

    def plain_end(self, start: int) -> int:
        """Ende eines Werts ohne Anführungszeichen am Anfang."""
        text, n = self.text, self.n
        i = start
        while i < n:
            char = text[i]
            if char.isspace() or char in _VALUE_STOPS:
                break
            if char in _QUOTES:
                following = text[i + 1] if i + 1 < n else ""
                if following == "" or following.isspace() or following in _VALUE_STOPS:
                    break
                limit = self.line_end(i)
                closing = text.find(char, i + 1, limit)
                if closing == -1:
                    # Unbalanciertes Zeichen im Wert: sicherheitshalber bis Zeilenende
                    return limit
                i = closing + 1
                continue
            i += 1
        return i

    def value_end(self, start: int, to_line_end: bool) -> int:
        """Ende des Werts, der bei ``start`` beginnt."""
        text = self.text
        char = text[start]
        if char in _QUOTES:
            return self.quoted_end(start)
        if char == "\\" and start + 1 < self.n and text[start + 1] in _QUOTES:
            return self.escaped_quoted_end(start)
        if to_line_end:
            return self.line_end(start)
        if char in _OPENERS:
            return self.container_end(start)
        return self.plain_end(start)

    def starts_value(self, i: int) -> bool:
        """True, wenn bei ``i`` ein nichtleerer Wert beginnen kann."""
        if i >= self.n:
            return False
        char = self.text[i]
        return not (char in "\r\n" or char in _VALUE_STOPS)


def _after_key(sc: _Scanner, word_start: int, word_end: int) -> tuple[bool, int]:
    """Bestimme, ob der Schlüssel in Anführungszeichen steht, und den Index dahinter."""
    text, n = sc.text, sc.n
    if word_start > 0 and text[word_start - 1] in _QUOTES:
        k = word_end
        while k < n and text[k] == "\\":
            k += 1
        if k < n and text[k] == text[word_start - 1]:
            return True, k + 1
    return False, word_end


def _paren_before(text: str, word_start: int) -> bool:
    """True, wenn vor dem öffnenden Anführungszeichen des Schlüssels ``(`` steht."""
    k = word_start - 2
    while k >= 0 and text[k] in " \t\\":
        k -= 1
    return k >= 0 and text[k] == "("


def _separator(sc: _Scanner, i: int, quoted: bool, word_start: int) -> int:
    """Lies den Trenner ab ``i`` und gib den Index dahinter zurück, sonst -1."""
    text, n = sc.text, sc.n
    if i >= n:
        return -1
    char = text[i]
    if char == "=":
        i += 1
        if i < n and text[i] == ">":
            i += 1
        return i
    if char == ":":
        return i + 1
    if text.startswith(("%3D", "%3d"), i):
        return i + 3
    if char == "," and quoted and _paren_before(text, word_start):
        return i + 1
    return -1


def _extend_scheme(sc: _Scanner, start: int, end: int) -> int:
    """Erweitere einen Wert, der ein Schema (Bearer, Basic ...) ist, um den Token."""
    text = sc.text
    if text[start:end].lower() not in _SCHEMES:
        return end
    k = sc.skip_blanks(end)
    if k == end or not sc.starts_value(k):
        return end
    if text[start:end].lower() == "digest":
        return sc.line_end(k)
    return sc.value_end(k, False)


def mask_text(text: str) -> str:
    """Maskiere Geheimnisse im Fließtext (N-SEC-02).

    Erkennt ``schluessel=wert``, ``schluessel: wert``, ``schluessel%3Dwert``,
    dict-artige Darstellungen (``{'pin': 'x'}``, ``('pin', 'x')``), Werte in
    Anführungszeichen mit Leerzeichen und Escapes sowie Header
    (``Authorization``, ``Cookie``: Wert bis Zeilenende) und nacktes
    ``Bearer <token>`` oder ``Basic <token>``. Der Wert wird durch ``***``
    ersetzt, Schlüssel und Spacing um den Trenner bleiben erhalten.

    Args:
        text: Zu maskierender Text.

    Returns:
        Text mit maskierten Werten.
    """
    if not text:
        return text
    sc = _Scanner(text)
    out: list[str] = []
    last = 0
    consumed = 0

    for match in _WORD_RE.finditer(text):
        start, end = match.start(), match.end()
        if start < consumed:
            continue
        word = match.group()

        if not _is_sensitive_key(word):
            if word.lower() in _BARE_SCHEMES:
                k = sc.skip_blanks(end)
                if k > end and sc.starts_value(k):
                    stop = sc.value_end(k, False)
                    if stop > k:
                        out.append(text[last:k])
                        out.append(MASK_VALUE)
                        last = consumed = stop
            continue

        line_key = _key_parts(word)[-1] in _LINE_VALUE_KEYS
        quoted, i = _after_key(sc, start, end)
        i = sc.skip_blanks(i)
        after_sep = _separator(sc, sc.skip_ws(i), quoted, start)

        if after_sep == -1:
            # Header ohne Trenner: "Authorization Digest ..."
            if line_key:
                scheme = _WORD_RE.match(text, i)
                if scheme and scheme.group().lower() in _SCHEMES:
                    value_start = scheme.start()
                    stop = sc.line_end(value_start)
                    out.append(text[last:value_start])
                    out.append(MASK_VALUE)
                    last = consumed = stop
            continue

        value_start = sc.skip_ws(after_sep)
        if not sc.starts_value(value_start):
            continue
        stop = sc.value_end(value_start, line_key)
        if not line_key and text[value_start] not in _QUOTES + "\\":
            stop = _extend_scheme(sc, value_start, stop)
        if stop <= value_start:
            continue
        out.append(text[last:value_start])
        out.append(MASK_VALUE)
        last = consumed = stop

    if last == 0:
        return text
    out.append(text[last:])
    return "".join(out)


def _safe_str(obj: object) -> str:
    """str() ohne Exception, Platzhalter bei Fehler."""
    try:
        return str(obj)
    except Exception:
        return UNREPRESENTABLE


def _mask_key(key: Any) -> Any:
    """Normalisiere einen Mapping-Schlüssel zu einem serialisierbaren Typ."""
    if isinstance(key, str):
        return mask_text(key)
    if key is None or isinstance(key, (bool, int, float)):
        return key
    if isinstance(key, (bytes, bytearray)):
        return mask_text(bytes(key).decode(errors="replace"))
    return mask_text(_safe_str(key))


def _mask(value: Any, key: Any, depth: int) -> Any:
    """Rekursiver Kern von ``mask_value``."""
    if depth > _MAX_DEPTH:
        return TOO_DEEP
    if isinstance(key, str) and _is_sensitive_key(key):
        return MASK_VALUE
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, (bytes, bytearray)):
        return mask_text(bytes(value).decode(errors="replace"))
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for k, v in value.items():
            norm = _mask_key(k)
            result[norm] = _mask(v, norm, depth + 1)
        return result
    if isinstance(value, (ItemsView, KeysView, ValuesView)):
        return [_mask(item, None, depth + 1) for item in value]
    if isinstance(value, tuple):
        if len(value) == 2 and isinstance(value[0], str) and _is_sensitive_key(value[0]):
            return (mask_text(value[0]), MASK_VALUE)
        return tuple(_mask(item, None, depth + 1) for item in value)
    if isinstance(value, list):
        return [_mask(item, None, depth + 1) for item in value]
    if isinstance(value, set):
        return {_mask(item, None, depth + 1) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_mask(item, None, depth + 1) for item in value)
    return mask_text(_safe_str(value))


def mask_value(value: Any, key: str | None = None) -> Any:
    """Maskiere einen Wert rekursiv (N-SEC-02).

    - Ist ``key`` (oder ein Mapping-Schlüssel) sensibel, wird der ganze Wert
      zu ``***``.
    - str, bytes: ``mask_text``. Mapping: Schlüssel werden zu str/int/float/
      bool/None normalisiert. list, tuple, set, frozenset und dict-Sichten
      werden elementweise behandelt; ein Paar ``('password', 'x')`` wird als
      Schlüssel/Wert gelesen.
    - Andere Objekte: ``str()`` und ``mask_text``; wirft ``str()``, entsteht
      ``[nicht darstellbar]``.
    - Die Funktion wirft nie.

    Args:
        value: Zu maskierender Wert.
        key: Optionaler Schlüsselname des Werts.

    Returns:
        Maskierter Wert.
    """
    try:
        return _mask(value, key, 0)
    except Exception:
        return UNREPRESENTABLE


class LogFilter(logging.Filter):
    """Filter zum Maskieren sensibler Werte in LogRecords (N-SEC-02, N-OPS-05).

    Formatiert die Nachricht, maskiert sie, leert ``args`` und maskiert
    extra-Felder und ``exc_text``. ``filter`` wirft nie und gibt immer True
    zurück.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Maskiere den LogRecord an Ort und Stelle.

        Args:
            record: LogRecord vom logging-System.

        Returns:
            Immer True.
        """
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = _safe_str(record.msg)
            record.msg = mask_text(message)
            record.args = ()

            for field_key in list(record.__dict__):
                if field_key not in STANDARD_FIELDS:
                    record.__dict__[field_key] = mask_value(record.__dict__[field_key], field_key)

            if isinstance(record.exc_text, str):
                record.exc_text = mask_text(record.exc_text)
            if isinstance(record.stack_info, str):
                record.stack_info = mask_text(record.stack_info)
        except Exception:
            try:
                record.msg = "[Logeintrag maskiert]"
                record.args = ()
                record.exc_text = None
                record.stack_info = None
                for field_key in list(record.__dict__):
                    if field_key not in STANDARD_FIELDS:
                        del record.__dict__[field_key]
            except Exception:
                pass
        return True
