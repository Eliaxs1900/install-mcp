"""Regresiones de las issues #4 (hijos que ignoran SIGTERM) y #5 (informe del backend maven)."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from watcher_mcp import process
from watcher_mcp.backends import BUILTIN
from watcher_mcp.backends.jvm import GradleBackend, MavenBackend
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager, JobState
from watcher_mcp.process import build_env, run_command

# Un hijo del grupo que ignora SIGTERM y NO comparte nuestras tuberías, como el JVM de surefire
# suspendido por JDWP de la issue #4: el líder (sh) muere con SIGTERM; el hijo y su sleep
# (que hereda SIG_IGN) no.
STUBBORN = ["sh", "-c", 'sh -c \'trap "" TERM; sleep 300\' </dev/null >/dev/null 2>&1 & sleep 300']


@pytest.fixture
def fast_grace(monkeypatch):
    monkeypatch.setattr(process, "TERM_GRACE_S", 1.0)
    monkeypatch.setattr(process, "KILL_GRACE_S", 2.0)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        state_dir=tmp_path / "state",
        allowed_backends=list(BUILTIN),
        allowed_roots=[tmp_path],
        backends={"run": {"programs": ["sh"]}},
    )


async def _members(pgid: int) -> list[int]:
    return await process._group_alive(pgid)


# ------------------------------------------------------------------ issue #4
@pytest.mark.skipif(not shutil.which("ps"), reason="sin ps")
async def test_timeout_kills_children_that_ignore_sigterm(fast_grace):
    lines: list[str] = []
    t0 = time.monotonic()
    res = await run_command(STUBBORN, env=build_env({}), cwd=None, on_line=lambda s, t: lines.append(t),
                            cancel_event=asyncio.Event(), timeout_s=1.0)
    assert res.timed_out
    assert res.killed, "el hijo que ignora SIGTERM debe acabar con SIGKILL"
    assert res.survivors == []
    assert await _members(res.pid) == [], "no debe quedar nadie vivo en el grupo"
    assert time.monotonic() - t0 < 1 + 1 + 2 + 3


@pytest.mark.skipif(not shutil.which("ps"), reason="sin ps")
async def test_cancel_kills_children_that_ignore_sigterm(fast_grace):
    ev = asyncio.Event()
    task = asyncio.create_task(run_command(STUBBORN, env=build_env({}), cwd=None, on_line=lambda s, t: None,
                                           cancel_event=ev, timeout_s=None))
    await asyncio.sleep(0.5)
    ev.set()
    res = await task
    assert res.cancelled and res.killed and await _members(res.pid) == []


@pytest.mark.skipif(not shutil.which("ps"), reason="sin ps")
async def test_leader_exits_but_child_keeps_our_pipe_open(fast_grace):
    """El proceso principal acaba bien, pero deja un hijo con nuestra stdout abierta: el paso
    debe terminar con su código (no quedarse colgado esperando la tubería) y el hijo, morir."""
    t0 = time.monotonic()
    lines: list[str] = []
    res = await asyncio.wait_for(
        run_command(["sh", "-c", "echo hola; sleep 300 & exit 0"], env=build_env({}), cwd=None,
                    on_line=lambda s, t: lines.append(t), cancel_event=asyncio.Event(), timeout_s=None),
        30,
    )
    assert res.exit_code == 0 and not res.timed_out and "hola" in lines
    assert time.monotonic() - t0 < process.DRAIN_S + 1 + 2 + 3
    assert await _members(res.pid) == []


@pytest.mark.skipif(not shutil.which("ps"), reason="sin ps")
async def test_obedient_group_reports_nothing(fast_grace):
    res = await run_command(["sh", "-c", "sleep 300 & sleep 300"], env=build_env({}), cwd=None,
                            on_line=lambda s, t: None, cancel_event=asyncio.Event(), timeout_s=0.5)
    assert res.timed_out and res.killed == [] and res.survivors == []


@pytest.mark.skipif(not shutil.which("ps"), reason="sin ps")
async def test_job_report_mentions_killed_children(cfg, tmp_path, fast_grace):
    m = JobManager(cfg, BUILTIN)
    job = await m.submit("run", {"program": "sh", "args": STUBBORN[1:], "cwd": str(tmp_path), "job_timeout_s": 1})
    assert await m.wait(job.id, 20)
    assert job.state == JobState.TIMED_OUT
    r = job.report
    assert r["killed_after_sigterm"] and "SIGKILL" in r["headline"] and "orphans" not in r
    assert any("ignoraron SIGTERM" in ln.text for ln in job.tail(50))
    await m.shutdown()


# ------------------------------------------------------------------ issue #5: validación y plan
def test_gav_accepts_hyphenated_types_and_explains_format(cfg):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    for a in ["org.apache.maven.plugins:maven-shade-plugin:3.6.0:maven-plugin",
              "com.x:lib:1.0:test-jar:tests", "org.slf4j:slf4j-api:2.0.16"]:
        mvn.validate({"artifacts": [a]})
    with pytest.raises(ValueError, match="groupId:artifactId:version"):
        mvn.validate({"artifacts": ["solo-un-nombre"]})


def test_ntp_only_in_project_mode(cfg, tmp_path):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    art = mvn.plan(mvn.validate({"artifacts": ["org.slf4j:slf4j-api:2.0.16"]}))
    assert "-ntp" not in art.steps[0].argv  # para ver "Downloaded from …"
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "pom.xml").write_text("<project/>")
    plan = mvn.plan(mvn.validate({"project_dir": str(proj), "goals": ["test"]}))
    assert "-ntp" in plan.steps[0].argv


# ------------------------------------------------------------------ issue #5: resumen
SUREFIRE_OK = """\
[INFO] Running com.eliax.avm.FooTest
[INFO] Tests run: 40, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.3 s - in com.eliax.avm.FooTest
[INFO] Tests run: 21, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 0.2 s - in com.eliax.avm.BarTest
[INFO] Results:
[WARNING] Tests run: 61, Failures: 0, Errors: 0, Skipped: 1
[INFO] BUILD SUCCESS""".splitlines()

SUREFIRE_FAIL = """\
[ERROR] Tests run: 5, Failures: 1, Errors: 1, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! - in com.x.YTest
[ERROR] com.x.YTest.parsesHeader -- Time elapsed: 0.01 s <<< FAILURE!
[ERROR] com.x.YTest.readsBlock -- Time elapsed: 0.02 s <<< ERROR!
[ERROR] Tests run: 5, Failures: 1, Errors: 1, Skipped: 0
[INFO] BUILD FAILURE
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test (default-test) on project x: There are test failures.""".splitlines()


def _maven(cfg, tmp_path, **params):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>com.eliax.avm</groupId>'
        "<artifactId>avm-firmware-tools</artifactId><version>0.1.0-SNAPSHOT</version></project>"
    )
    return mvn, mvn.validate({"project_dir": str(proj), **params}), proj


def test_maven_tests_summary_ok_and_fail(cfg, tmp_path):
    mvn, spec, _ = _maven(cfg, tmp_path, goals=["test"])
    s = mvn.summarize(spec, SUREFIRE_OK, ok=True)
    assert s["report"]["tests"] == {"run": 61, "failures": 0, "errors": 0, "skipped": 1, "passed": 60}
    assert s["headline"].startswith("tests: 61 ejecutados, 0 fallidos")
    s = mvn.summarize(spec, SUREFIRE_FAIL, ok=False)
    t = s["report"]["tests"]
    assert (t["run"], t["failures"], t["errors"]) == (5, 1, 1)
    assert t["failed_tests"] == ["com.x.YTest.parsesHeader", "com.x.YTest.readsBlock"]
    assert s["error_detail"].startswith("Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin")


def test_maven_project_mode_installs_nothing(cfg, tmp_path):
    mvn, spec, proj = _maven(cfg, tmp_path, goals=["test"])
    (proj / "target").mkdir()
    (proj / "target" / "avm-firmware-tools-0.1.0-SNAPSHOT.jar").write_bytes(b"")
    assert asyncio.run(mvn.detect_versions(spec, None)) == {}
    info = mvn.result_info(spec)
    assert info["project"] == "com.eliax.avm:avm-firmware-tools:0.1.0-SNAPSHOT"
    assert info["built"] == ["target/avm-firmware-tools-0.1.0-SNAPSHOT.jar"]
    _, spec_install, _ = _maven(cfg, tmp_path, goals=["install"])
    assert asyncio.run(mvn.detect_versions(spec_install, None)) == {
        "com.eliax.avm:avm-firmware-tools": "0.1.0-SNAPSHOT (en ~/.m2)"}


def test_maven_artifacts_downloads_and_offline_hint(cfg, tmp_path):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    spec = mvn.validate({"artifacts": ["org.apache.maven.plugins:maven-shade-plugin:3.6.0"]})
    out = [
        "Downloading from central: https://repo.maven.apache.org/maven2/org/ow2/asm/asm/9.7/asm-9.7.jar",
        "Downloaded from central: https://repo.maven.apache.org/maven2/org/ow2/asm/asm/9.7/asm-9.7.jar (125 kB at 1.2 MB/s)",
        "Downloaded from central: https://repo.maven.apache.org/maven2/org/jdom/jdom2/2.0.6.1/jdom2-2.0.6.1.jar (328 kB at 2 MB/s)",
    ]
    s = mvn.summarize(spec, out, ok=True)
    assert s["report"]["downloads"] == {"count": 2, "files": ["asm-9.7.jar", "jdom2-2.0.6.1.jar"],
                                        "already_present": False}
    assert mvn.summarize(spec, ["[INFO] BUILD SUCCESS"], ok=True)["report"]["downloads"]["already_present"] is True

    _, spec_off, _ = _maven(cfg, tmp_path, goals=["package"], offline=True)
    fail = ["[ERROR] Failed to execute goal on project x: Plugin org.apache.maven.plugins:maven-shade-plugin:3.6.0 "
            "or one of its dependencies could not be resolved: Cannot access central in offline mode and the "
            "artifact org.ow2.asm:asm:jar:9.7 has not been downloaded from it before."]
    s = mvn.summarize(spec_off, fail, ok=False)
    assert "offline=false" in s["hint"] and s["error_detail"].startswith("Failed to execute goal")


def test_gradle_summary(cfg, tmp_path):
    proj = tmp_path / "g"
    proj.mkdir()
    (proj / "settings.gradle").write_text("")
    (proj / "gradlew").write_text("#!/bin/sh\n")
    g = GradleBackend(cfg)
    spec = g.validate({"project_dir": str(proj), "tasks": ["test"]})
    out = ["FooTest > parsesHeader() FAILED", "    java.lang.AssertionError at FooTest.java:12",
           "61 tests completed, 1 failed, 2 skipped", "* What went wrong:", "Execution failed for task ':test'."]
    s = g.summarize(spec, out, ok=False)
    assert s["report"]["tests"]["run"] == 61 and s["report"]["tests"]["failures"] == 1
    assert s["report"]["tests"]["failed_tests"] == ["FooTest > parsesHeader()"]
    assert s["error_detail"] == "Execution failed for task ':test'."
    assert asyncio.run(g.detect_versions(spec, None)) == {}


async def test_end_to_end_report_with_fake_mvn(cfg, tmp_path):
    """El resumen llega al informe real del job (titular, tests, installed vacío)."""
    fake = tmp_path / "fake-mvn"
    fake.write_text("#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in SUREFIRE_OK))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    c = Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": str(fake)}}})
    m = JobManager(c, BUILTIN)
    proj = tmp_path / "proj2"
    proj.mkdir()
    (proj / "pom.xml").write_text("<project><groupId>g</groupId><artifactId>a</artifactId><version>1</version></project>")
    job = await m.submit("maven", {"project_dir": str(proj), "goals": ["test"]})
    assert await m.wait(job.id, 20)
    r = job.report
    assert job.state == JobState.SUCCEEDED, job.error
    assert r["installed"] == {} and r["tests"]["run"] == 61 and "tests: 61 ejecutados" in r["headline"]
    assert r["info"]["project"] == "g:a:1"
    await m.shutdown()


def test_sys_executable_present():  # sanidad del entorno de tests
    assert os.path.exists(sys.executable)
