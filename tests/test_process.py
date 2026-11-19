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
from watcher_mcp.backends import Backend, BackendParams, CommandStep, Plan
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager
from watcher_mcp.process import (
    MAX_LINE, SERVER_ONLY_ENV, SERVER_ONLY_PATTERNS, build_env, is_server_only, run_capture, run_command,
)
from watcher_mcp.security import Redactor


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


# --- ola 3: tope de las sondas, credenciales por patrón, redacción antes de trocear ---------------------
async def test_run_capture_legit_probe_of_1mib_works():
    code, out, _ = await run_capture(
        [sys.executable, "-c", "import sys; sys.stdout.write('v' * (1 << 20))"], env=build_env({}), timeout_s=30,
    )
    assert code == 0 and len(out) == 1 << 20


async def test_run_capture_default_cap_is_generous_but_bounded():
    assert 1 << 20 <= process.MAX_CAPTURE_BYTES <= 16 << 20
    t0 = time.monotonic()
    code, out, err = await run_capture(
        [sys.executable, "-c", FLOOD.format(stream="stdout"), "/dev/null"], env=build_env({}), timeout_s=30,
    )
    assert code == -1 and "límite" in err and time.monotonic() - t0 < 10


async def test_run_capture_max_bytes_per_call():
    code, _, err = await run_capture(
        [sys.executable, "-c", "print('z' * 5000)"], env=build_env({}), max_bytes=1000, timeout_s=30,
    )
    assert code == -1 and "1000" in err


@pytest.mark.parametrize("name", [
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN", "CLAUDE_CODE_MESSAGING_TOKEN", "claude_code_oauth_token",
    "CLAUDE_CODE_FOO_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
])
def test_credential_patterns_are_server_only(name: str, monkeypatch: pytest.MonkeyPatch):
    assert is_server_only(name)
    monkeypatch.setenv(name, "secreto-de-prueba")
    assert name not in build_env({})
    assert name in build_env({name: "dado-a-proposito"})  # inherit_env/env a propósito: lo explícito manda


@pytest.mark.parametrize("name", [
    "PATH", "HOME", "HTTPS_PROXY", "GH_TOKEN", "NPM_TOKEN", "HF_TOKEN", "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_OAUTH_SCOPES", "CLAUDE_CODE_ENTRYPOINT",
])
def test_ordinary_variables_are_inherited(name: str, monkeypatch: pytest.MonkeyPatch):
    assert not is_server_only(name)
    monkeypatch.setenv(name, "v")
    assert build_env({})[name] == "v"


def test_patterns_are_documented_as_a_tuple():
    assert "ANTHROPIC_*" in SERVER_ONLY_PATTERNS and "CLAUDE_CODE_*TOKEN*" in SERVER_ONLY_PATTERNS


SECRET = "ghp_" + "A" * 36


async def test_secret_crossing_the_8kib_cut_is_redacted_before_chunking():
    """Con salto de línea: la línea entera se redacta y luego se trocea."""
    code = f"print('x' * ({MAX_LINE} - 10) + ' ' + {SECRET!r} + ' ' + 'y' * 100)"
    lines: list[str] = []
    res = await run_command([sys.executable, "-c", code], env=build_env({}), cwd=None,
                            on_line=lambda s, t: lines.append(t) if s == "stdout" else None,
                            cancel_event=asyncio.Event(), timeout_s=30, redact=Redactor())
    assert res.exit_code == 0
    joined = "".join(lines)
    assert "AAAAAAAAAA" not in joined and "***" in joined and joined.endswith("y" * 100)
    assert all(len(t) <= MAX_LINE for t in lines)


async def test_secret_crossing_the_cut_without_newline_and_across_reads(tmp_path: Path):
    """Sin salto y llegando en dos lecturas: el trozo con el principio del secreto no sale sin redactar."""
    code = (
        "import sys, time\n"
        f"sys.stdout.write('x' * 8700 + ' ghp_' + 'A' * 10); sys.stdout.flush(); time.sleep(0.5)\n"
        f"sys.stdout.write('A' * 26 + ' ' + 'y' * 30000); sys.stdout.flush()\n"
    )
    lines: list[str] = []
    await run_command([sys.executable, "-c", code], env=build_env({}), cwd=None,
                      on_line=lambda s, t: lines.append(t) if s == "stdout" else None,
                      cancel_event=asyncio.Event(), timeout_s=30, redact=Redactor())
    joined = "".join(lines)
    assert "AAAAAAAAAA" not in joined and "***" in joined
    assert joined.count("y") == 30000 and all(len(t) <= MAX_LINE for t in lines)


class LongParams(BackendParams):
    secret: str


class LongLineBackend(Backend[LongParams]):
    name = "longline"
    description = "Imprime una línea larga con el secreto justo en el corte de 8 KiB"
    Params = LongParams

    def plan(self, spec: LongParams) -> Plan:
        code = f"print('x' * ({MAX_LINE} - 20) + {spec.secret!r} + 'y' * 50)"
        return Plan(summary="línea larga", steps=[CommandStep("long", [sys.executable, "-c", code])],
                    secrets=[spec.secret])


async def test_job_log_redacts_literal_secret_split_by_the_cut(config):
    """Camino real: Job.add_line es el on_line y su redactor (con los literales del plan) se usa sin tocar
    jobs.py. Un literal que no casa con ningún patrón genérico quedaba partido en el corte."""
    config.allowed_backends = ["longline"]
    m = JobManager(config, {"longline": LongLineBackend})
    try:
        secret = "S3cr3t-" + "Q" * 40
        job = await m.submit("longline", {"secret": secret})
        assert await m.wait(job.id, 30)
        text = "\n".join(ln.text for ln in job.tail(500))
        assert "QQQQQQQQQQ" not in text and secret not in text and "***" in text
        assert "QQQQQQQQQQ" not in job.log_path.read_text()
    finally:
        await m.shutdown(timeout_s=5)


# --- ola 3: run no deja rodear la lista con env_from/env; sonda de pip acotada ------------------------
PY = Path(sys.executable).name


def run_manager(tmp_path: Path, **opts) -> JobManager:
    from watcher_mcp.backends import BUILTIN

    cfg = Config(state_dir=tmp_path / "state", allowed_backends=["run"], allowed_roots=[tmp_path],
                 backends={"run": {"programs": [PY], **opts}})
    return JobManager(cfg, BUILTIN)


@pytest.mark.parametrize("name", ["CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ACCESS_TOKEN", "ANTHROPIC_API_KEY"])
async def test_run_rejects_server_credentials_in_env_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    from watcher_mcp.jobs import JobError

    monkeypatch.setenv(name, "secreto-de-prueba")
    m = run_manager(tmp_path)
    try:
        with pytest.raises(JobError, match="credencial del servidor.*inherit_env"):
            await m.submit("run", {"program": PY, "args": ["-c", "pass"], "cwd": str(tmp_path), "env_from": [name]})
    finally:
        await m.shutdown(timeout_s=5)


async def test_run_rejects_server_credentials_in_env(tmp_path: Path):
    from watcher_mcp.jobs import JobError

    m = run_manager(tmp_path, env_allow=["CLAUDE_CODE_MESSAGING_TOKEN"])
    try:
        with pytest.raises(JobError, match="credencial del servidor"):
            await m.submit("run", {"program": PY, "args": ["-c", "pass"], "cwd": str(tmp_path),
                                   "env": {"CLAUDE_CODE_MESSAGING_TOKEN": "x"}})
    finally:
        await m.shutdown(timeout_s=5)


async def test_job_sees_credential_only_via_inherit_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-prueba-0123456789-no-real")
    code = "import os; print('K', os.environ.get('ANTHROPIC_API_KEY', '<none>'))"
    texts = {}
    for label, opts in (("sin", {}), ("con", {"inherit_env": ["ANTHROPIC_API_KEY"]})):
        (tmp_path / label).mkdir(exist_ok=True)
        m = run_manager(tmp_path / label, **opts)
        try:
            job = await m.submit("run", {"program": PY, "args": ["-c", code], "cwd": str(tmp_path / label)})
            assert await m.wait(job.id, 30)
            texts[label] = job.log_path.read_text()
        finally:
            await m.shutdown(timeout_s=5)
    assert "K <none>" in texts["sin"]
    assert "K ***" in texts["con"] and "sk-prueba" not in texts["con"]  # llega, y redactada


async def test_pip_probe_asks_only_for_named_dists(config: Config):
    from watcher_mcp.backends.base import ProbeResult
    from watcher_mcp.backends.pip import _PROBE_CODE, PipBackend, PipParams

    backend = PipBackend(config)
    seen: list[list[str]] = []

    async def probe(argv, env=None, timeout_s=60, cwd=None):
        seen.append(argv)
        code, out, err = await run_capture([sys.executable, *argv[1:]], env=build_env({}), timeout_s=30)
        return ProbeResult(code, out, err)

    spec = PipParams(packages=["PyTest>=8", "no_existe_xyz"])
    found = await backend.detect_versions(spec, probe)
    (argv,) = seen
    assert argv[-2:] == ["pytest", "no-existe-xyz"] and argv[3] == _PROBE_CODE  # nombres canónicos
    assert set(found) == {"pytest", "no-existe-xyz", "(python)"} and found["no-existe-xyz"] == "NO ENCONTRADO"
    assert found["pytest"] not in ("NO ENCONTRADO", "?")


async def test_pip_probe_output_stays_small_with_a_big_env():
    """Aunque el venv tenga miles de dists, la sonda solo imprime las pedidas."""
    from watcher_mcp.backends.pip import _PROBE_CODE

    code, out, _ = await run_capture([sys.executable, "-I", "-c", _PROBE_CODE, "pytest"], env=build_env({}))
    import json

    data = json.loads(out.strip().splitlines()[-1])
    assert code == 0 and set(data["dists"]) <= {"pytest"} and len(out) < 500
