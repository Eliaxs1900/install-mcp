"""Núcleo (jobs.py), hallazgos de la auditoría: #99 redacción, #102 saltos de línea, #110 lock entre
procesos, #101 jobs de otra sesión, #111 cancel en la sonda, #126 poda, #119 tope del log."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from watcher_mcp.backends import Backend, BackendParams, CommandStep, Plan
from watcher_mcp.backends.base import Probe
from watcher_mcp.config import Config
from watcher_mcp.jobs import Job, JobEvent, JobManager, JobState
from watcher_mcp.security import Redactor

from .conftest import FakeBackend

SECRET = "hunter2pw"


async def until(cond, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "condición no cumplida a tiempo"
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #99 redacción
class LeakyParams(BackendParams):
    pass


class LeakyBackend(Backend[LeakyParams]):
    name = "leaky"
    description = "summary, argv y paso con un secreto"
    Params = LeakyParams

    def plan(self, spec: LeakyParams) -> Plan:
        return Plan(
            summary=f"descargar https://user:{SECRET}@h.example/x.bin",
            steps=[CommandStep(f"login-{SECRET}", [sys.executable, "-c", "import sys; sys.exit(3)"])],
            secrets=[SECRET],
        )

    def result_info(self, spec: LeakyParams) -> dict[str, Any]:
        return {"argv": ["echo", f"--password={SECRET}"], "nota": f"clave {SECRET}"}


async def test_secrets_never_leave_the_core(config, registry, tmp_path):
    m = JobManager(config, {**registry, "leaky": LeakyBackend})
    config.allowed_backends.append("leaky")
    events: list[JobEvent] = []

    async def listener(ev: JobEvent) -> None:
        events.append(ev)

    m.listeners.append(listener)
    job = await m.submit("leaky", {})
    assert await m.wait(job.id, 30)
    assert job.state == JobState.FAILED
    outputs = [
        json.dumps(job.status(tail=50)), json.dumps(job.report), job.summary, job.error or "",
        (job.dir / "job.json").read_text(), (job.dir / "report.json").read_text(), job.log_path.read_text(),
        *[ev.message + json.dumps(ev.data, default=str) for ev in events],
    ]
    for text in outputs:
        assert SECRET not in text
    assert "***" in job.summary and "***" in json.dumps(job.report["info"])
    await m.shutdown()


# --------------------------------------------------------------------------- #102 saltos de línea
def _job(tmp_path: Path, memory_lines: int = 2, **kw) -> Job:
    job = Job("abc123abc123", "x", {}, Plan("resumen", []), tmp_path / "j", Redactor(), memory_lines, **kw)
    job.open_log()
    return job


def test_multiline_text_is_one_event_one_physical_line(tmp_path):
    job = _job(tmp_path)
    job.add_line("core", "job creado")
    job.add_line("core", "$ python3 -c print('a')\nprint('b')\r\nprint('c')")
    job.add_line("stdout", "hola")
    job.add_line("core", "error\nmultilínea")
    job.add_line("stdout", "fin")
    job.close_log()
    physical = job.log_path.read_text().splitlines()
    assert len(physical) == job.last_seq == 5
    from_disk = job.tail(100)  # memoria de 2 líneas: se lee del disco
    assert [ln.seq for ln in from_disk] == [1, 2, 3, 4, 5] or [ln.seq for ln in from_disk][-1] == 5
    assert all(ln.stream in ("core", "stdout") for ln in from_disk)
    assert "\\n" in from_disk[1].text and "print('c')" in from_disk[1].text
    job.persist()
    reloaded = Job.from_disk(job.dir, Redactor(), 2)
    assert reloaded is not None and reloaded.last_seq == 5  # = log_lines antes de reiniciar


async def test_run_with_multiline_argument_keeps_seq_consistent(manager: JobManager):
    class Multi(FakeBackend):
        name = "multi"

        def plan(self, spec: Any) -> Plan:
            return Plan("multi", [CommandStep("c", [sys.executable, "-c", "print('a')\nprint('b')"])])

    manager.registry["multi"] = Multi
    manager.config.allowed_backends.append("multi")
    job = await manager.submit("multi", {})
    assert await manager.wait(job.id, 20)
    assert len(job.log_path.read_text().splitlines()) == job.last_seq


# --------------------------------------------------------------------------- #110 lock entre procesos
async def test_lock_key_serializes_across_servers(config, registry):
    a, b = JobManager(config, registry), JobManager(config, registry)
    ja = await a.submit("fake", {"lines": 20, "interval": 0.05, "lock": "venv:/x"})
    await until(lambda: ja.state == JobState.RUNNING)
    jb = await b.submit("fake", {"lines": 2, "interval": 0, "lock": "venv:/x"})
    await asyncio.sleep(0.6)
    assert ja.state == JobState.RUNNING and jb.state == JobState.QUEUED  # antes: ambos running
    assert await a.wait(ja.id, 20) and await b.wait(jb.id, 20)
    assert jb.state == JobState.SUCCEEDED
    assert jb.started_at is not None and ja.steps[0].finished_at is not None
    assert jb.started_at >= ja.steps[0].finished_at
    assert a._locks == {} and b._locks == {}  # sin acumular una entrada por clave
    await a.shutdown()
    await b.shutdown()


async def test_cross_server_wait_respects_max_queue(config, registry):
    a = JobManager(config, registry)
    cfg_b = Config(**{**config.__dict__, "max_queue_s": 0.6})
    b = JobManager(cfg_b, registry)
    ja = await a.submit("fake", {"lines": 40, "interval": 0.05, "lock": "venv:/y"})
    await until(lambda: ja.state == JobState.RUNNING)
    jb = await b.submit("fake", {"lines": 1, "lock": "venv:/y"})
    assert await b.wait(jb.id, 10)
    assert jb.state == JobState.TIMED_OUT and "cola" in (jb.error or "")
    jc = await b.submit("fake", {"lines": 1, "lock": "venv:/y"})  # y se puede cancelar esperando
    await b.cancel(jc.id)
    assert await b.wait(jc.id, 10) and jc.state in (JobState.CANCELLED, JobState.TIMED_OUT)
    await a.shutdown()
    await b.shutdown()


# --------------------------------------------------------------------------- #101 jobs de otra sesión
async def test_foreign_job_is_refreshed_and_new_jobs_are_visible(config, registry):
    a = JobManager(config, registry)
    j1 = await a.submit("fake", {"lines": 20, "interval": 0.05})
    await until(lambda: j1.state == JobState.RUNNING)
    b = JobManager(config, registry)
    b.load_history()
    assert b.get(j1.id).state == JobState.RUNNING and b.get(j1.id).foreign
    assert await b.wait(j1.id, 20)  # antes: agotaba su espera siempre
    seen = b.get(j1.id)
    assert seen.state == JobState.SUCCEEDED and not seen.foreign and seen.report
    j2 = await a.submit("fake", {"lines": 1})  # lanzado tras arrancar B
    assert await a.wait(j2.id, 20)
    assert b.get(j2.id).state == JobState.SUCCEEDED
    assert {j1.id, j2.id} <= {j.id for j in b.list(limit=50)}
    with pytest.raises(Exception, match="No existe"):
        b.get("0" * 12)
    await a.shutdown()
    await b.shutdown()


async def test_foreign_job_whose_owner_died_becomes_failed(config, registry):
    a = JobManager(config, registry)
    j = await a.submit("fake", {"lines": 200, "interval": 0.05})
    await until(lambda: j.state == JobState.RUNNING)
    b = JobManager(config, registry)
    b.load_history()
    assert b.get(j.id).foreign
    a._release_instance()  # el dueño "muere": suelta su flock
    a._instance_fh = None
    seen = b.get(j.id)
    assert seen.state == JobState.FAILED and "interrumpido" in (seen.error or "") and not seen.foreign
    await a.cancel(j.id)
    await a.shutdown()
    await b.shutdown()


# --------------------------------------------------------------------------- #111 cancel en la sonda
class SlowProbe(FakeBackend):
    name = "slowprobe"

    async def detect_versions(self, spec: Any, probe: Probe) -> dict[str, str]:
        await asyncio.sleep(60)
        return {"x": "1"}


async def test_cancel_during_version_probe(config, registry):
    config.allowed_backends.append("slowprobe")
    m = JobManager(config, {**registry, "slowprobe": SlowProbe})
    job = await m.submit("slowprobe", {"lines": 1, "interval": 0})
    await until(lambda: job.steps[0].state == "ok")
    assert job.state == JobState.RUNNING  # ya en la sonda
    await m.cancel(job.id)
    assert await m.wait(job.id, 10)
    assert job.state == JobState.CANCELLED  # antes: succeeded tras cancelar
    await m.shutdown()


async def test_probe_is_inside_the_job_timeout(config, registry):
    config.allowed_backends.append("slowprobe")
    m = JobManager(config, {**registry, "slowprobe": SlowProbe})
    job = await m.submit("slowprobe", {"lines": 1, "interval": 0, "job_timeout_s": 1})
    assert await m.wait(job.id, 15)
    assert job.state == JobState.TIMED_OUT
    await m.shutdown()


# --------------------------------------------------------------------------- #126 retención
def test_unreadable_job_dirs_are_swept_when_old(config, registry):
    jobs = config.jobs_dir
    old, fresh = jobs / "deadbeef0000", jobs / "deadbeef0001"
    for d in (old, fresh):
        d.mkdir(parents=True)
        (d / "output.log").write_text("x\n")
    past = time.time() - 8 * 86400
    for p in (old, old / "output.log"):
        os.utime(p, (past, past))
    m = JobManager(config, registry)
    m.load_history()
    assert not old.exists() and fresh.exists()  # lo reciente puede ser un job en marcha de otra sesión


# --------------------------------------------------------------------------- #119 tope del log
async def test_log_cap_truncates_disk_but_not_the_job(config, registry):
    cfg = Config(**{**config.__dict__, "max_log_bytes": 3000})
    m = JobManager(cfg, registry)
    job = await m.submit("fake", {"lines": 300, "interval": 0})
    assert await m.wait(job.id, 30)
    assert job.state == JobState.SUCCEEDED
    text = job.log_path.read_text()
    assert len(text.encode()) < 4000 and "log truncado" in text
    assert job.last_seq >= 300 and "line 299" in "\n".join(ln.text for ln in job.tail(100))  # memoria al día (stdout y stderr llegan intercalados)
    await m.shutdown()


async def test_log_has_no_cap_when_zero(config, registry):
    cfg = Config(**{**config.__dict__, "max_log_bytes": 0})
    m = JobManager(cfg, registry)
    job = await m.submit("fake", {"lines": 300, "interval": 0})
    assert await m.wait(job.id, 30)
    assert "log truncado" not in job.log_path.read_text()
    await m.shutdown()


# --------------------------------------------------------------------------- no confirmados
async def test_report_json_is_written_atomically(manager: JobManager):
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    assert json.loads((job.dir / "report.json").read_text())["job_id"] == job.id
    assert not (job.dir / "report.json.tmp").exists()


async def test_acquire_does_not_swallow_an_external_cancellation(manager: JobManager):
    class SlowDying(asyncio.Lock):
        async def acquire(self) -> bool:  # tarda en morir al cancelarla
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                raise
            return True

    job = Job("abc123abc123", "x", {}, Plan("r", []), manager.config.jobs_dir / "x", Redactor(), 10)
    task = asyncio.create_task(manager._acquire(job, SlowDying(), time.monotonic() + 0.1))
    await asyncio.sleep(0.25)  # ya venció el plazo: ``await acq`` está esperando a que muera
    task.cancel()  # cierre del servidor
    with pytest.raises(asyncio.CancelledError):
        await task
