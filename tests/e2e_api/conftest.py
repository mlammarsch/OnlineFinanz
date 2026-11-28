"""Fixtures fuer den API-E2E-Lauf gegen einen echten Uvicorn-Prozess (T-113).

Der Rahmen startet je Test einen eigenen Serverprozess mit genau einem Worker
(D3) und einem temporaeren Datenverzeichnis, wartet auf ``/api/health/ready``
und beendet den Prozess auch bei Fehlschlag sicher. Bereitgestellt werden:

- ``e2e_server``: der laufende Prozess samt Basis-URL, Datenverzeichnis und
  Logdatei (Leckage-Scan in Szenario Teil 2).
- ``api_client``: ein ``httpx.Client`` gegen denselben Server.
- ``sse_listener``: ein Hintergrund-Thread, der ``GET /api/events`` liest und
  die empfangenen Ereignisse sammelt; ``wait_for`` wartet auf einen Typ.

Der FinTS-Client ist ueber die App-Fabrik durch den Fake ersetzt (D8); es gibt
keinen Netzwerkzugriff auf Banken und keine echten Zugangsdaten (N-SEC-01/02).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config: pytest.Config) -> None:
    """Registriert den Marker ``api_e2e`` (zusaetzlich zum Eintrag in pyproject)."""

    config.addinivalue_line(
        "markers",
        "api_e2e: End-to-End-Szenario gegen einen echten Uvicorn-Prozess mit Fake-FinTS-Client",
    )


# ------------------------------------------------------------------ Server


@dataclass
class E2EServer:
    """Laufender Uvicorn-Prozess mit Adresse, Datenverzeichnis und Logdatei."""

    proc: subprocess.Popen[bytes]
    base_url: str
    data_dir: Path
    log_path: Path
    log_file: object = field(repr=False)


def _free_port() -> int:
    """Ermittelt einen freien TCP-Port auf der Loopback-Adresse."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_ready(base_url: str, proc: subprocess.Popen[bytes], *, timeout: float = 60.0) -> None:
    """Wartet, bis ``/api/health/ready`` 200 liefert oder der Prozess endet."""

    deadline = time.monotonic() + timeout
    last_error: str | None = None
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Serverprozess endete vorzeitig mit Code {proc.returncode}.")
            try:
                response = client.get(f"{base_url}/api/health/ready")
            except httpx.HTTPError as exc:
                last_error = repr(exc)
            else:
                if response.status_code == 200:
                    return
                last_error = f"Status {response.status_code}: {response.text}"
            time.sleep(0.2)
    raise RuntimeError(f"Server wurde nicht bereit (letzter Fehler: {last_error!r}).")


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    """Beendet den Serverprozess sicher, auch wenn er nicht mehr antwortet."""

    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def e2e_server(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> E2EServer:
    """Startet einen frischen Uvicorn-Prozess mit temporaeren Daten (D3, D8).

    Das Fake-FinTS-Skript ist je Test waehlbar: der Standard ist ``happy_path``
    (Szenario Teil 1, T-113) bzw. ``FINUE_E2E_SCRIPT`` aus der Umgebung; ein Test
    kann die Fixture indirekt parametrisieren (``@pytest.mark.parametrize(
    "e2e_server", ["sync_tan"], indirect=True)``), um ein anderes Skript zu
    setzen (Szenario Teil 2, T-115).
    """

    script_name = getattr(request, "param", None) or os.environ.get("FINUE_E2E_SCRIPT", "happy_path")
    data_dir = tmp_path_factory.mktemp("e2e-data")
    ui_dir = tmp_path_factory.mktemp("e2e-ui")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = ui_dir / "server.log"

    env = dict(os.environ)
    env["FINUE_E2E_DATA_DIR"] = str(data_dir)
    # Szenario Teil 1 synct nicht; der Fake bleibt dennoch verdrahtet (D8). Der
    # Szenario-Teil 2 (T-115) waehlt sein Skript ueber die indirekte
    # Parametrisierung dieser Fixture.
    env["FINUE_E2E_SCRIPT"] = script_name
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT), str(_REPO_ROOT / "backend"), env.get("PYTHONPATH", "")])
    env.setdefault("PYTHONUTF8", "1")

    log_file = log_path.open("wb")
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "--factory",
        "tests.e2e_api.app_factory:create_app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    server = E2EServer(proc=proc, base_url=base_url, data_dir=data_dir, log_path=log_path, log_file=log_file)
    try:
        _wait_for_ready(base_url, proc)
        yield server
    finally:
        _stop_process(proc)
        log_file.close()


@pytest.fixture
def api_client(e2e_server: E2EServer) -> httpx.Client:
    """Ein httpx-Client gegen den laufenden E2E-Server."""

    with httpx.Client(base_url=e2e_server.base_url, timeout=30.0) as client:
        yield client


# ------------------------------------------------------------------ SSE


class SseListener:
    """Hintergrund-Thread, der die SSE-Ereignisse des Servers sammelt (F-CON-04).

    Der Thread liest ``GET /api/events`` Zeile fuer Zeile und sammelt die
    Ereignisse als ``(type, ids)``-Tupel. ``wait_for`` wartet auf das Auftreten
    eines Typs ab einem Positionsmarker. Nur Typ und IDs werden ausgewertet;
    die Nutzlast traegt keine Geheimnisse (N-SEC-02).
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._events: list[tuple[str, dict[str, object]]] = []
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sse-listener", daemon=True)
        self._error: BaseException | None = None

    def start(self) -> None:
        """Startet den Listener-Thread."""

        self._thread.start()

    def wait_connected(self, *, timeout: float = 20.0) -> None:
        """Wartet, bis die SSE-Verbindung aufgebaut ist."""

        if not self._connected.wait(timeout):
            if self._error is not None:
                raise AssertionError(f"SSE-Listener-Fehler: {self._error!r}")
            raise AssertionError("SSE-Verbindung wurde nicht aufgebaut.")

    @property
    def count(self) -> int:
        """Anzahl der bisher empfangenen Ereignisse (Positionsmarker)."""

        with self._lock:
            return len(self._events)

    def snapshot(self) -> list[tuple[str, dict[str, object]]]:
        """Kopie aller bisher empfangenen Ereignisse (fuer Fehlermeldungen)."""

        with self._lock:
            return list(self._events)

    def wait_for(self, event_type: str, *, since: int = 0, timeout: float = 10.0) -> bool:
        """Wartet, bis ein Ereignis des Typs ab ``since`` vorliegt."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if any(kind == event_type for kind, _ in self._events[since:]):
                    return True
            if self._error is not None:
                raise AssertionError(f"SSE-Listener-Fehler: {self._error!r}")
            time.sleep(0.05)
        return False

    def types_since(self, since: int) -> list[str]:
        """Ereignistypen ab Positionsmarker ``since`` (Reihenfolge des Eingangs)."""

        with self._lock:
            return [kind for kind, _ in self._events[since:]]

    def close(self) -> None:
        """Beendet den Listener-Thread (idempotent)."""

        self._stop.set()
        self._thread.join(timeout=5)

    # -- interner Thread ---------------------------------------------------

    def _run(self) -> None:
        current_type: str | None = None
        try:
            with httpx.Client(timeout=None) as client:
                with client.stream("GET", f"{self._base_url}/api/events") as response:
                    response.raise_for_status()
                    for raw in response.iter_lines():
                        if self._stop.is_set():
                            break
                        self._connected.set()
                        if raw.startswith("event:"):
                            current_type = raw[len("event:") :].strip()
                        elif raw.startswith("data:"):
                            payload = raw[len("data:") :].strip()
                            ids: dict[str, object] = {}
                            if payload:
                                parsed = json.loads(payload)
                                if isinstance(parsed, dict):
                                    ids = {str(k): v for k, v in parsed.get("ids", {}).items()}
                            if current_type:
                                with self._lock:
                                    self._events.append((current_type, ids))
                            current_type = None
        except BaseException as exc:  # noqa: BLE001 - Fehler an den Test weiterreichen
            self._error = exc
            self._connected.set()


@pytest.fixture
def sse_listener(e2e_server: E2EServer) -> SseListener:
    """Gestarteter, verbundener SSE-Listener (wiederverwendbare Fixture)."""

    listener = SseListener(e2e_server.base_url)
    listener.start()
    try:
        listener.wait_connected()
        yield listener
    finally:
        listener.close()
