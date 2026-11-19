"""process.py: líneas largas, tope en las sondas, hijos que escapan del grupo y entorno de los jobs."""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from watcher_mcp import process
from watcher_mcp.process import MAX_LINE, SERVER_ONLY_ENV, build_env, run_capture, run_command


def alive(pid: int) -> bool:
    """Vivo y no zombi (en un contenedor sin init los huérfanos muertos quedan como zombis)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


async def wait_dead(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        await asyncio.sleep(0.1)
    return not alive(pid)


async def run(argv: list[str], **kw) -> tuple[process.ProcResult, list[tuple[str, str]]]:
    lines: list[tuple[str, str]] = []
    res = await run_command(
        argv, env=build_env({}), cwd=None, on_line=lambda s, t: lines.append((s, t)),
        cancel_event=kw.pop("cancel_event", asyncio.Event()), timeout_s=kw.pop("timeout_s", 30), **kw,
    )
    return res, lines


# --- #103: líneas de más de 8 KiB -----------------------------------------------------------------
@pytest.mark.parametrize("io_mode", ["pipe", "pty"])
async def test_long_line_with_newline_is_chunked_not_truncated(io_mode: str):
    code = "print('X' * 20000)"
    res, lines = await run([sys.executable, "-c", code], io_mode=io_mode)
    got = [t for s, t in lines if s == "stdout"]
    assert res.exit_code == 0
    assert "".join(got) == "X" * 20000
    assert [len(t) for t in got] == [MAX_LINE, MAX_LINE, 20000 - 2 * MAX_LINE]


async def test_long_line_without_newline_is_chunked_not_dropped():
    code = "import sys; sys.stdout.write('Y' * 20000)"
    _, lines = await run([sys.executable, "-c", code])
    got = [t for s, t in lines if s == "stdout"]
    assert "".join(got) == "Y" * 20000
    assert all(len(t) <= MAX_LINE for t in got)


async def test_many_chunks_in_one_read_and_stderr():
    code = "import sys; sys.stderr.write('E' * 70000 + '\\n'); print('ok')"
    _, lines = await run([sys.executable, "-c", code])
    err = "".join(t for s, t in lines if s == "stderr")
    assert err == "E" * 70000
    assert ("stdout", "ok") in lines


# --- #132: tope de salida en run_capture ----------------------------------------------------------
FLOOD = (
    "import os, sys\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "while True:\n"
    "    sys.{stream}.write('x' * 65536); sys.{stream}.flush()\n"
)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_run_capture_caps_output_and_kills(tmp_path: Path, stream: str):
    pidfile = tmp_path / "pid"
    t0 = time.monotonic()
    code, out, err = await run_capture(
        [sys.executable, "-c", FLOOD.format(stream=stream), str(pidfile)], env=build_env({}), timeout_s=30,
    )
    assert time.monotonic() - t0 < 10  # corta al pasar el tope, no espera al timeout
    assert code == -1 and "límite" in err
    assert len(out) <= process.MAX_CAPTURE_BYTES and len(err) < 1000
    assert await wait_dead(int(pidfile.read_text()))


async def test_run_capture_small_output_and_exit_code_unchanged():
    code, out, err = await run_capture(
        [sys.executable, "-c", "import sys; print('1.2.3'); print('aviso', file=sys.stderr); sys.exit(3)"],
        env=build_env({}),
    )
    assert (code, out.strip(), err.strip()) == (3, "1.2.3", "aviso")


async def test_run_capture_timeout_still_works():
    t0 = time.monotonic()
    code, _, err = await run_capture([sys.executable, "-c", "import time; time.sleep(60)"], env=build_env({}),
                                     timeout_s=0.5)
    assert code == -1 and "timeout" in err and time.monotonic() - t0 < 8


# --- #120: hijos que escapan del grupo y ruido de asyncio ------------------------------------------
ESCAPE = (
    "import subprocess, sys, time\n"
    "c = subprocess.Popen(['sleep', '317'], start_new_session=True)\n"
    "open(sys.argv[1], 'w').write(str(c.pid))\n"
    "time.sleep(300)\n"
)


@pytest.fixture
def fast_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "TERM_GRACE_S", 1.0)
    monkeypatch.setattr(process, "KILL_GRACE_S", 2.0)
    monkeypatch.setattr(process, "DRAIN_S", 1.0)


@pytest.mark.parametrize("how", ["cancel", "timeout"])
async def test_setsid_child_is_killed_on_cancel_or_timeout(tmp_path: Path, fast_grace, how: str):
    pidfile = tmp_path / "child.pid"
    cancel = asyncio.Event()
    task = asyncio.create_task(run(
        [sys.executable, "-c", ESCAPE, str(pidfile)], cancel_event=cancel, timeout_s=2.0 if how == "timeout" else 60,
    ))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    child = int(pidfile.read_text())
    assert alive(child)
    if how == "cancel":
        await asyncio.sleep(0.3)
        cancel.set()
    res, lines = await asyncio.wait_for(task, 20)
    assert res.cancelled if how == "cancel" else res.timed_out
    assert await wait_dead(child), "el hijo que hizo setsid no debe sobrevivir a cancel/timeout"
    assert res.survivors == []
    assert any("salido del grupo" in t and str(child) in t for s, t in lines if s == "core"), lines


async def test_escapee_survivor_is_reported(tmp_path: Path, fast_grace, monkeypatch: pytest.MonkeyPatch):
    """Si ni SIGKILL lo mata (aquí: no se le puede señalar), queda en ``survivors`` (-> ``orphans``)."""
    pidfile = tmp_path / "child.pid"
    cancel = asyncio.Event()
    monkeypatch.setattr(process, "_signal_pid", lambda pid, sig: None)
    task = asyncio.create_task(run([sys.executable, "-c", ESCAPE, str(pidfile)], cancel_event=cancel))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    child = int(pidfile.read_text())
    try:
        await asyncio.sleep(0.3)
        cancel.set()
        res, _ = await asyncio.wait_for(task, 20)
        assert child in res.survivors
    finally:
        os.kill(child, 9)


async def test_normal_exit_does_not_touch_detached_children(tmp_path: Path, fast_grace):
    """Solo se persigue a los que escapan al cancelar/agotar el tiempo: si el job acaba bien, un
    servicio que dejó en segundo plano (p. ej. un daemon de build) se respeta."""
    pidfile = tmp_path / "child.pid"
    code = ESCAPE.replace("time.sleep(300)", "")
    res, _ = await run([sys.executable, "-c", code, str(pidfile)])
    child = int(pidfile.read_text())
    try:
        assert res.exit_code == 0 and alive(child)
    finally:
        os.kill(child, 9)


async def test_drain_timeout_leaves_no_never_retrieved_noise(tmp_path: Path, fast_grace):
    """El proceso acaba bien pero un descendiente fuera del grupo mantiene abierta la tubería: al agotar
    el drenaje y cancelar los lectores no debe quedar ``_GatheringFuture exception was never retrieved``."""
    pidfile = tmp_path / "child.pid"
    code = ESCAPE.replace("time.sleep(300)", "")  # el hijo hereda nuestra stdout y el líder sale ya
    loop = asyncio.get_running_loop()
    problems: list[str] = []
    loop.set_exception_handler(lambda _l, ctx: problems.append(str(ctx.get("message"))))
    try:
        res, _ = await run([sys.executable, "-c", code, str(pidfile)])
        assert res.exit_code == 0
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
        os.kill(int(pidfile.read_text()), 9)
    assert not [p for p in problems if "never retrieved" in p], problems


# --- entorno de los jobs -------------------------------------------------------------------------
def test_build_env_drops_server_only_credentials(monkeypatch: pytest.MonkeyPatch):
    for name in SERVER_ONLY_ENV:
        monkeypatch.setenv(name, "secreto-de-prueba")
    monkeypatch.setenv("CLAUDE_CODE_HOST_SESSION_ID", "local_x")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = build_env({})
    assert not set(SERVER_ONLY_ENV) & set(env)
    assert "CLAUDE_CODE_MESSAGING_TOKEN" in SERVER_ONLY_ENV and "CLAUDE_CODE_MESSAGING_SOCKET" in SERVER_ONLY_ENV
    # Lo que una herramienta puede necesitar sigue ahí.
    assert env["PATH"] == os.environ["PATH"] and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["HOME"] == os.environ["HOME"] and env["CLAUDE_CODE_HOST_SESSION_ID"] == "local_x"


def test_build_env_explicit_extra_still_wins(monkeypatch: pytest.MonkeyPatch):
    """``inherit_env``/``env`` del backend pasan por ``extra``: se pueden dar a propósito."""
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "del-servidor")
    assert build_env({"CLAUDE_CODE_MESSAGING_TOKEN": "pedido"})["CLAUDE_CODE_MESSAGING_TOKEN"] == "pedido"


async def test_job_process_does_not_see_messaging_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "secreto-de-prueba")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/cc-socks/x.sock")
    code = "import os; print(sorted(k for k in os.environ if k.startswith('CLAUDE_CODE_MESSAGING')))"
    _, lines = await run([sys.executable, "-c", code])
    assert ("stdout", "[]") in lines
