"""Offline-Machbarkeitspruefung F-AUF-07 gegen die gepinnte python-fints-Version.

Belegt ohne jeden Netzzugriff (D8, keine echten Bank- oder Zugangsdaten):

- installierte und in ``pyproject.toml`` gepinnte Versionen von ``fints`` (python-fints)
  und ``cryptography``,
- Existenz und Signaturen von ``FinTS3PinTanClient``, ``NeedTANResponse``,
  ``pause_dialog``/``resume_dialog``/``deconstruct``/``from_data``,
  ``get_sepa_accounts``, ``get_balance``, ``get_transactions_xml`` und
  ``get_transactions(include_pending=...)``,
- ``init_tan_response`` als Attribut (kein Methodenname) inklusive Fundstelle im Dialog,
- Exception-Klassen und Antwortcodes fuer falsche PIN und gesperrten Zugang,
- fehlende eingebaute HKCDB-/HKCSB-Unterstuetzung und die vorhandene, oeffentliche
  Segment-Erweiterung als Umsetzungsweg fuer T-075,
- das Ergebnis-Kennzeichen der Machbarkeitspruefung in ``decisions.md``.

Anforderungen: F-AUF-07 (Machbarkeit), F-AUF-04/F-AUF-05 (Auftragsabruf bzw. Zustand
ohne Auftragsdaten), D2 (python-fints, camt/MT940), D3 (Pause/Resume des Dialogs).
"""

from __future__ import annotations

import importlib.metadata
import inspect
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import fints
import fints.dialog
from fints.client import (
    FinTS3Client,
    FinTS3PinTanClient,
    NeedRetryResponse,
    NeedTANResponse,
)
from fints.exceptions import (
    FinTSClientError,
    FinTSClientPINError,
    FinTSClientTemporaryAuthError,
    FinTSError,
)
from fints.segments.base import FinTS3Segment

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DECISIONS = REPO_ROOT / "projects" / "finanzuebersicht" / "decisions.md"

# Kennung nur fuer den Test; die echte Produkt-ID kommt aus FINTS_PRODUCT_ID (D2, SPEC Abschnitt 7).
_FAKE_PRODUCT_ID = "0000000000000000000000000"
_FAKE_SERVER = "https://example.invalid/fints"
_FAKE_BANK = "12345678"


def _make_client() -> FinTS3PinTanClient:
    """Baut einen Client ohne Netzzugriff (Konstruktor oeffnet keine Verbindung)."""
    return FinTS3PinTanClient(
        _FAKE_BANK,
        "max-mustermann",
        "12345",
        _FAKE_SERVER,
        product_id=_FAKE_PRODUCT_ID,
    )


def _declared_pin(distribution: str) -> str | None:
    """Liest die exakte Versionsvorgabe (``name==x.y.z``) aus pyproject.toml."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    for dependency in data["project"]["dependencies"]:
        if dependency.startswith(f"{distribution}=="):
            return dependency.split("==", 1)[1]
    return None


def _installed_package_sources() -> list[Path]:
    """Alle Python-Quellen der installierten fints-Distribution."""
    package_dir = Path(inspect.getfile(fints)).resolve().parent
    return sorted(package_dir.rglob("*.py"))


def test_pinned_versions_are_installed() -> None:
    """F-AUF-07: fints und cryptography sind exakt gepinnt und passen zum Lock."""
    assert _declared_pin("fints") == importlib.metadata.version("fints")
    assert _declared_pin("cryptography") == importlib.metadata.version("cryptography")
    # python-fints fuehrt die Version als Modulattribut ``version``, nicht als ``__version__``.
    assert fints.version == importlib.metadata.version("fints")


def test_pin_tan_client_exists_with_documented_signature() -> None:
    """F-AUF-07, D2: FinTS3PinTanClient existiert und nimmt die erwarteten Parameter."""
    assert issubclass(FinTS3PinTanClient, FinTS3Client)

    client_signature = inspect.signature(FinTS3PinTanClient.__init__)
    client_parameters = client_signature.parameters
    for name in ("bank_identifier", "user_id", "pin", "server", "customer_id", "tan_medium"):
        assert name in client_parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in client_parameters.values())

    base_signature = inspect.signature(FinTS3Client.__init__)
    # Ab python-fints 4 ist product_id Pflicht; from_data stellt einen Client wieder her (D3).
    assert "product_id" in base_signature.parameters
    assert "from_data" in base_signature.parameters


def test_dialog_pause_resume_and_state_api() -> None:
    """F-AUF-07, D3: Pause/Resume und Zustandssicherung sind vorhanden und benannt."""
    pause = inspect.signature(FinTS3Client.pause_dialog)
    assert list(pause.parameters) == ["self"]

    resume = inspect.signature(FinTS3Client.resume_dialog)
    assert list(resume.parameters) == ["self", "dialog_data"]

    deconstruct = inspect.signature(FinTS3Client.deconstruct)
    assert "including_private" in deconstruct.parameters

    assert issubclass(NeedTANResponse, NeedRetryResponse)
    assert hasattr(NeedRetryResponse, "get_data")
    assert isinstance(inspect.getattr_static(NeedRetryResponse, "from_data"), classmethod)
    # NeedTANResponse traegt Challenge-Text und App-Freigabe-Kennzeichen (D3, F-SYN-04).
    assert hasattr(NeedTANResponse, "get_data")
    assert NeedTANResponse.challenge is None
    assert NeedTANResponse.decoupled is None


def test_readonly_account_fetch_api_signatures() -> None:
    """F-AUF-07, D2: lesende Abrufe fuer Konten, Saldo und Umsaetze sind vorhanden."""
    assert list(inspect.signature(FinTS3Client.get_sepa_accounts).parameters) == ["self"]

    assert list(inspect.signature(FinTS3Client.get_balance).parameters) == ["self", "account"]

    xml_parameters = inspect.signature(FinTS3Client.get_transactions_xml).parameters
    assert {"self", "account", "start_date", "end_date", "supported_camt_messages"} <= set(xml_parameters)

    transactions_parameters = inspect.signature(FinTS3Client.get_transactions).parameters
    assert {"self", "account", "start_date", "end_date", "include_pending"} <= set(transactions_parameters)
    # MT940-Fallback mit Vormerkungen nur auf Anforderung (D2).
    assert transactions_parameters["include_pending"].default is False


def test_transactions_xml_splits_booked_and_pending() -> None:
    """F-AUF-07, D2: get_transactions_xml trennt gebuchte und vorgemerkte camt-Stroeme."""
    booked_statement = SimpleNamespace(camt_statements=[b"<booked/>", b"<booked2/>"])
    segment = SimpleNamespace(statement_booked=booked_statement, statement_pending=b"<pending/>")

    booked, pending = FinTS3Client._response_handler_get_transactions_xml([segment])

    assert booked == [b"<booked/>", b"<booked2/>"]
    assert pending == [b"<pending/>"]


def test_init_tan_response_is_instance_attribute() -> None:
    """F-AUF-07: init_tan_response ist vorhanden, aber ein Attribut, keine Methode."""
    client = _make_client()

    assert hasattr(client, "init_tan_response")
    assert client.init_tan_response is None
    assert not callable(client.init_tan_response)
    # Kein Methodenname in der Klasse selbst, nur Instanzzustand.
    assert "init_tan_response" not in FinTS3Client.__dict__
    # Der Dialog setzt das Attribut, wenn schon die Initialisierung eine TAN verlangt.
    dialog_source = inspect.getsource(fints.dialog)
    assert re.search(r"init_tan_response\s*=\s*NeedTANResponse", dialog_source)


def test_pin_error_classes_exist() -> None:
    """F-AUF-07: Exception-Hierarchie fuer PIN- und Sperrfaelle."""
    assert issubclass(FinTSClientPINError, FinTSClientError)
    assert issubclass(FinTSClientTemporaryAuthError, FinTSClientError)
    assert issubclass(FinTSClientError, FinTSError)


@pytest.mark.parametrize("code", ["9800", "9910", "9930", "9931", "9942"])
def test_wrong_pin_codes_raise_pin_error(code: str) -> None:
    """F-AUF-07, F-SYN-05: 9xxx-Codes vor offenem Dialog und 99xx bedeuten falsche PIN."""
    client = _make_client()
    client._bootstrap_mode = False  # noqa: SLF001 - interner Schalter der Bibliothek, nur Testzustand

    with pytest.raises(FinTSClientPINError):
        client._process_response(  # noqa: SLF001 - verhaltensnahe, offline Pruefung der Fehlerabbildung
            SimpleNamespace(open=False), None, SimpleNamespace(code=code)
        )

    assert client.pin.blocked is True


def test_locked_access_code_raises_temporary_auth_error() -> None:
    """F-AUF-07, F-SYN-05: Code 3938 (Konto gesperrt) hat eine eigene Exception."""
    client = _make_client()

    with pytest.raises(FinTSClientTemporaryAuthError):
        client._process_response(  # noqa: SLF001 - verhaltensnahe, offline Pruefung der Fehlerabbildung
            SimpleNamespace(open=True), None, SimpleNamespace(code="3938")
        )

    assert client.pin.blocked is True


def test_bpd_error_code_raises_client_error() -> None:
    """F-AUF-07, F-SYN-05: Code 9010 (BPD nicht abrufbar) ist ein Client-Fehler."""
    client = _make_client()

    with pytest.raises(FinTSClientError):
        client._process_response(  # noqa: SLF001 - verhaltensnahe, offline Pruefung der Fehlerabbildung
            SimpleNamespace(open=True), None, SimpleNamespace(code="9010")
        )


def test_no_builtin_hkcdb_hkcsb_support() -> None:
    """F-AUF-07: Die gepinnte Version enthaelt keinen fertigen HKCDB-/HKCSB-Abruf."""
    sources = _installed_package_sources()
    assert sources  # Paket ist installiert und lesbar

    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "HKCDB" not in combined
    assert "HKCSB" not in combined


def test_custom_readonly_segments_are_supported() -> None:
    """F-AUF-07: Lesende Erweiterung ueber eigene Segmente ist technisch tragfaehig."""

    class HKCDB4(FinTS3Segment):
        """Dauerauftraege (nur lesend) - Testsegment ohne Felder."""

    class HKCSB4(FinTS3Segment):
        """Terminauftraege (nur lesend) - Testsegment ohne Felder."""

    assert (HKCDB4.TYPE, HKCDB4.VERSION) == ("HKCDB", 4)
    assert (HKCSB4.TYPE, HKCSB4.VERSION) == ("HKCSB", 4)

    registered = FinTS3Segment._all_subclasses()
    assert HKCDB4 in registered
    assert HKCSB4 in registered

    # Der BPD-Abgleich waehlt die hoechste von der Bank angebotene Version der eigenen Klasse.
    requested: list[tuple[str, tuple[int, ...]]] = []

    class FakeBpd:
        def find_segment_highest_version(self, name: str, versions: object) -> object:
            requested.append((name, tuple(versions)))  # type: ignore[arg-type]
            return SimpleNamespace(header=SimpleNamespace(version=4))

        def find_segments(self, name: str) -> list[object]:
            return []

    client = _make_client()
    client.bpd = FakeBpd()  # type: ignore[assignment]

    assert client._find_highest_supported_command(HKCDB4) is HKCDB4  # noqa: SLF001
    assert client._find_highest_supported_command(HKCSB4) is HKCSB4  # noqa: SLF001
    assert requested == [("HICDBS", (4,)), ("HICSBS", (4,))]


def test_decisions_marker_is_positive() -> None:
    """F-AUF-07: decisions.md haelt das Ergebnis-Kennzeichen der Pruefung fest."""
    text = DECISIONS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if "F-AUF-07" in line]
    assert rows, "decisions.md enthaelt keinen F-AUF-07-Eintrag"

    row = rows[-1]
    assert "Ergebnis: tragfaehig" in row
    assert importlib.metadata.version("fints") in row
    assert "HKCDB" in row
    assert "HKCSB" in row
