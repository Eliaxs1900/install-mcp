"""Puente a sessionbus: aviso del fin de job a la sesión que lo lanzó (con un CLI falso)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from watcher_mcp.busbridge import BODY_MAX_BYTES, SUMMARY_MAX, BusBridge, create_bridge, launcher_node
from watcher_mcp.config import Config, SessionbusConfig
from watcher_mcp.jobs import JobEvent, JobManager, JobState
from watcher_mcp.server import create_server

NODE = "local_11111111-2222-3333-4444-555555555555"

# Un ``sessionbus`` falso: guarda su argv, su stdin y su entorno; el modo sale de FAKE_SB_MODE.
FAKE_CLI = """#!{python}
import json, os, sys, time
out = os.environ["FAKE_SB_OUT"]
n = len(os.listdir(out))
with open(os.path.join(out, f"call-{{n}}.json"), "w") as fh:
    json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": dict(os.environ)}}, fh)
mode = os.environ.get("FAKE_SB_MODE", "ok")
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    print("sessionbus publish: la base de datos está bloqueada", file=sys.stderr)
    sys.exit(1)
print(json.dumps({{"frame_id": 42, "topic": "x", "recipients": []}}))
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "sessionbus"
    path.parent.mkdir()
    path.write_text(FAKE_CLI.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def calls_dir(tmp_path: Path) -> Path:
    d = tmp_path / "calls"
    d.mkdir()
    return d


def make_env(calls_dir: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "SESSIONBUS"))}
    env.update(CLAUDE_CODE_HOST_SESSION_ID=NODE, CLAUDE_CODE_SESSION_ID="abc-def", FAKE_SB_OUT=str(calls_dir), **extra)
    return env


def read_calls(calls_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(calls_dir.glob("call-*.json"))]


def opt(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def attach(manager: JobManager, cfg: SessionbusConfig, env: dict[str, str]) -> BusBridge:
    bridge = BusBridge(cfg, env=env)
    manager.listeners.append(bridge.on_event)
    return bridge


async def run_job(manager: JobManager, bridge: BusBridge, params: dict[str, Any]):
    job = await manager.submit("fake", params)
    assert await manager.wait(job.id, 20)
    await bridge.drain()
    return job


async def test_disabled_by_default_calls_nothing(config: Config, registry, fake_cli, calls_dir, monkeypatch):
    assert Config().sessionbus.enabled is False
    assert create_bridge(config) is None
    monkeypatch.setenv("FAKE_SB_OUT", str(calls_dir))
    cfg = Config(state_dir=config.state_dir, allowed_backends=["fake"], allowed_roots=config.allowed_roots,
                 sessionbus=SessionbusConfig(command=[str(fake_cli)]))  # command sin enabled
    _, manager = create_server(cfg, registry)
    n = len(manager.listeners)
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    await asyncio.sleep(0.2)
    assert read_calls(calls_dir) == []
    assert n == 1  # solo el notificador MCP
    await manager.shutdown(timeout_s=5)


async def test_enabled_registers_listener(config: Config, registry, fake_cli, calls_dir, monkeypatch):
    monkeypatch.setenv("FAKE_SB_OUT", str(calls_dir))
    monkeypatch.setenv("CLAUDE_CODE_HOST_SESSION_ID", NODE)
    cfg = Config(state_dir=config.state_dir, allowed_backends=["fake"], allowed_roots=config.allowed_roots,
                 sessionbus=SessionbusConfig(enabled=True, command=[str(fake_cli)]))
    _, manager = create_server(cfg, registry)
    assert len(manager.listeners) == 2
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    await asyncio.sleep(0.5)
    assert len(read_calls(calls_dir)) == 1
    await manager.shutdown(timeout_s=5)


async def test_finished_ok(manager: JobManager, fake_cli, calls_dir):
    bridge = attach(manager, SessionbusConfig(enabled=True, command=[str(fake_cli)]), make_env(calls_dir))
    job = await run_job(manager, bridge, {"lines": 2})
    (call,) = read_calls(calls_dir)
    argv = call["argv"]
    assert argv[0] == "publish"
    assert opt(argv, "--topic") == f"nodo/{NODE}/jobs/{job.id}"
    assert opt(argv, "--kind") == "info"
    assert opt(argv, "--priority") == "normal"  # baja nunca se inyecta
    assert opt(argv, "--ttl") == "86400"
    assert opt(argv, "--body-file") == "-"
    assert len(opt(argv, "--summary")) <= SUMMARY_MAX and job.id in opt(argv, "--summary")
    body = call["stdin"]
    for needle in ("estado: succeeded", "exit_code: 0", "duración:", f"job: job://{job.id}", "log_file:"):
        assert needle in body
    # El remitente no es la sesión: sin CLAUDE_CODE_* y con el nombre acordado.
    assert "CLAUDE_CODE_HOST_SESSION_ID" not in call["env"] and "CLAUDE_CODE_SESSION_ID" not in call["env"]
    assert call["env"]["SESSIONBUS_NODE_NAME"] == "watcher-mcp"
    assert any("aviso publicado en sessionbus (trama 42)" in ln.text for ln in job.tail(50))


async def test_finished_failure_is_alert_with_relevant_lines(manager: JobManager, fake_cli, calls_dir):
    bridge = attach(manager, SessionbusConfig(enabled=True, command=[str(fake_cli)]), make_env(calls_dir))
    job = await run_job(manager, bridge, {"lines": 2, "exit_code": 3})
    assert job.state == JobState.FAILED
    (call,) = read_calls(calls_dir)
    assert opt(call["argv"], "--priority") == "alerta"
    assert "FALLÓ" in opt(call["argv"], "--summary")
    assert "exit_code: 3" in call["stdin"] and "líneas relevantes:" in call["stdin"]
    assert "something broke badly" in call["stdin"]


async def test_timed_out_is_alert(manager: JobManager, fake_cli, calls_dir):
    bridge = attach(manager, SessionbusConfig(enabled=True, command=[str(fake_cli)]), make_env(calls_dir))
    job = await run_job(manager, bridge, {"lines": 100, "interval": 0.1, "job_timeout_s": 0.5})
    assert job.state == JobState.TIMED_OUT
    (call,) = read_calls(calls_dir)
    assert opt(call["argv"], "--priority") == "alerta"


async def test_custom_priorities_and_stalled_event(manager: JobManager, fake_cli, calls_dir):
    cfg = SessionbusConfig(enabled=True, command=[str(fake_cli)], events=["finished", "stalled"],
                           priorities={"succeeded": "baja", "stalled": "normal"})
    bridge = attach(manager, cfg, make_env(calls_dir))
    job = await run_job(manager, bridge, {"lines": 2, "pause_s": 1.5, "stall_timeout_s": 0.3})
    calls = read_calls(calls_dir)
    by_prio = sorted(opt(c["argv"], "--priority") for c in calls)
    assert by_prio == ["baja", "normal"], calls  # finished OK -> baja; stalled -> normal
    stalled = next(c for c in calls if "ATASCADO" in opt(c["argv"], "--summary"))
    assert opt(stalled["argv"], "--topic") == f"nodo/{NODE}/jobs/{job.id}"
    assert "pid" in stalled["stdin"] and "sin salida desde hace" in stalled["stdin"]


async def test_stalled_not_published_by_default(manager: JobManager, fake_cli, calls_dir):
    bridge = attach(manager, SessionbusConfig(enabled=True, command=[str(fake_cli)]), make_env(calls_dir))
    await run_job(manager, bridge, {"lines": 2, "pause_s": 1.0, "stall_timeout_s": 0.3})
    assert len(read_calls(calls_dir)) == 1  # solo finished


async def test_missing_cli_does_not_affect_job(manager: JobManager, calls_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "vacio"))  # sin autodetección posible
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    cfg = SessionbusConfig(enabled=True, command=[str(tmp_path / "no-existe" / "sessionbus")])
    bridge = attach(manager, cfg, make_env(calls_dir))
    t0 = time.monotonic()
    job = await run_job(manager, bridge, {"lines": 1})
    assert time.monotonic() - t0 < 5
    assert job.state == JobState.SUCCEEDED and job.report["ok"]
    assert any("no se encuentra el CLI de sessionbus" in ln.text for ln in job.tail(50))
    assert "sessionbus" in capsys.readouterr().err


async def test_hanging_cli_is_cut_and_job_finishes_on_time(manager: JobManager, fake_cli, calls_dir, capsys):
    cfg = SessionbusConfig(enabled=True, command=[str(fake_cli)], timeout_s=0.5)
    bridge = attach(manager, cfg, make_env(calls_dir, FAKE_SB_MODE="hang"))
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    t0 = time.monotonic()
    await asyncio.sleep(0)  # el listener solo lanza la tarea
    assert job.state == JobState.SUCCEEDED and job.done.is_set()
    await bridge.drain()
    assert time.monotonic() - t0 < 4
    assert any("tardó más de 0.5s" in ln.text for ln in job.tail(50))
    assert "tardó más de" in capsys.readouterr().err


async def test_listener_returns_immediately(manager: JobManager, fake_cli, calls_dir):
    cfg = SessionbusConfig(enabled=True, command=[str(fake_cli)], timeout_s=2)
    bridge = BusBridge(cfg, env=make_env(calls_dir, FAKE_SB_MODE="hang"))
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    t0 = time.monotonic()
    await bridge.on_event(JobEvent("finished", job, "info", "x", {"report": job.report}))
    assert time.monotonic() - t0 < 0.2
    await bridge.drain()


async def test_failing_cli_is_logged_only(manager: JobManager, fake_cli, calls_dir, capsys):
    bridge = attach(manager, SessionbusConfig(enabled=True, command=[str(fake_cli)]),
                    make_env(calls_dir, FAKE_SB_MODE="fail"))
    job = await run_job(manager, bridge, {"lines": 1})
    assert job.state == JobState.SUCCEEDED
    assert any("terminó con código 1" in ln.text for ln in job.tail(50))
    assert "base de datos" in capsys.readouterr().err


async def test_no_identity_publishes_nothing_and_warns_once(manager: JobManager, fake_cli, calls_dir, capsys):
    env = make_env(calls_dir)
    del env["CLAUDE_CODE_HOST_SESSION_ID"], env["CLAUDE_CODE_SESSION_ID"]
    bridge = attach(manager, SessionbusConfig(enabled=True, command=[str(fake_cli)]), env)
    await run_job(manager, bridge, {"lines": 1})
    await run_job(manager, bridge, {"lines": 1})
    assert read_calls(calls_dir) == []
    assert capsys.readouterr().err.count("no se publica en sessionbus") == 1


def test_launcher_node_validates_segment():
    assert launcher_node({"CLAUDE_CODE_HOST_SESSION_ID": NODE}) == NODE
    assert launcher_node({"CLAUDE_CODE_SESSION_ID": "abc-123"}) == "abc-123"  # sin alias de la app
    assert launcher_node({"CLAUDE_CODE_HOST_SESSION_ID": "con/barra"}) is None  # no es un segmento de tema
    assert launcher_node({}) is None


async def test_body_is_redacted_and_capped(manager: JobManager, fake_cli, calls_dir):
    bridge = BusBridge(SessionbusConfig(enabled=True, command=[str(fake_cli)]), env=make_env(calls_dir))
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    # Un informe con secretos "sin redactar" (como si alguien hubiera tocado _report) y enorme.
    secret = "ghp_" + "A" * 30
    job.report = {**job.report, "state": "failed", "ok": False, "headline": f"[{job.id}] FALLÓ token={secret}",
                  "error": f"Authorization: Bearer {secret}",
                  "relevant_lines": [f"2026 stderr | password=hunter2hunter2 {'x' * 500}"] * 20 + ["ÿ" * 20000]}
    await bridge.on_event(JobEvent("finished", job, "error", job.report["headline"], {"report": job.report}))
    await bridge.drain()
    (call,) = read_calls(calls_dir)
    assert secret not in call["stdin"] and "hunter2hunter2" not in call["stdin"]
    assert secret not in " ".join(call["argv"])
    assert "***" in call["stdin"]
    assert len(call["stdin"].encode()) <= BODY_MAX_BYTES


def test_summary_capped_at_200():
    class J:  # lo mínimo que lee build_summary
        id = "abc"
        report = {"headline": "x" * 500}

    ev = JobEvent("finished", J(), "info", "m")  # type: ignore[arg-type]
    assert len(BusBridge.build_summary(ev)) == SUMMARY_MAX


def test_config_validation_and_defaults():
    cfg = Config.from_dict({"sessionbus": {"enabled": True, "timeout_s": 60}})
    sb = cfg.sessionbus
    assert sb.enabled and sb.timeout_s == 5.0  # tope duro
    assert sb.events == ["finished"] and sb.priorities["succeeded"] == "normal" and sb.priorities["failed"] == "alerta"
    assert Config.from_dict({"sessionbus": {"priorities": {"failed": "normal"}}}).sessionbus.priorities["timed_out"] == "alerta"
    with pytest.raises(ValueError, match="events"):
        Config.from_dict({"sessionbus": {"events": ["started"]}})
    with pytest.raises(ValueError, match="priorities"):
        Config.from_dict({"sessionbus": {"priorities": {"failed": "urgente"}}})
    with pytest.raises(ValueError, match="desconocidas"):
        Config.from_dict({"sessionbus": {"cmd": ["x"]}})
