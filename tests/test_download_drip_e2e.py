"""Auditoría #118: un servidor que gotea bytes no puede bloquear la descarga ni ignorar la cancelación."""

from __future__ import annotations

import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from watcher_mcp.backends.download import DownloadCancelled, fetch

# E2E: servidor HTTPS local con CA de prueba. Las pruebas puntuales se ejecutan con -m 'not e2e'.
pytestmark = pytest.mark.e2e

trustme = pytest.importorskip("trustme")


@pytest.fixture
def drip_server(tmp_path: Path):
    """HTTPS local que anuncia 100 000 bytes y manda 1 byte cada 0,1 s (``read(256 KiB)`` esperaría horas)."""
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silencio
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                while not stop.is_set():
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert.configure_cert(ctx)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{httpd.server_address[1]}/drip.bin", str(ca_file)
    stop.set()
    httpd.shutdown()


def test_cancel_stops_a_dripping_download_promptly(tmp_path, drip_server):
    url, ca_file = drip_server
    cancel = threading.Event()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = fetch(url, tmp_path / "out.part", log=lambda m: None, cancelled=cancel.is_set,
                                      ca_bundle=ca_file, timeout_s=30)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.7)
    assert thread.is_alive(), "la descarga debería seguir en curso (el servidor gotea)"
    cancelled_at = time.monotonic()
    cancel.set()
    thread.join(timeout=5)
    # En main: el hilo seguía vivo (bloqueado en read(256 KiB)) a los 25 s, escribiendo en el .part.
    assert not thread.is_alive(), "el hilo de descarga sigue vivo tras cancelar"
    assert time.monotonic() - cancelled_at < 3
    assert isinstance(outcome.get("error"), DownloadCancelled), outcome
