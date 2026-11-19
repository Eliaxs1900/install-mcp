"""CLI y arranque de punta a punta: procesos reales de ``watcher-mcp`` por stdio y por consola."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from watcher_mcp import doctor
from watcher_mcp.config import Config
from watcher_mcp.server import create_server

from .test_cli import GRANDCHILD, PY, wait_dead

# E2E: lanza procesos reales de watcher-mcp. Las pruebas puntuales se ejecutan con -m 'not e2e'.
pytestmark = pytest.mark.e2e

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


def stdio_session(cmd: list[str], env: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Lanza el comando como servidor MCP, hace initialize + tools/list y devuelve (initialize, tools/list, stderr)."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, **(env or {})})
    assert proc.stdin and proc.stdout

    def call(message: dict[str, Any]) -> dict[str, Any]:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            assert line, "el servidor cerró stdout"
            reply = json.loads(line)
            if reply.get("id") == message["id"]:
                return reply

    try:
        init = call(INITIALIZE)
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    finally:
        proc.stdin.close()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return init, tools, proc.stderr.read() if proc.stderr else ""


def test_no_arguments_still_starts_the_stdio_server(tmp_path):
    init, tools, _ = stdio_session([sys.executable, "-m", "watcher_mcp"], {"WATCHER_MCP_STATE_DIR": str(tmp_path)})
    assert init["result"]["serverInfo"]["name"] == "watcher-mcp"
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"install", "status", "logs", "list_jobs", "wait", "cancel", "backends"} <= names


def test_config_and_state_dir_flags_still_start_the_server_as_registered_before(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'allowed_roots = ["{tmp_path}"]\n')
    state = tmp_path / "estado"
    init, _, _ = stdio_session([sys.executable, "-m", "watcher_mcp", "--config", str(cfg), "--state-dir", str(state)])
    assert init["result"]["serverInfo"]["name"] == "watcher-mcp"
    assert (state / "instances").is_dir()  # usó el state_dir pedido


def test_install_mcp_alias_warns_on_stderr_and_serves(tmp_path):
    code = "from watcher_mcp.__main__ import legacy_main; legacy_main()"
    init, tools, err = stdio_session([sys.executable, "-c", code], {"WATCHER_MCP_STATE_DIR": str(tmp_path)})
    assert init["result"]["serverInfo"]["name"] == "watcher-mcp" and tools["result"]["tools"]
    assert "obsoleto" in err and "claude mcp add --scope user watcher-mcp --" in err


def test_doctor_launches_the_server_and_gets_initialize(tmp_path):
    finding = doctor.check_server(None, timeout_s=30)
    assert finding.level == "ok" and "initialize" in finding.message


def test_doctor_reports_a_server_that_cannot_start(tmp_path):
    cfg = tmp_path / "roto.toml"
    cfg.write_text("[[[")
    finding = doctor.check_server(cfg, timeout_s=30)
    assert finding.level == "error" and "sin responder" in finding.message
    assert finding.fix and "watcher-mcp" in finding.fix


def test_doctor_end_to_end_exit_codes(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'allowed_roots = ["{tmp_path}"]\nstate_dir = "{tmp_path / "s"}"\n')
    env = {**os.environ, "HOME": str(tmp_path)}
    cmd = [sys.executable, "-m", "watcher_mcp", "doctor", "--config", str(cfg), "--no-clients"]
    good = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    assert good.returncode == 0, good.stdout
    assert "responde a initialize" in good.stdout
    cfg.write_text("[[[")
    bad = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    assert bad.returncode == 1 and "TOML no válido" in bad.stdout


def cli_process(state: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-m", "watcher_mcp", "run", "--state-dir", str(state), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for_file(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not (path.exists() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"no apareció {path}"
        time.sleep(0.05)


def test_ctrl_c_cancels_the_job_cleanly_and_kills_the_group(tmp_path):
    state, pidfile = tmp_path / "state", tmp_path / "gc.pid"
    proc = cli_process(state, "--", PY, "-c", GRANDCHILD, str(pidfile))
    wait_for_file(pidfile)
    grandchild = int(pidfile.read_text())
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 130, err
    assert "cancelando" in err and "cancelado tras" in err
    assert wait_dead(grandchild)
    (job_dir,) = (state / "jobs").iterdir()
    assert json.loads((job_dir / "job.json").read_text())["state"] == "cancelled"


def test_sigterm_to_the_cli_also_cancels(tmp_path):
    state, pidfile = tmp_path / "state", tmp_path / "gc.pid"
    proc = cli_process(state, "--", PY, "-c", GRANDCHILD, str(pidfile))
    wait_for_file(pidfile)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 130 and wait_dead(int(pidfile.read_text()))


def test_second_ctrl_c_forces_sigkill_immediately(tmp_path):
    # Un hijo que ignora SIGTERM: la cancelación ordenada tardaría ~5 s en escalar a SIGKILL.
    state, pidfile = tmp_path / "state", tmp_path / "pid"
    stubborn = ("import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "open(os.environ['PIDFILE'], 'w').write(str(os.getpid()))\ntime.sleep(120)\n")
    proc = subprocess.Popen([sys.executable, "-m", "watcher_mcp", "run", "-q", "--state-dir", str(state), "--", PY, "-c", stubborn],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "PIDFILE": str(pidfile)})
    wait_for_file(pidfile)
    pid = int(pidfile.read_text())
    started = time.monotonic()
    proc.send_signal(signal.SIGINT)
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert time.monotonic() - started < 4.5  # sin esperar la gracia de SIGTERM
    assert wait_dead(pid)


async def test_a_cli_job_is_visible_from_the_mcp_server(tmp_path):
    """El job lanzado por la CLI se consulta luego con status/logs del servidor MCP sobre el mismo state_dir."""
    state = tmp_path / "state"
    done = subprocess.run(
        [sys.executable, "-m", "watcher_mcp", "run", "-q", "--state-dir", str(state), "--", PY, "-c",
         "print('desde-la-cli'); raise SystemExit(0)"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    (job_id,) = [p.name for p in (state / "jobs").iterdir()]

    config = Config(state_dir=state, allowed_roots=[tmp_path])
    mcp, manager = create_server(config)
    try:
        async with Client(mcp, mode="legacy") as client:
            status = (await client.call_tool("status", {"job_id": job_id})).structured_content
            assert status["state"] == "succeeded" and status["report"]["ok"] is True
            logs = (await client.call_tool("logs", {"job_id": job_id, "tail": 20})).structured_content
            assert any("desde-la-cli" in line for line in logs["lines"])
            listed = (await client.call_tool("list_jobs", {})).structured_content
            rows = listed.get("result", listed) if isinstance(listed, dict) else listed
            assert job_id in [row["job_id"] for row in rows]
    finally:
        await manager.shutdown(timeout_s=5)
