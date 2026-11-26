"""Tests für den Log-Filter zur Maskierung von Geheimnissen.

Implementiert N-SEC-02 und N-OPS-05. Alle Werte sind Fantasiewerte.
"""

import json
import logging
import time
from collections.abc import Iterator

import pytest

from finue.log_filter import LogFilter, _is_sensitive_key, mask_text, mask_value
from finue.logging_setup import JSONFormatter, configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Stelle Handler und Level des Root-Loggers nach jedem Test wieder her."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _record(msg: str = "m", args: object = ()) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, "t.py", 1, msg, args, None)  # type: ignore[arg-type]


class _Bad:
    def __str__(self) -> str:
        raise RuntimeError("kaputt")


MASK_CASES = [
    ("Login with pin=FANTASIE1", "Login with pin=***"),
    ("Auth password: FANTASIE2 end", "Auth password: *** end"),
    ('{"token": "F3"}', '{"token": ***}'),
    ("{'secret': 'F4'}", "{'secret': ***}"),
    ('{"pin": 123456}', '{"pin": ***}'),
    ("password='fant asie7' x", "password=*** x"),
    ('password="fant asie37" x', "password=*** x"),
    (r'{"password": "FANT\"ASIE38"}', '{"password": ***}'),
    (r"{\"pin\": \"SE K\"} x", r"{\"pin\": ***} x"),
    ("authorization=Bearer FANTASIE4 rest", "authorization=***"),
    ("Authorization: Bearer FANTASIE3 rest", "Authorization: ***"),
    ('Authorization Digest username="a", response="SEK"', "Authorization ***"),
    ("Proxy-Authorization: Basic QUJD", "Proxy-Authorization: ***"),
    ("Set-Cookie: sid=abc; Path=/", "Set-Cookie: ***"),
    ("cookie=a=1; b=2", "cookie=***"),
    ("Token is Bearer FANTASIE8 here", "Token is Bearer *** here"),
    ("got Basic dXNlcjpwYXNz ok", "got Basic *** ok"),
    ("token=Bearer FANTASIE5 rest", "token=*** rest"),
    ("password = 'a b SEK'", "password = ***"),
    ("password=a'b SEK", "password=***"),
    ("pin=\tSEK", "pin=\t***"),
    ("tan : SEK", "tan : ***"),
    ("password%3DSEK", "password%3D***"),
    ("pin=A;tan=B", "pin=***;tan=***"),
    ("pin=A tan=B", "pin=*** tan=***"),
    ("pin=A\nuser=b", "pin=***\nuser=b"),
    ("(pin=SEK)", "(pin=***)"),
    ("{'msg': 'pin=1234'}", "{'msg': 'pin=***'}"),
    ('{"pin": SEK}', '{"pin": ***}'),
    ("{'password': \"it's SEK\"}", "{'password': ***}"),
    ("{'password': 'x'}", "{'password': ***}"),
    ("('password', 'SEK')", "('password', ***)"),
    ("dict_items([('password','SEK')])", "dict_items([('password',***)])"),
    ("X-Api-Key: abc", "X-Api-Key: ***"),
    ("secretKey=abc userPassword=def", "secretKey=*** userPassword=***"),
    ("PIN=A Password=B TOKEN=C", "PIN=*** Password=*** TOKEN=***"),
    ("pwd=A passwd: B", "pwd=*** passwd: ***"),
    ("monkey=banana pin=1", "monkey=banana pin=***"),
    # T-080: passphrase, otp, jwt, csrf und Plurale
    ("passphrase=SEK-P1", "passphrase=***"),
    ("Passphrase: SEK-P2", "Passphrase: ***"),
    ("otp=123456", "otp=***"),
    ("otp_code=123456", "otp_code=***"),
    ("jwt=abc.def.ghi", "jwt=***"),
    ("csrf=SEK-C1", "csrf=***"),
    ("X-CSRF-Token: SEK-C2", "X-CSRF-Token: ***"),
    ("passwords=A tokens=B pins=C tans=D", "passwords=*** tokens=*** pins=*** tans=***"),
    ("secrets=E keys=F cookies=G sessions=H", "secrets=*** keys=*** cookies=*** sessions=***"),
    ("passphrases=I otps=J jwts=K", "passphrases=*** otps=*** jwts=***"),
    ("OTPs=L JWTs=M", "OTPs=*** JWTs=***"),
]

UNCHANGED_CASES = [
    "access_token=",
    "refresh_token:",
    "client_secret=",
    "secret_key=",
    "client_secret=, user=a",
    "monkey=banana",
    "turkey: 5",
    "spin=3",
    "keyboard=us",
    "tank=full",
    "pinned=yes",
    "passenger=2",
    "sidebar=open",
    "amount=100",
    "addresses=3",
    "statuses=2",
    "classes=4",
    "processes=5",
    "",
]


class TestMaskText:
    """N-SEC-02: mask_text maskiert Geheimnisse im Fließtext."""

    @pytest.mark.parametrize(("source", "expected"), MASK_CASES)
    def test_masks_exactly(self, source: str, expected: str) -> None:
        """N-SEC-02: Gesamtergebnis ist exakt wie erwartet."""
        assert mask_text(source) == expected

    @pytest.mark.parametrize("source", UNCHANGED_CASES)
    def test_unchanged(self, source: str) -> None:
        """N-SEC-02: Falsch-Positive und leere Werte bleiben unverändert."""
        assert mask_text(source) == source

    @pytest.mark.parametrize(("source", "_expected"), MASK_CASES)
    def test_idempotent(self, source: str, _expected: str) -> None:
        """N-SEC-02: Zweimaliges Maskieren ergibt dasselbe wie einmaliges."""
        once = mask_text(source)
        assert mask_text(once) == once

    def test_linear_runtime(self) -> None:
        """N-SEC-02: 100.000 Zeichen werden in unter 0,5 s maskiert."""
        for unit in ("pin=abc ", "pin=a'b\n", "password='", "Authorization: x\n", "k "):
            text = unit * (100_000 // len(unit))
            started = time.perf_counter()
            mask_text(text)
            assert time.perf_counter() - started < 0.5, unit


class TestSensitiveKey:
    """N-SEC-02: Schlüsselerkennung."""

    @pytest.mark.parametrize(
        "key",
        [
            "pin",
            "tan",
            "password",
            "Passwort",
            "token",
            "secret",
            "key",
            "cookie",
            "authorization",
            "access_token",
            "refresh_token",
            "client_secret",
            "secret_key",
            "access_key",
            "private_key",
            "X-Api-Key",
            "secretKey",
            "UserPassword",
            "apikey",
            "sessionid",
            "sid",
            "pwd",
            "passwd",
            "pass",
            "passphrase",
            "otp",
            "jwt",
            "csrf",
            "xsrf",
            "credentials",
            "tokens",
            "passwords",
            "pins",
            "tans",
            "secrets",
            "keys",
            "cookies",
            "sessions",
            "passphrases",
            "otps",
            "jwts",
        ],
    )
    def test_sensitive(self, key: str) -> None:
        """N-SEC-02: Diese Schlüssel sind sensibel."""
        assert _is_sensitive_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "monkey",
            "turkey",
            "spin",
            "keyboard",
            "tank",
            "pinned",
            "passenger",
            "sidebar",
            "mypin",
            "user",
            "addresses",
            "statuses",
            "classes",
            "processes",
            "monkeys",
            "turkeys",
            "tanks",
        ],
    )
    def test_not_sensitive(self, key: str) -> None:
        """N-SEC-02: Diese Schlüssel sind nicht sensibel."""
        assert _is_sensitive_key(key) is False


class TestMaskValue:
    """N-SEC-02: mask_value maskiert rekursiv."""

    def test_string(self) -> None:
        """N-SEC-02: Strings laufen durch mask_text."""
        assert mask_value("pin=F12") == "pin=***"

    def test_sensitive_key_masks_whole_value(self) -> None:
        """N-SEC-02: Sensibler Schlüssel maskiert beliebige Werte."""
        assert mask_value({"a": 1}, key="api_key") == "***"
        assert mask_value(None, key="pin") == "***"

    def test_scalars_pass(self) -> None:
        """N-SEC-02: Zahlen und None bleiben."""
        assert mask_value(5) == 5
        assert mask_value(None) is None

    def test_dict_nested(self) -> None:
        """N-SEC-02: Verschachtelte Dicts."""
        value = {"data": {"cfg": {"api_key": "F14", "n": 1}}, "user": "alice"}
        assert mask_value(value) == {"data": {"cfg": {"api_key": "***", "n": 1}}, "user": "alice"}

    def test_containers(self) -> None:
        """N-SEC-02: list, tuple, set, frozenset."""
        assert mask_value(["password=F1", "normal"]) == ["password=***", "normal"]
        assert mask_value(("password=F2",)) == ("password=***",)
        assert mask_value({"pin=F3"}) == {"pin=***"}
        assert mask_value(frozenset({"pin=F4"})) == frozenset({"pin=***"})

    def test_pair_tuple(self) -> None:
        """N-SEC-02: Paar (Schlüssel, Wert) wird als Schlüssel gelesen."""
        assert mask_value(("password", "SEK")) == ("password", "***")

    def test_bytes(self) -> None:
        """N-SEC-02: bytes werden dekodiert und maskiert."""
        assert mask_value(b"pin=F16") == "pin=***"

    def test_views(self) -> None:
        """N-SEC-02: dict_items, dict_values, dict_keys."""
        source = {"password": "SEK", "user": "pin=A"}
        assert mask_value(source.items()) == [("password", "***"), ("user", "pin=***")]
        assert mask_value(source.values()) == ["SEK", "pin=***"]
        assert mask_value(source.keys()) == ["password", "user"]

    def test_exotic_keys(self) -> None:
        """N-SEC-02: bytes-, tuple- und int-Schlüssel."""
        value = {b"password": "x", (1, 2): "y", 1: "a", "password": "b"}
        assert mask_value(value) == {"password": "***", "(1, 2)": "y", 1: "a"}

    def test_exception(self) -> None:
        """N-SEC-02: Exceptions werden als Text maskiert."""
        assert mask_value(Exception("pin=F17")) == "pin=***"

    def test_failing_str(self) -> None:
        """N-SEC-02: Objekt mit werfendem __str__."""
        assert mask_value(_Bad()) == "[nicht darstellbar]"
        assert mask_value({"k": _Bad()}) == {"k": "[nicht darstellbar]"}

    def test_cycle_does_not_raise(self) -> None:
        """N-SEC-02: Zyklen enden mit Platzhalter."""
        cyc: list[object] = []
        cyc.append(cyc)
        result = mask_value(cyc)
        while isinstance(result, list):
            result = result[0]
        assert result == "[zu tief verschachtelt]"

    def test_idempotent(self) -> None:
        """N-SEC-02: Zweimal maskieren ist gleich einmal."""
        once = mask_value({"password": "F18", "m": "pin=A", "l": [("tan", "B")]})
        assert mask_value(once) == once


class TestLogFilter:
    """N-SEC-02: LogFilter maskiert Records und wirft nie."""

    def test_returns_true_and_keeps_harmless(self) -> None:
        """N-SEC-02: Harmloser Text bleibt, Rückgabe True."""
        record = _record("Processing amount=100")
        assert LogFilter().filter(record) is True
        assert record.msg == "Processing amount=100"
        assert record.args == ()

    def test_formats_and_masks_message(self) -> None:
        """N-SEC-02: Nachricht wird formatiert, dann maskiert, args geleert."""
        record = _record("Login %s pin=%s", ("alice", "SEK"))
        LogFilter().filter(record)
        assert record.msg == "Login alice pin=***"
        assert record.args == ()

    def test_dict_args(self) -> None:
        """N-SEC-02: dict als args."""
        record = _record("Login %(user)s pin=%(pin)s", ({"user": "alice", "pin": "SEK"},))
        LogFilter().filter(record)
        assert record.msg == "Login alice pin=***"

    def test_none_args(self) -> None:
        """N-SEC-02: args=None führt zu ()."""
        record = _record("hi", None)
        LogFilter().filter(record)
        assert record.args == ()
        assert record.msg == "hi"

    def test_format_error_fallback(self) -> None:
        """N-SEC-02: Nicht formatierbare Nachricht bleibt maskiert erhalten."""
        record = _record("Message %s pin=SEK", ("a", "b"))
        assert LogFilter().filter(record) is True
        assert record.msg == "Message %s pin=***"
        assert record.args == ()

    def test_extra_fields(self) -> None:
        """N-SEC-02: extra-Felder: Schlüssel, Strings, bytes-/tuple-Schlüssel, Objekte."""
        record = _record()
        record.pin = "SEK"
        record.note = "token=SEK2"
        record.data = {b"password": "x", (1, 2): "y"}
        record.bad = _Bad()
        record.amount = 100
        assert LogFilter().filter(record) is True
        assert record.pin == "***"
        assert record.note == "token=***"
        assert record.data == {"password": "***", "(1, 2)": "y"}
        assert record.bad == "[nicht darstellbar]"
        assert record.amount == 100

    def test_exc_text(self) -> None:
        """N-SEC-02: exc_text wird maskiert."""
        record = _record()
        record.exc_text = "Traceback pin=SEK"
        LogFilter().filter(record)
        assert record.exc_text == "Traceback pin=***"

    def test_never_throws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """N-SEC-02: Auch wenn die Maskierung wirft, kommt True und nichts Rohes."""

        def boom(_text: str) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr("finue.log_filter.mask_text", boom)
        record = _record("pin=SEK")
        record.extra_field = "pin=SEK"
        assert LogFilter().filter(record) is True
        assert record.msg == "[Logeintrag maskiert]"
        assert not hasattr(record, "extra_field")


class TestConfigureLogging:
    """N-SEC-02, N-OPS-05: Integration über configure_logging."""

    def test_message_and_extra_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        """N-SEC-02: Gültiges JSON, Geheimnis nicht enthalten."""
        configure_logging("INFO")
        logging.getLogger("t_mask").info("Auth password=%s", "SEKRET1", extra={"pin": "SEKRET2", "user": "alice"})
        out = capsys.readouterr().out
        assert "SEKRET" not in out
        obj = json.loads(out)
        assert obj["message"] == "Auth password=***"
        assert obj["extra"] == {"pin": "***", "user": "alice"}
        assert obj["logger"] == "t_mask"
        assert obj["level"] == "INFO"

    def test_exotic_keys_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """N-SEC-02: bytes-/tuple-Schlüssel erzeugen gültiges JSON."""
        configure_logging("INFO")
        logging.getLogger("t_keys").info("x", extra={"data": {b"password": "SEKRET", (1, 2): "y"}})
        obj = json.loads(capsys.readouterr().out)
        assert obj["extra"] == {"data": {"password": "***", "(1, 2)": "y"}}

    def test_harmless_data_preserved(self, capsys: pytest.CaptureFixture[str]) -> None:
        """N-SEC-02: Harmlose Felder bleiben unverändert."""
        configure_logging("INFO")
        logging.getLogger("t_ok").info("Processing", extra={"user": "alice", "amount": 100})
        obj = json.loads(capsys.readouterr().out)
        assert obj["extra"] == {"user": "alice", "amount": 100}

    def test_custom_object_serialized(self, capsys: pytest.CaptureFixture[str]) -> None:
        """N-OPS-05: Beliebige Objekte werden als Text ausgegeben."""

        class Custom:
            def __str__(self) -> str:
                return "custom_object"

        configure_logging("INFO")
        logging.getLogger("t_obj").info("x", extra={"obj": Custom()})
        assert json.loads(capsys.readouterr().out)["extra"] == {"obj": "custom_object"}

    def test_handler_only_once(self) -> None:
        """N-OPS-05: Mehrfachaufruf erzeugt genau einen Handler mit einem Filter."""
        configure_logging("INFO")
        configure_logging("DEBUG")
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1
        assert [type(f) for f in handlers[0].filters] == [LogFilter]


class TestJSONFormatterFallback:
    """N-OPS-05: Der Formatter verliert keinen Eintrag."""

    def test_unserializable_extra_keeps_entry(self) -> None:
        """N-OPS-05: Tuple-Schlüssel ohne Filter ergeben Eintrag ohne extra mit Hinweis."""
        record = _record("hallo")
        record.data = {(1, 2): "y"}
        obj = json.loads(JSONFormatter().format(record))
        assert obj["message"] == "hallo"
        assert "extra" not in obj
        assert obj["extra_fehler"] == "extra nicht serialisierbar"

    def test_circular_extra_keeps_entry(self) -> None:
        """N-OPS-05: Zirkuläre Struktur wird abgefangen."""
        circ: dict[str, object] = {}
        circ["self"] = circ
        record = _record("hallo")
        record.data = circ
        obj = json.loads(JSONFormatter().format(record))
        assert obj["message"] == "hallo"
        assert obj["extra_fehler"] == "extra nicht serialisierbar"


class TestContainerValues:
    """N-SEC-02: Container-Werte hinter sensiblen Schlüsseln werden ganz maskiert."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("{'password': ['SEK1', 'SEK2']}", "{'password': ***}"),
            ("password=['SEK1','SEK2']", "password=***"),
            ("{'token': ('SEK1', 'SEK2')}", "{'token': ***}"),
            ("{'pin': {'a': 'SEK1', 'b': 'SEK2'}}", "{'pin': ***}"),
            ("password: [SEK1, SEK2]", "password: ***"),
            ("{'tan': ['it\\'s \"x\" SEK']}", "{'tan': ***}"),
            ("password=[[SEK1], (SEK2)] danach", "password=*** danach"),
            ("password=[SEK1, SEK2", "password=***"),
            ("password=['SEK1, SEK2", "password=***"),
        ],
    )
    def test_container(self, text: str, expected: str) -> None:
        result = mask_text(text)
        assert result == expected
        assert mask_text(result) == result

    def test_too_deep_masks_to_line_end(self) -> None:
        text = "password=" + "[" * 20 + "SEK" + "]" * 20 + " x\nnext"
        assert mask_text(text) == "password=***\nnext"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("pin\n=SEK", "pin\n=***"),
            ("password:\r\nSEK", "password:\r\n***"),
            ("pin\xa0=\xa0SEK", "pin\xa0=\xa0***"),
            ("pin = SEK rest", "pin = *** rest"),
            ("password=SEK mit Leerzeichen", "password=*** mit Leerzeichen"),
        ],
    )
    def test_whitespace(self, text: str, expected: str) -> None:
        assert mask_text(text) == expected

    @pytest.mark.parametrize("text", ["monkey=[a, b]", "turkey: (1, 2)", "spin={'a': 1}", "sidebar=[x]"])
    def test_false_positives_stay(self, text: str) -> None:
        assert mask_text(text) == text

    @pytest.mark.parametrize(
        "text",
        ["[" * 100000, "password=[" * 100000, "password=['" * 100000, 'password=["a\\' * 100000],
    )
    def test_linear_runtime(self, text: str) -> None:
        start = time.perf_counter()
        mask_text(text)
        assert time.perf_counter() - start < 0.5


class TestMultilineContainers:
    """N-SEC-02 (T-080): Mehrzeilige Container werden ganz maskiert."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("password=[\n 'SEK1',\n 'SEK2'\n]", "password=***"),
            ("{'token': {\n 'a': 'SEK1',\n 'b': 'SEK2'\n}}", "{'token': ***}"),
            ("pin: (\n  'SEK1',\n  'SEK2'\n) danach", "pin: *** danach"),
            ("passphrase=(\n  'SEK1',\n)", "passphrase=***"),
            ("otps=[\n  123456,\n  654321\n] ende", "otps=*** ende"),
            ("password=[\n 'SEK1',\n 'SEK2'\n", "password=***"),
        ],
    )
    def test_multiline_container(self, text: str, expected: str) -> None:
        """N-SEC-02: Container über mehrere Zeilen werden vollständig ersetzt."""
        result = mask_text(text)
        assert result == expected
        assert mask_text(result) == result

    def test_long_container_masks_rest(self) -> None:
        """N-SEC-02: Mehr als 32 Zeilen werden sicherheitshalber ganz maskiert."""
        text = "password=[\n" + "'SEK',\n" * 40 + "]"
        assert mask_text(text) == "password=***"

    def test_unbalanced_multiline_masks_rest(self) -> None:
        """N-SEC-02: Ein offener Container maskiert sicherheitshalber den Rest."""
        assert mask_text("password=[\nSEK1\nSEK2") == "password=***"
