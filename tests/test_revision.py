"""Un test por hallazgo de la revisión de seguridad y fiabilidad (rama fix/revision-seguridad-fiabilidad)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from watcher_mcp.backends import BUILTIN, Backend, BackendParams, FuncStep, Plan
from watcher_mcp.backends.download import extract
from watcher_mcp.config import Config
from watcher_mcp.jobs import Job, JobError, JobManager, JobState
from watcher_mcp.process import _pump
from watcher_mcp.security import Redactor, ensure_safe_path


def backend(cfg: Config, name: str) -> Backend[Any]:
    return BUILTIN[name](cfg)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path / "state", allowed_backends=list(BUILTIN), allowed_roots=[tmp_path])


# ------------------------------------------------------------------ #1 rutas protegidas + overwrite
def test_dotfiles_and_hidden_dirs_are_protected(cfg: Config, tmp_path: Path):
    download = backend(cfg, "download")
    for dest in [tmp_path / ".zshrc", tmp_path / ".ssh" / "authorized_keys",
                 tmp_path / "proyecto" / ".git" / "hooks" / "pre-commit"]:
        with pytest.raises(ValueError, match="zona protegida"):
            download.validate({"url": "https://example.com/x", "dest": str(dest)})
    download.validate({"url": "https://example.com/x", "dest": str(tmp_path / "proyecto" / "x.bin")})


def test_protected_zone_allowed_only_as_explicit_root(tmp_path: Path):
    local_bin = tmp_path / ".local" / "bin"
    with pytest.raises(ValueError, match="zona protegida"):
        ensure_safe_path(local_bin / "jq", [tmp_path])
    assert ensure_safe_path(local_bin / "jq", [tmp_path, local_bin]) == (local_bin / "jq").resolve()


def test_home_library_is_protected(tmp_path: Path):
    agent = tmp_path / "Library" / "LaunchAgents" / "evil.plist"
    with pytest.raises(ValueError, match="Library"):
        ensure_safe_path(agent, [tmp_path], home=tmp_path, protected_home_dirs=["Library"])
    ensure_safe_path(tmp_path / "Proyectos" / "a", [tmp_path], home=tmp_path, protected_home_dirs=["Library"])


def test_downloads_do_not_overwrite_by_default(cfg: Config, tmp_path: Path):
    spec = backend(cfg, "download").validate({"url": "https://example.com/x", "dest": str(tmp_path / "x")})
    assert spec.overwrite is False
    bspec = backend(cfg, "binary").validate({"url": "https://example.com/jq", "name": "jq",
                                             "checksum": "sha256:" + "0" * 64})
    assert bspec.overwrite is False


# ------------------------------------------------------------------ #2 / #16 lecturas sensibles
def test_requirements_must_be_inside_safe_roots(cfg: Config, tmp_path: Path):
    pip = backend(cfg, "pip")
    outside = Path(os.path.realpath("/etc/hosts"))
    with pytest.raises(ValueError, match="fuera de las raíces"):
        pip.validate({"requirements": str(outside)})
    key = tmp_path / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    with pytest.raises(ValueError, match="zona protegida"):
        pip.validate({"requirements": str(key)})
    ok = tmp_path / "requirements.txt"
    ok.write_text("pypdf\n")
    pip.validate({"requirements": str(ok)})


def test_pip_python_must_be_an_interpreter(cfg: Config, tmp_path: Path):
    evil = tmp_path / "payload"
    evil.write_text("#!/bin/sh\n")
    evil.chmod(0o755)
    with pytest.raises(ValueError, match="intérprete"):
        backend(cfg, "pip").validate({"packages": ["x"], "python": str(evil)})


def test_git_local_source_must_be_safe(cfg: Config, tmp_path: Path):
    hidden = tmp_path / ".secret-repo"
    hidden.mkdir()
    with pytest.raises(ValueError, match="zona protegida"):
        backend(cfg, "git").validate({"url": str(hidden), "dest": str(tmp_path / "clone")})


# ------------------------------------------------------------------ #4 git: repos ajenos y endurecimiento
def test_git_refuses_to_update_foreign_repos(cfg: Config, tmp_path: Path):
    git = backend(cfg, "git")
    repo = tmp_path / "ajeno"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="trust_existing"):
        git.validate({"url": "https://example.com/r.git", "dest": str(repo)})
    git.validate({"url": "https://example.com/r.git", "dest": str(repo), "trust_existing": True})
    (repo / ".git" / "watcher-mcp-clone").write_text("x")
    git.validate({"url": "https://example.com/r.git", "dest": str(repo)})  # clonado por watcher-mcp


def test_git_commands_neutralise_repo_config(cfg: Config, tmp_path: Path):
    git = backend(cfg, "git")
    plan = git.plan(git.validate({"url": "https://example.com/r.git", "dest": str(tmp_path / "nuevo")}))
    clone = plan.steps[0].argv
    assert "core.hooksPath=/dev/null" in clone and "core.fsmonitor=false" in clone and "credential.helper=" in clone
    assert [s.name for s in plan.steps][:2] == ["clone", "mark"]


# ------------------------------------------------------------------ #18 npm sin scripts por defecto
def test_npm_ignores_lifecycle_scripts_by_default(cfg: Config):
    npm = backend(cfg, "npm")
    plan = npm.plan(npm.validate({"packages": ["left-pad"]}))
    assert "--ignore-scripts" in plan.steps[0].argv


# ------------------------------------------------------------------ #5 varias instancias
async def test_other_instance_does_not_hijack_a_live_job(config: Config, registry):
    a = JobManager(config, registry)
    job = await a.submit("fake", {"lines": 40, "interval": 0.05})
    while job.state != JobState.RUNNING:
        await asyncio.sleep(0.02)
    b = JobManager(config, registry)
    b.load_history()  # otra sesión de Claude Code arranca su servidor
    on_disk = json.loads((job.dir / "job.json").read_text())
    assert on_disk["state"] == "running", "B no debe marcar como fallido un job vivo de A"
    seen = b.get(job.id)
    assert seen.foreign and seen.status()["foreign"]
    with pytest.raises(JobError, match="otra sesión"):
        await b.cancel(job.id)
    await a.wait(job.id, 20)
    assert job.state == JobState.SUCCEEDED
    await a.shutdown()
    await b.shutdown()


# ------------------------------------------------------------------ #6 huérfanos al morir el servidor
async def test_orphans_of_a_dead_instance_are_reaped(config: Config, registry):
    a = JobManager(config, registry)
    job = await a.submit("fake", {"lines": 400, "interval": 0.05})
    while job.pid is None:
        await asyncio.sleep(0.02)
    pid = job.pid
    a._release_instance()  # simula kill -9: su flock desaparece, el proceso sigue vivo
    os.kill(pid, 0)  # sigue vivo
    b = JobManager(config, registry)
    b.load_history()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("el proceso huérfano sigue vivo")
    assert b.get(job.id).state == JobState.FAILED and "terminó" in b.get(job.id).error
    await a.wait(job.id, 10)
    await a.shutdown()
    await b.shutdown()


# ------------------------------------------------------------------ #7 / #8 cancelación de pasos internos
class SlowFuncParams(BackendParams):
    seconds: float = 6.0
    marks: str


class SlowFunc(Backend[SlowFuncParams]):
    """Un paso interno que trabaja en un hilo y NO atiende la cancelación enseguida (como una
    descarga bloqueada en un read de red)."""

    name = "slowfunc"
    description = "pruebas"
    Params = SlowFuncParams

    def plan(self, spec: SlowFuncParams) -> Plan:
        marks = Path(spec.marks)

        async def run(ctx) -> None:  # type: ignore[no-untyped-def]
            with marks.open("a") as fh:
                fh.write(f"start {time.monotonic()}\n")
            await asyncio.to_thread(time.sleep, spec.seconds)
            with marks.open("a") as fh:
                fh.write(f"end {time.monotonic()}\n")

        return Plan(summary="slow", steps=[FuncStep("slow", run)], lock_key="mismo-destino")


async def test_cancelled_thread_keeps_its_lock_until_it_really_stops(config: Config, registry, tmp_path: Path):
    config.allowed_backends = ["slowfunc"]
    config.func_cancel_grace_s = 15
    m = JobManager(config, {**registry, "slowfunc": SlowFunc})
    marks = tmp_path / "marks.txt"
    first = await m.submit("slowfunc", {"seconds": 6.0, "marks": str(marks)})
    while first.state != JobState.RUNNING:
        await asyncio.sleep(0.02)
    await m.cancel(first.id)
    second = await m.submit("slowfunc", {"seconds": 0.1, "marks": str(marks)})  # reintento inmediato
    await m.wait(second.id, 30)
    events = [line.split() for line in marks.read_text().splitlines()]
    first_end = float(events[1][1])
    second_start = float(events[2][1])
    assert events[1][0] == "end" and events[2][0] == "start"
    assert second_start >= first_end, "el reintento empezó mientras el hilo cancelado seguía escribiendo"
    assert first.state == JobState.CANCELLED
    await m.shutdown()


def test_part_files_are_unique_per_job(cfg: Config, tmp_path: Path):
    download = backend(cfg, "download")
    spec = download.validate({"url": "https://example.com/fw.image", "dest": str(tmp_path) + "/"})

    def part_of(plan: Plan) -> str:
        cells = plan.steps[0].func.__closure__ or ()  # type: ignore[union-attr]
        return next(str(c.cell_contents) for c in cells if str(c.cell_contents).endswith(".part"))

    assert part_of(download.plan(spec)) != part_of(download.plan(spec))


# ------------------------------------------------------------------ #9 límites al extraer + overwrite
def _tar(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def test_extract_stops_decompression_bombs_and_cleans_up(tmp_path: Path):
    archive = _tar(tmp_path / "bomb.tar.gz", {"a.bin": b"\0" * (2 << 20), "b.bin": b"\0" * 10})
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="max_extract_bytes"):
        extract(archive, out, log=lambda m: None, max_bytes=1 << 20)
    assert not any(p.is_file() for p in out.rglob("*")), "debe borrar lo escrito"
    with pytest.raises(ValueError, match="max_extract_files"):
        extract(archive, out, log=lambda m: None, max_files=1)


def test_extract_respects_overwrite_false(tmp_path: Path):
    archive = _tar(tmp_path / "a.tar.gz", {"conf.txt": b"nuevo"})
    out = tmp_path / "out"
    out.mkdir()
    (out / "conf.txt").write_text("original")
    with pytest.raises(ValueError, match="overwrite=false"):
        extract(archive, out, log=lambda m: None, overwrite=False)
    assert (out / "conf.txt").read_text() == "original"


# ------------------------------------------------------------------ #10 tail con memoria acotada
def test_tail_from_disk_returns_only_what_was_asked(tmp_path: Path):
    job = Job("abc", "fake", {}, Plan("s", []), tmp_path / "j", Redactor(), memory_lines=10)
    job.open_log()
    for i in range(1000):
        job.add_line("stdout", f"line {i}")
    job.close_log()
    lines = job.tail(5, since_seq=100)  # anterior al búfer en memoria: se lee del disco
    assert [ln.text for ln in lines] == [f"line {i}" for i in range(995, 1000)]
    assert [ln.seq for ln in job.tail(3, since_seq=0)] == [998, 999, 1000]


# ------------------------------------------------------------------ #11 _finish siempre libera
async def test_finish_failure_still_marks_the_job_done(manager: JobManager, monkeypatch):
    def broken(*a: Any, **k: Any) -> dict[str, Any]:
        raise OSError("disco lleno")

    monkeypatch.setattr(manager, "_report", broken)
    job = await manager.submit("fake", {"lines": 1})
    assert await manager.wait(job.id, 15), "done debe activarse aunque falle el cierre"
    assert job.state in (JobState.SUCCEEDED, JobState.FAILED) and job.report is not None


# ------------------------------------------------------------------ #12 job.json raro no impide arrancar
def test_unreadable_job_json_does_not_break_startup(config: Config, registry):
    bad = config.jobs_dir / "roto"
    bad.mkdir(parents=True)
    (bad / "job.json").write_text(json.dumps({"job_id": "roto", "created_at": "no-es-fecha"}))
    m = JobManager(config, registry)
    m.load_history()  # antes: KeyError/ValueError y el servidor no arrancaba
    assert "roto" not in m.jobs


# ------------------------------------------------------------------ #13 UTF-8 partido entre lecturas
async def test_utf8_split_across_reads():
    reader = asyncio.StreamReader()
    got: list[str] = []
    task = asyncio.create_task(_pump(reader, "stdout", lambda s, t: got.append(t)))
    raw = "configuración ñ 🚀\n".encode()
    for i in range(len(raw)):  # byte a byte: cada multibyte llega partido
        reader.feed_data(raw[i:i + 1])
        await asyncio.sleep(0)
    reader.feed_eof()
    await task
    assert got == ["configuración ñ 🚀"]


# ------------------------------------------------------------------ #14 tope duro
def test_no_limit_means_up_to_the_hard_cap(config: Config, registry):
    config.max_job_timeout_s = 120
    m = JobManager(config, registry)
    fake = m.backend("fake")
    assert m.limits(fake, Plan("s", [], timeout_s=0)).job_timeout_s == 120
    assert m.limits(fake, Plan("s", [], timeout_s=30)).job_timeout_s == 30
    assert m.limits(fake, Plan("s", [], timeout_s=9999)).job_timeout_s == 120


# ------------------------------------------------------------------ #17 secretos en parámetros por clave
def test_params_are_redacted_by_key_name():
    r = Redactor()
    data = {"url": "https://u:p@h/x", "headers": {"api_token": "abc123456"}, "list": [{"password": "x1y2z3"}],
            "name": "visible"}
    out = r.redact_obj(data)
    assert out["headers"]["api_token"] == "***" and out["list"][0]["password"] == "***"
    assert out["name"] == "visible" and "u:p@" not in out["url"]


async def test_secret_params_never_reach_job_json(manager: JobManager):
    job = await manager.submit("fake", {"lines": 1, "secret": "zz-top-secret"})
    await manager.wait(job.id, 15)
    assert "zz-top-secret" not in (job.dir / "job.json").read_text()
    assert job.params["secret"] == "***"


# ------------------------------------------------------------------ #15 tests aislados de git global
def test_tests_do_not_inherit_user_git_config():
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull and os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
