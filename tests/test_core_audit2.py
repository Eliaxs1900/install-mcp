"""Núcleo 2: efectos secundarios de los arreglos del núcleo (auditoría, verificación de la 2.ª tanda)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from watcher_mcp import jobs as jobs_mod
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager, JobState
from watcher_mcp.process import ProcResult

from .test_core_audit import until


def _age(path: Path, days: float = 8) -> None:
    past = time.time() - days * 86400
    for p in (path, *path.rglob("*")):
        os.utime(p, (past, past))


# --------------------------------------------------------------------------- _sweep_unreadable
async def test_sweep_never_deletes_a_valid_job_of_another_session(config, registry):
    b = JobManager(config, registry)  # arranca antes de que A lance nada
    a = JobManager(config, registry)
    job = await a.submit("fake", {"lines": 2})
    assert await a.wait(job.id, 20)
    _age(job.dir)  # un job válido y terminado de otra sesión, de hace 8 días, que B aún no había cargado
    assert job.id not in b.jobs
    b._prune()  # lo que ejecuta B al terminar cualquier job suyo
    assert (job.dir / "job.json").exists() and (job.dir / "output.log").exists()
    assert b.get(job.id).state == JobState.SUCCEEDED  # y B lo ve
    await a.shutdown()
    await b.shutdown()


def test_sweep_only_deletes_dirs_without_a_json_job(config, registry):
    jobs = config.jobs_dir
    cases = {"nojson": None, "basura": "esto no es json {", "otraversion": json.dumps({"v": 99, "x": 1})}
    for name, content in cases.items():
        d = jobs / f"dead{name}"
        d.mkdir(parents=True)
        (d / "output.log").write_text("x\n")
        if content is not None:
            (d / "job.json").write_text(content)
        _age(d)
    m = JobManager(config, registry)
    m.load_history()
    assert not (jobs / "deadnojson").exists() and not (jobs / "deadbasura").exists()
    assert (jobs / "deadotraversion").exists()  # JSON legible de una versión que no entendemos: ante la duda, no


# --------------------------------------------------------------------------- _sync_foreign
async def test_sync_foreign_does_not_reread_the_log(config, registry, monkeypatch):
    a = JobManager(config, registry)
    job = await a.submit("fake", {"lines": 40, "interval": 0.05})
    await until(lambda: job.state == JobState.RUNNING)
    b = JobManager(config, registry)
    b.load_history()
    assert b.get(job.id).foreign
    opened: list[str] = []
    real_open = open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(jobs_mod, "open", spy, raising=False)
    for _ in range(5):
        b.get(job.id)
        b.list()
    assert not [p for p in opened if p.endswith("output.log")], opened  # solo job.json
    assert await b.wait(job.id, 30)
    assert not [p for p in opened if p.endswith("output.log")]
    monkeypatch.undo()
    seen = b.get(job.id)
    assert seen.state == JobState.SUCCEEDED and seen.last_seq >= 40  # cursor del job.json
    # el log se lee al pedirlo, y el cursor sigue al día
    assert [ln.text for ln in seen.tail(3)]
    await a.shutdown()
    await b.shutdown()


# --------------------------------------------------------------------------- log truncado
async def test_truncated_log_report_uses_the_end_not_the_cut(config, registry):
    cfg = Config(**{**config.__dict__, "max_log_bytes": 2000, "memory_log_lines": 60})
    m = JobManager(cfg, registry)
    job = await m.submit("fake", {"lines": 600, "interval": 0, "exit_code": 1})
    assert await m.wait(job.id, 30)
    assert job.state == JobState.FAILED and job.log_truncated
    r = job.report
    assert any("something broke badly" in ln for ln in r["relevant_lines"]), r["relevant_lines"]  # antes: []
    assert "job failed" in r["log_tail"][-1] or any("something broke" in ln for ln in r["log_tail"])
    assert not any("se alcanzó max_log_bytes" in ln for ln in r["log_tail"])
    assert r["log_truncated"]["max_log_bytes"] == 2000 and "log truncado" in r["headline"]
    # logs con tail mayor que la memoria: principio del disco + final de la memoria, sin la nota del corte
    lines = job.tail(300)
    seqs = [ln.seq for ln in lines]
    assert seqs == sorted(seqs) and lines[-1].seq == job.last_seq
    assert not any("se alcanzó max_log_bytes" in ln.text for ln in lines)
    assert job.status()["log_truncated"] is True
    await m.shutdown()


async def test_truncated_flag_survives_a_restart(config, registry):
    cfg = Config(**{**config.__dict__, "max_log_bytes": 2000})
    a = JobManager(cfg, registry)
    job = await a.submit("fake", {"lines": 300, "interval": 0})
    assert await a.wait(job.id, 30)
    await a.shutdown()
    b = JobManager(cfg, registry)
    b.load_history()
    assert b.get(job.id).log_truncated and b.get(job.id).last_seq == job.last_seq
    await b.shutdown()


# --------------------------------------------------------------------------- escaped en el informe
async def test_escaped_processes_reach_the_report(manager: JobManager, monkeypatch: pytest.MonkeyPatch):
    async def fake_run_command(argv, **kw):
        return ProcResult(exit_code=0, pid=1, escaped=[111, 222])

    monkeypatch.setattr(jobs_mod, "run_command", fake_run_command)
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 20)
    assert job.report["escaped"] == [111, 222]
    assert "2 proceso(s) escaparon del grupo" in job.report["headline"]
    clean = await manager.submit("fake", {"lines": 1})  # sin escapados: nada nuevo en el informe
    monkeypatch.setattr(jobs_mod, "run_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert await manager.wait(clean.id, 20)
