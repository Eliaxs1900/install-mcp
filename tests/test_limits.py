"""Ninguna espera es infinita: stalled, timed_out, cola acotada, wait acotado, salida sin buffering."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from install_mcp.jobs import JobEvent, JobManager, JobState
from install_mcp.process import stdbuf_path

from .test_core import _alive


def collect(manager: JobManager) -> list[JobEvent]:
    events: list[JobEvent] = []

    async def listener(ev: JobEvent) -> None:
        events.append(ev)

    manager.listeners.append(listener)
    return events


async def test_stalled_then_resumed(manager: JobManager):
    events = collect(manager)
    job = await manager.submit(
        "fake", {"lines": 3, "interval": 0.05, "pause_s": 1.5, "stall_timeout_s": 0.5}
    )
    # Se detecta el atasco mientras el proceso sigue vivo, sin esperar a que termine.
    t0 = time.monotonic()
    while job.state != JobState.STALLED:
        assert time.monotonic() - t0 < 3, "debería pasar a stalled"
        await asyncio.sleep(0.05)
    assert not job.done.is_set() and _alive(job.pid)
    st = job.status()
    assert st["state"] == "stalled" and st["pid"] == job.pid and st["silent_for_s"] >= 0.5
    stalled = next(e for e in events if e.kind == "stalled")
    assert stalled.level == "warning"
    assert stalled.data["pid"] == job.pid
    assert "python" in Path(stalled.data["command"][0]).name and stalled.data["silent_for_s"] >= 0.5
    assert any("line 0" in ln for ln in stalled.data["log_tail"])
    assert any("atasco" in ln.text for ln in job.tail(50))  # el log nunca se queda mudo
    # La salida vuelve: resumed y el job termina bien.
    assert await manager.wait(job.id, 10)
    assert job.state == JobState.SUCCEEDED and job.stalls == 1
    assert "resumed" in [e.kind for e in events]
    assert job.report["stalls"] == 1


async def test_stall_watchdog_can_be_disabled(manager: JobManager):
    events = collect(manager)
    job = await manager.submit("fake", {"lines": 2, "pause_s": 0.8, "stall_timeout_s": 0})
    assert await manager.wait(job.id, 10)
    assert job.state == JobState.SUCCEEDED
    assert "stalled" not in [e.kind for e in events]


async def test_job_timeout_kills_and_reports_timed_out(manager: JobManager, tmp_path: Path):
    events = collect(manager)
    pidfile = tmp_path / "child.pid"
    t0 = time.monotonic()
    job = await manager.submit(
        "fake", {"lines": 1000, "interval": 0.1, "job_timeout_s": 1, "child_pidfile": str(pidfile), "steps": 2}
    )
    assert await manager.wait(job.id, 15)
    elapsed = time.monotonic() - t0
    assert job.state == JobState.TIMED_OUT
    assert elapsed < 1 + 5 + 3, elapsed  # timeout + gracia de SIGTERM + margen
    assert "tiempo máximo del job" in job.error
    assert [s.state for s in job.steps] == ["timed_out", "cancelled"]
    r = job.report
    assert r["state"] == "timed_out" and not r["ok"] and "TIEMPO AGOTADO" in r["headline"] and r["log_tail"]
    await asyncio.sleep(0.3)
    assert not _alive(job.pid) and not _alive(int(pidfile.read_text()))
    finished = events[-1]
    assert finished.kind == "finished" and finished.level == "error"


async def test_job_timeout_from_backend_config(config, registry):
    config.backends = {"fake": {"job_timeout_s": 0.5}}
    m = JobManager(config, registry)
    job = await m.submit("fake", {"lines": 1000, "interval": 0.1})
    assert job.limits.job_timeout_s == 0.5
    assert await m.wait(job.id, 10)
    assert job.state == JobState.TIMED_OUT
    # La petición manda sobre la configuración.
    job2 = await m.submit("fake", {"lines": 1, "job_timeout_s": 30})
    assert job2.limits.job_timeout_s == 30
    await m.wait(job2.id, 10)
    await m.shutdown()


async def test_queue_timeout(config, registry):
    config.max_parallel = 1
    config.max_queue_s = 0.5
    m = JobManager(config, registry)
    a = await m.submit("fake", {"lines": 30, "interval": 0.1})
    b = await m.submit("fake", {"lines": 1})
    assert await m.wait(b.id, 5)
    assert b.state == JobState.TIMED_OUT and "cola" in b.error and b.started_at is None
    assert a.state == JobState.RUNNING
    await m.cancel(a.id)
    await m.shutdown()


async def test_wait_is_bounded(manager: JobManager):
    job = await manager.submit("fake", {"lines": 1000, "interval": 0.1})
    t0 = time.monotonic()
    assert await manager.wait(job.id, 0.3) is False
    assert 0.25 < time.monotonic() - t0 < 1.0
    await manager.cancel(job.id)


async def test_slow_listener_does_not_block_job(config, registry):
    config.listener_timeout_s = 0.2
    m = JobManager(config, registry)

    async def stuck(ev: JobEvent) -> None:
        await asyncio.sleep(3600)

    m.listeners.append(stuck)
    t0 = time.monotonic()
    job = await m.submit("fake", {"lines": 1})
    assert await m.wait(job.id, 10)
    assert job.state == JobState.SUCCEEDED and time.monotonic() - t0 < 3
    assert any("listener tardó" in ln.text for ln in job.tail(50))
    await m.shutdown()


async def _stdout_lines_while_running(manager: JobManager, io_mode: str | None) -> tuple[int, JobManager]:
    job = await manager.submit(
        "fake",
        {"lines": 12, "interval": 0.1, "buffered": True, "stdout_only": True, "io_mode": io_mode},
    )
    await asyncio.sleep(0.8)
    assert job.state == JobState.RUNNING
    seen = len([ln for ln in job.tail(100) if ln.stream == "stdout"])
    assert await manager.wait(job.id, 10)
    assert job.state == JobState.SUCCEEDED
    assert len([ln for ln in job.tail(100) if ln.stream == "stdout"]) == 12
    return seen, manager


async def test_buffered_program_pipe_vs_pty(manager: JobManager):
    # Un programa con buffer de libc (aquí: Python sin PYTHONUNBUFFERED) por tubería no
    # muestra nada hasta terminar: justo el problema de "archivo de salida vacío".
    seen_pipe, _ = await _stdout_lines_while_running(manager, "pipe")
    assert seen_pipe == 0
    # Con un pty la libc pasa a buffer por líneas: la salida llega en vivo.
    seen_pty, _ = await _stdout_lines_while_running(manager, "pty")
    assert seen_pty >= 4


async def test_stdbuf_mode_falls_back_to_pty(manager: JobManager):
    job = await manager.submit("fake", {"lines": 3, "io_mode": "stdbuf"})
    assert await manager.wait(job.id, 10)
    assert job.state == JobState.SUCCEEDED
    if stdbuf_path() is None:
        assert any("stdbuf no disponible" in ln.text for ln in job.tail(50))


async def test_log_never_empty_while_running(manager: JobManager):
    job = await manager.submit("fake", {"lines": 2, "pause_s": 1.0, "buffered": True})
    await asyncio.sleep(0.3)
    text = job.log_path.read_text()
    assert "creado" in text and "$ " in text and "pid " in text  # al menos qué se ejecuta y con qué pid
    await manager.wait(job.id, 10)


@pytest.mark.parametrize("bad", ["fifo", "x"])
async def test_invalid_io_mode_rejected(config, registry, bad):
    config.default_io_mode = bad
    m = JobManager(config, registry)
    with pytest.raises(ValueError, match="io_mode"):
        await m.submit("fake", {"lines": 1})
