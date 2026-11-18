"""Integración sin red: descargas por HTTPS local, binarios, git con repos locales y run."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from watcher_mcp.backends import BUILTIN
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager, JobState

trustme = pytest.importorskip("trustme")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def targz(files: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, (data, mode) in files.items():
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), mode
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class Routes(dict):
    pass


@pytest.fixture
def https(tmp_path):
    """Servidor HTTPS local con CA de prueba. Rutas: path -> bytes | ('redirect', url) | ('slow', bytes, s)."""
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    routes = Routes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silencio
            pass

        def do_GET(self):  # noqa: N802
            r = routes.get(self.path.split("?")[0])
            if r is None:
                self.send_response(404)
                self.end_headers()
                return
            if isinstance(r, tuple) and r[0] == "redirect":
                self.send_response(302)
                self.send_header("Location", r[1])
                self.end_headers()
                return
            if isinstance(r, tuple) and r[0] == "slow":
                _, data, pause = r
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data[:1000])
                    self.wfile.flush()
                    time.sleep(pause)
                    self.wfile.write(data[1000:])
                except OSError:
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(r)))
            self.end_headers()
            self.wfile.write(r)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert.configure_cert(ctx)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"https://127.0.0.1:{httpd.server_address[1]}"
    yield base, routes, ca_file
    httpd.shutdown()


@pytest.fixture
async def mgr(tmp_path, https):
    _, _, ca_file = https
    cfg = Config(
        state_dir=tmp_path / "state",
        allowed_backends=list(BUILTIN),
        allowed_roots=[tmp_path],
        backends={"download": {"ca_bundle": str(ca_file), "read_timeout_s": 3},
                  "run": {"programs": [Path(sys.executable).name]}},
    )
    m = JobManager(cfg, BUILTIN)
    yield m
    await m.shutdown(timeout_s=5)


async def finish(m: JobManager, job, timeout: float = 30):
    assert await m.wait(job.id, timeout), [ln.format() for ln in job.tail(30)]
    return job


# ------------------------------------------------------------------ download
async def test_download_with_checksum(mgr, https, tmp_path):
    base, routes, _ = https
    data = os.urandom(3 * 1024 * 1024)
    routes["/files/data.bin"] = data
    dest = tmp_path / "dl"
    dest.mkdir()
    job = await finish(mgr, await mgr.submit("download", {"url": f"{base}/files/data.bin?token=abc123secret",
                                                          "dest": str(dest), "checksum": sha(data)}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert (dest / "data.bin").read_bytes() == data and not list(dest.glob(".*part"))
    log = job.log_path.read_text()
    assert "checksum sha256 correcto" in log and "abc123secret" not in log
    assert "data.bin" in job.installed and "3.0 MiB" in job.installed["data.bin"]


async def test_download_bad_checksum_leaves_nothing(mgr, https, tmp_path):
    base, routes, _ = https
    routes["/x.bin"] = b"hola" * 100
    job = await finish(mgr, await mgr.submit("download", {"url": f"{base}/x.bin", "dest": str(tmp_path / "x.bin"),
                                                          "checksum": "sha256:" + "0" * 64}))
    assert job.state == JobState.FAILED and "checksum" in job.error
    assert not (tmp_path / "x.bin").exists() and not list(tmp_path.glob(".x.bin*"))


async def test_download_extract_strip_and_reject_traversal(mgr, https, tmp_path):
    base, routes, _ = https
    routes["/pkg.tar.gz"] = targz({"pkg-1.0/bin/tool": (b"#!/bin/sh\n", 0o755), "pkg-1.0/README": (b"hi", 0o644)})
    out = tmp_path / "pkg"
    job = await finish(mgr, await mgr.submit("download", {"url": f"{base}/pkg.tar.gz", "dest": str(out),
                                                          "extract": True, "strip_components": 1}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert (out / "bin" / "tool").read_bytes() == b"#!/bin/sh\n" and os.access(out / "bin" / "tool", os.X_OK)

    routes["/evil.tar.gz"] = targz({"../../escaped": (b"x", 0o644)})
    job = await finish(mgr, await mgr.submit("download", {"url": f"{base}/evil.tar.gz", "dest": str(tmp_path / "e"),
                                                          "extract": True}))
    assert job.state == JobState.FAILED and "insegura" in job.error
    assert not (tmp_path.parent / "escaped").exists()


async def test_download_rejects_http_and_http_redirect(mgr, https, tmp_path):
    base, routes, _ = https
    with pytest.raises(ValueError, match="https"):
        await mgr.submit("download", {"url": "http://example.org/x", "dest": str(tmp_path / "x")})
    routes["/r"] = ("redirect", "http://example.org/x")
    job = await finish(mgr, await mgr.submit("download", {"url": f"{base}/r", "dest": str(tmp_path / "r")}))
    assert job.state == JobState.FAILED and "redirección rechazada" in job.error


async def test_download_stall_then_cancel(mgr, https, tmp_path):
    base, routes, _ = https
    routes["/slow.bin"] = ("slow", b"x" * 5000, 2.5)
    events = []

    async def listener(ev):
        events.append(ev.kind)

    mgr.listeners.append(listener)
    job = await mgr.submit("download", {"url": f"{base}/slow.bin", "dest": str(tmp_path / "slow.bin"),
                                        "stall_timeout_s": 0.5})
    t0 = time.monotonic()
    while job.state != JobState.STALLED:
        assert time.monotonic() - t0 < 5
        await asyncio.sleep(0.05)
    await mgr.cancel(job.id)
    await finish(mgr, job, 10)
    assert job.state == JobState.CANCELLED and "stalled" in events
    assert not (tmp_path / "slow.bin").exists()


# ------------------------------------------------------------------ binary
async def test_binary_from_archive_member(mgr, https, tmp_path):
    base, routes, _ = https
    script = b"#!/bin/sh\necho mytool 1.2.3\n"
    archive = targz({"mytool-1.2.3/mytool": (script, 0o644), "mytool-1.2.3/LICENSE": (b"MIT", 0o644)})
    routes["/mytool.tar.gz"] = archive
    bin_dir = tmp_path / "bin"
    job = await finish(mgr, await mgr.submit("binary", {
        "url": f"{base}/mytool.tar.gz", "checksum": sha(archive), "name": "mytool",
        "member": "mytool-1.2.3/mytool", "bin_dir": str(bin_dir)}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert os.access(bin_dir / "mytool", os.X_OK) and sorted(p.name for p in bin_dir.iterdir()) == ["mytool"]
    assert job.installed == {"mytool": "mytool 1.2.3"}
    with pytest.raises(ValueError, match="checksum|Field required"):
        await mgr.submit("binary", {"url": f"{base}/mytool.tar.gz", "name": "mytool"})


# ------------------------------------------------------------------ git
def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.mark.skipif(not shutil.which("git"), reason="git no instalado")
async def test_git_clone_update_and_fail_fast(mgr, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "a.txt").write_text("1")
    _git("add", ".", cwd=origin)
    _git("commit", "-q", "-m", "uno", cwd=origin)
    _git("tag", "v1.0", cwd=origin)
    dest = tmp_path / "clone"
    job = await finish(mgr, await mgr.submit("git", {"url": str(origin), "dest": str(dest)}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert (dest / "a.txt").read_text() == "1"
    assert job.installed["clone"].startswith("v1.0")

    (origin / "a.txt").write_text("2")
    _git("commit", "-q", "-am", "dos", cwd=origin)
    job = await finish(mgr, await mgr.submit("git", {"url": str(origin), "dest": str(dest)}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert [s.name for s in job.steps] == ["fetch", "merge"] and (dest / "a.txt").read_text() == "2"

    # Sin servidor: falla enseguida, nunca se queda esperando credenciales.
    t0 = time.monotonic()
    job = await finish(mgr, await mgr.submit("git", {"url": "https://127.0.0.1:9/o/r.git",
                                                     "dest": str(tmp_path / "nope")}), 60)
    assert job.state == JobState.FAILED and time.monotonic() - t0 < 30


# ------------------------------------------------------------------ run
async def test_run_allowlisted_program(mgr, tmp_path):
    prog = Path(sys.executable).name
    job = await finish(mgr, await mgr.submit("run", {
        "program": prog, "args": ["-c", "import os; print('hola', os.environ['MY_TOKEN'][:3])"],
        "cwd": str(tmp_path), "env": {"MY_TOKEN": "tok-very-secret"}}))
    assert job.state == JobState.SUCCEEDED, job.error
    text = job.log_path.read_text()
    assert "hola tok" in text and "tok-very-secret" not in text
    job = await finish(mgr, await mgr.submit("run", {"program": prog, "args": ["-c", "raise SystemExit(3)"],
                                                     "cwd": str(tmp_path), "ok_exit_codes": [0, 3]}))
    assert job.state == JobState.SUCCEEDED
