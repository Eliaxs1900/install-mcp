"""Núcleo de jobs con un backend falso (sin red)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from watcher_mcp.jobs import JobError, JobEvent, JobManager, JobState


async def test_parallel_jobs(manager: JobManager):
    t0 = time.monotonic()
    jobs = [await manager.submit("fake", {"lines": 10, "interval": 0.1}) for _ in range(3)]
    assert all(j.state == JobState.QUEUED for j in jobs)  # submit no bloquea
    assert time.monotonic() - t0 < 0.5
    for j in jobs:
        assert await manager.wait(j.id, 20)
    elapsed = time.monotonic() - t0
    assert [j.state for j in jobs] == [JobState.SUCCEEDED] * 3
    # En serie serían >= 3 s; en paralelo ~1 s.
    assert elapsed < 2.8, elapsed
    starts = sorted(j.started_at for j in jobs)
    ends = sorted(j.finished_at for j in jobs)
    assert starts[-1] < ends[0], "los jobs deberían solaparse"


async def test_max_parallel_queues(config, registry):
    config.max_parallel = 1
    m = JobManager(config, registry)
    a = await m.submit("fake", {"lines": 8, "interval": 0.1})
    b = await m.submit("fake", {"lines": 1})
    await asyncio.sleep(0.4)
    assert a.state == JobState.RUNNING
    assert b.state == JobState.QUEUED
    assert await m.wait(b.id, 20)
    assert b.started_at >= a.finished_at
    await m.shutdown()


async def test_same_lock_key_serializes(manager: JobManager):
    a = await manager.submit("fake", {"lines": 6, "interval": 0.1, "lock": "venv:x"})
    b = await manager.submit("fake", {"lines": 1, "lock": "venv:x"})
    await asyncio.sleep(0.3)
    assert a.state == JobState.RUNNING and b.state == JobState.QUEUED
    assert await manager.wait(b.id, 20)
    assert b.started_at >= a.finished_at


async def test_live_logs_tail_unbuffered(manager: JobManager):
    job = await manager.submit("fake", {"lines": 40, "interval": 0.1})
    await asyncio.sleep(1.2)
    assert job.state == JobState.RUNNING
    live = job.tail(5)
    assert 0 < len(live) <= 5
    texts = [ln.text for ln in job.tail(1000)]
    assert any(t.startswith("line ") for t in texts), "la salida debe llegar mientras el proceso sigue vivo"
    streams = {ln.stream for ln in job.tail(1000)}
    assert {"stdout", "stderr", "core"} <= streams
    # El fichero en disco se escribe línea a línea (se puede seguir con tail -f).
    on_disk = job.log_path.read_text().splitlines()
    assert any("| line 0" in ln for ln in on_disk)
    assert on_disk[0][:4].isdigit() and "Z " in on_disk[0]  # marca de tiempo ISO
    # Seguimiento incremental con since_seq.
    cursor = job.last_seq
    await asyncio.sleep(0.5)
    newer = job.tail(1000, since_seq=cursor)
    assert newer and all(ln.seq > cursor for ln in newer)
    await manager.cancel(job.id)
    await manager.wait(job.id, 10)


async def test_carriage_return_progress_lines(manager: JobManager):
    job = await manager.submit("fake", {"lines": 0, "progress_bar": True})
    await manager.wait(job.id, 10)
    texts = [ln.text for ln in job.tail(100, stream="stdout")]
    assert texts[-3:] == ["10%", "50%", "100%"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Un zombi aún "existe": comprobamos su estado.
    try:
        import subprocess

        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(st) and not st.startswith("Z")
    except OSError:
        return True


async def test_cancel_kills_process_group(manager: JobManager, tmp_path: Path):
    pidfile = tmp_path / "child.pid"
    job = await manager.submit("fake", {"lines": 1000, "interval": 0.1, "child_pidfile": str(pidfile)})
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.1)
    child = int(pidfile.read_text())
    assert job.pid and _alive(job.pid) and _alive(child)
    t0 = time.monotonic()
    await manager.cancel(job.id)
    assert await manager.wait(job.id, 10)
    assert time.monotonic() - t0 < 7
    assert job.state == JobState.CANCELLED
    assert job.steps[0].state == "cancelled"
    await asyncio.sleep(0.3)
    assert not _alive(job.pid)
    assert not _alive(child), "el nieto también debe morir (kill del grupo de procesos)"
    assert job.report and job.report["state"] == "cancelled"


async def test_cancel_while_queued(config, registry):
    config.max_parallel = 1
    m = JobManager(config, registry)
    a = await m.submit("fake", {"lines": 20, "interval": 0.1})
    b = await m.submit("fake", {"lines": 1})
    await asyncio.sleep(0.2)
    await m.cancel(b.id)
    assert await m.wait(b.id, 5)
    assert b.state == JobState.CANCELLED and b.started_at is None
    await m.cancel(a.id)
    await m.shutdown()


async def test_failure_report(manager: JobManager):
    job = await manager.submit("fake", {"lines": 4, "exit_code": 3, "steps": 2})
    assert await manager.wait(job.id, 10)
    assert job.state == JobState.FAILED
    assert job.exit_code == 3
    r = job.report
    assert r["ok"] is False and r["exit_code"] == 3 and r["duration_s"] > 0
    assert "FALLÓ" in r["headline"]
    assert any("ERROR: something broke badly" in ln for ln in r["relevant_lines"])
    assert r["log_tail"]
    assert [s["state"] for s in r["steps"]] == ["failed", "pending"]
    saved = json.loads((job.dir / "report.json").read_text())
    assert saved["exit_code"] == 3


async def test_success_report_has_versions(manager: JobManager):
    job = await manager.submit("fake", {"lines": 2})
    assert await manager.wait(job.id, 10)
    assert job.report["ok"] and job.report["installed"] == {"fake-tool": "1.2.3"}
    assert job.report["steps"][0]["exit_code"] == 0


async def test_step_timeout_is_timed_out(manager: JobManager):
    job = await manager.submit("fake", {"lines": 100, "interval": 0.1, "timeout_s": 0.5})
    assert await manager.wait(job.id, 10)
    assert job.state == JobState.TIMED_OUT
    assert "tiempo máximo" in job.error and job.steps[0].state == "timed_out"


async def test_secrets_are_redacted(manager: JobManager):
    secret = "s3cr3t-Value-123"
    job = await manager.submit("fake", {"lines": 1, "secret": secret})
    await manager.wait(job.id, 10)
    everything = job.log_path.read_text() + json.dumps(job.status()) + json.dumps(job.report)
    assert secret not in everything
    assert "token=***" in everything
    assert "https://***@example.org" in everything


async def test_events_in_order(manager: JobManager):
    events: list[JobEvent] = []

    async def listener(ev: JobEvent) -> None:
        events.append(ev)

    manager.listeners.append(listener)
    job = await manager.submit("fake", {"lines": 1, "steps": 2})
    await manager.wait(job.id, 10)
    await asyncio.sleep(0.05)
    assert [e.kind for e in events] == ["queued", "started", "step", "step", "finished"]
    assert events[-1].data["report"]["ok"] is True


async def test_rejections(manager: JobManager, config):
    with pytest.raises(JobError, match="no permitido"):
        await manager.submit("apt", {})
    with pytest.raises(JobError, match="privilegios"):
        await manager.submit("fake", {"privileged": True})
    with pytest.raises(JobError, match="Parámetros inválidos"):
        await manager.submit("fake", {"unknown": 1})
    with pytest.raises(JobError, match="No existe"):
        manager.get("nope")


async def test_history_survives_restart(config, registry):
    m = JobManager(config, registry)
    job = await m.submit("fake", {"lines": 3})
    await m.wait(job.id, 10)
    # Un job que estaba "running" cuando murió el servidor.
    ghost = config.jobs_dir / "deadbeef0000"
    ghost.mkdir(parents=True)
    (ghost / "job.json").write_text(json.dumps({"job_id": "deadbeef0000", "backend": "fake", "state": "running"}))

    m2 = JobManager(config, registry)
    m2.load_history()
    old = m2.get(job.id)
    assert old.state == JobState.SUCCEEDED and old.report["ok"]
    assert [ln.text for ln in old.tail(100) if ln.stream == "stdout"][:2] == ["line 0", "line 1"]
    g = m2.get("deadbeef0000")
    assert g.state == JobState.FAILED and "interrumpido" in g.error
