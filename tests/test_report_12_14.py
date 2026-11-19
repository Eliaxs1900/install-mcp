"""Issues #12 (relevant_lines/error con trazas) y #14 (built con jars viejos)."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from watcher_mcp.backends import BUILTIN, Backend, BackendParams, Plan, ResultContext
from watcher_mcp.backends.jvm import GradleBackend, MavenBackend
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager, JobState, LogLine, _result_info
from watcher_mcp.logsummary import select_relevant

# --- el log de la issue #12 (Maven con el puerto de depuración ocupado) -------------------------
STDERR_12 = [
    "ERROR: transport error 202: bind failed: Address already in use",
    "ERROR: JDWP Transport dt_socket failed to initialize, TRANSPORT_INIT(510)",
]
FRAMES = [f"[ERROR] \tat org.apache.maven.lifecycle.Frame{i}.execute(Frame{i}.java:{100 + i})" for i in range(13)]
STDOUT_12 = [
    "[INFO] Running com.x.FooTest",
    "[ERROR] The forked VM terminated without properly saying goodbye. VM crash or System.exit called?",
    "[ERROR] Error occurred in starting fork, check output in log",
    "[ERROR] Tests run: 0, Failures: 0, Errors: 0, Skipped: 0",
    "[INFO] BUILD FAILURE",
    "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.6.1:test "
    "(default-test) on project avm-firmware-tools:",
    "[ERROR] ",
    "[ERROR] The forked VM terminated without properly saying goodbye. VM crash or System.exit called?",
    "[ERROR] -> [Help 1]",
    *FRAMES,
    "[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.",
    "[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
    "[ERROR] For more information about the errors and possible solutions, please read the following articles:",
    "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoExecutionException",
]


def _fake_mvn(tmp_path: Path, stderr: list[str], stdout: list[str], code: int = 1) -> Path:
    script = tmp_path / "fake-mvn"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"for l in {stderr!r}:\n    print(l, file=sys.stderr, flush=True)\n"
        f"for l in {stdout!r}:\n    print(l, flush=True)\n"
        f"sys.exit({code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "pom.xml").write_text("<project><groupId>g</groupId><artifactId>a</artifactId><version>1</version></project>")
    return proj


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path / "state", allowed_backends=["maven", "gradle"], allowed_roots=[tmp_path],
                  max_parallel=2, max_wait_s=60)


async def test_issue_12_cause_survives_maven_trace(cfg, tmp_path):
    mvn = _fake_mvn(tmp_path, STDERR_12, STDOUT_12)
    c = Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": str(mvn)}}})
    m = JobManager(c, BUILTIN)
    job = await m.submit("maven", {"project_dir": str(_project(tmp_path)), "goals": ["test"]})
    assert await m.wait(job.id, 30)
    assert job.state == JobState.FAILED
    r = job.report
    text = "\n".join(r["relevant_lines"])
    assert "Address already in use" in text
    assert "The forked VM terminated" in text
    assert "[Help" not in text and "Re-run Maven" not in text and "To see the full stack" not in text
    assert "\tat " not in text and " at org.apache" not in text
    assert len(r["relevant_lines"]) <= 20
    # la línea repetida solo sale una vez
    assert sum("The forked VM terminated" in ln for ln in r["relevant_lines"]) == 1
    # error: "Failed to execute goal …:" más la causa
    assert r["error"].endswith("on project avm-firmware-tools: The forked VM terminated without properly "
                               "saying goodbye. VM crash or System.exit called?")
    # run == 0 con el job fallido: no se presenta como "0 ejecutados, 0 fallidos"
    assert r["headline"].endswith("tests: no llegaron a ejecutarse")
    assert "0 ejecutados" not in r["headline"]
    assert r["tests"]["executed"] is False
    # y un job fallido no lista jars (#14)
    assert "built" not in r["info"] and "stale" not in r["info"]
    await m.shutdown()


def _line(i: int, text: str, stream: str = "stdout") -> LogLine:
    return LogLine(i, 0.0, stream, text)


def test_select_relevant_first_and_last_in_order():
    lines = [_line(i, f"[ERROR] fallo número {i}") for i in range(100)]
    lines.insert(50, _line(999, "ERROR: la causa en stderr", "stderr"))
    got = [ln.text for ln in select_relevant(lines)]
    assert len(got) == 20
    assert "ERROR: la causa en stderr" in got
    assert got[0] == "[ERROR] fallo número 0" or got[0].startswith("ERROR: la causa")
    assert got[-1] == "[ERROR] fallo número 99"
    texts = [ln.text for ln in select_relevant(lines)]
    assert [t for t in texts if t.startswith("[ERROR] fallo")] == sorted(
        (t for t in texts if t.startswith("[ERROR] fallo")), key=lambda t: int(t.rsplit(" ", 1)[1]))


def test_python_traceback_and_gradle_and_npm_still_reported():
    tb = [
        "Traceback (most recent call last):",
        '  File "setup.py", line 3, in <module>',
        "    import foo",
        "ModuleNotFoundError: No module named 'foo'",
    ]
    got = [ln.text for ln in select_relevant([_line(i, t, "stderr") for i, t in enumerate(tb)])]
    assert got == ["Traceback (most recent call last):", "ModuleNotFoundError: No module named 'foo'"]
    gradle = ["FAILURE: Build failed with an exception.", "* Try:", "Run with --stacktrace option to get the stack trace.",
              "Get more help at https://help.gradle.org", "\tat org.gradle.Foo.run(Foo.java:1)"]
    assert [ln.text for ln in select_relevant([_line(i, t) for i, t in enumerate(gradle)])] == [
        "FAILURE: Build failed with an exception."]
    npm = ["npm ERR! code E404", "npm ERR! 404 Not Found - GET https://registry.npmjs.org/nope",
           "npm ERR! A complete log of this run can be found in: /root/.npm/_logs/x.log"]
    assert len(select_relevant([_line(i, t) for i, t in enumerate(npm)])) == 2


async def test_python_traceback_in_real_job(config, registry, tmp_path):
    """Un job de verdad (backend falso) que acaba con un traceback de Python sigue dando la causa."""
    m = JobManager(config, registry)
    job = await m.submit("fake", {"lines": 3, "interval": 0, "exit_code": 1})
    assert await m.wait(job.id, 30)
    assert any("something broke badly" in ln for ln in job.report["relevant_lines"])
    await m.shutdown()


# --- headline con run == 0 ---------------------------------------------------------------------
def test_headline_run_zero(cfg, tmp_path):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    spec = mvn.validate({"project_dir": str(_project(tmp_path)), "goals": ["test"]})
    zero = ["[WARNING] Tests run: 0, Failures: 0, Errors: 0, Skipped: 0"]
    bad = mvn.summarize(spec, zero, ok=False)
    assert bad["headline"] == "tests: no llegaron a ejecutarse" and bad["report"]["tests"]["executed"] is False
    good = mvn.summarize(spec, zero, ok=True)  # sin tests y todo bien: sigue siendo "0 ejecutados"
    assert good["headline"].startswith("tests: 0 ejecutados") and "executed" not in good["report"]["tests"]

    proj = tmp_path / "g"
    proj.mkdir()
    (proj / "settings.gradle").write_text("")
    (proj / "gradlew").write_text("#!/bin/sh\n")
    g = GradleBackend(cfg)
    gspec = g.validate({"project_dir": str(proj), "tasks": ["test"]})
    out = ["0 tests completed"]
    assert g.summarize(gspec, out, ok=False)["headline"] == "tests: no llegaron a ejecutarse"
    assert g.summarize(gspec, out, ok=True)["headline"].startswith("tests: 0 ejecutados")


def test_maven_error_detail_skips_noise(cfg, tmp_path):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    spec = mvn.validate({"project_dir": str(_project(tmp_path)), "goals": ["package"]})
    out = ["[ERROR] Failed to execute goal x:y:1:z (id) on project p:", "[ERROR] ", "[ERROR] -> [Help 1]",
           "[ERROR] Compilation failure: cannot find symbol"]
    assert mvn.summarize(spec, out, ok=False)["error_detail"].endswith("on project p: Compilation failure: cannot find symbol")
    # si la línea ya trae la causa, no se añade nada
    one = ["[ERROR] Failed to execute goal x on project p: There are test failures.", "[ERROR] otra cosa"]
    assert mvn.summarize(spec, one, ok=False)["error_detail"] == "Failed to execute goal x on project p: There are test failures."


# --- #14: built / stale ------------------------------------------------------------------------
def _jar(d: Path, name: str, mtime: float) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"")
    os.utime(d / name, (mtime, mtime))


def test_built_vs_stale_by_mtime_maven(cfg, tmp_path):
    mvn = MavenBackend(Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": "/bin/echo"}}}))
    proj = _project(tmp_path)
    spec = mvn.validate({"project_dir": str(proj), "goals": ["package"]})
    started = time.time() - 30
    _jar(proj / "target", "old.jar", started - 3600)
    _jar(proj / "target", "new.jar", started + 5)
    ok = mvn.result_info(spec, ResultContext(started_at=started, ok=True, state="succeeded"))
    assert ok["built"] == ["target/new.jar"] and ok["stale"] == ["target/old.jar"]
    # un job que no acaba en succeeded no lista ninguno
    for state in ("failed", "cancelled", "timed_out"):
        bad = mvn.result_info(spec, ResultContext(started_at=started, ok=False, state=state))
        assert "built" not in bad and "stale" not in bad
    # llamada antigua sin contexto: comportamiento previo (todos en built)
    assert mvn.result_info(spec)["built"] == ["target/new.jar", "target/old.jar"]


def test_built_vs_stale_by_mtime_gradle(cfg, tmp_path):
    proj = tmp_path / "g"
    proj.mkdir()
    (proj / "settings.gradle").write_text("")
    (proj / "gradlew").write_text("#!/bin/sh\n")
    g = GradleBackend(cfg)
    spec = g.validate({"project_dir": str(proj), "tasks": ["test"]})
    started = time.time() - 30
    _jar(proj / "build" / "libs", "app-old.jar", started - 100)
    info = g.result_info(spec, ResultContext(started_at=started, ok=True, state="succeeded"))
    assert "built" not in info and info["stale"] == ["build/libs/app-old.jar"]  # `test` no genera jar
    _jar(proj / "build" / "libs", "app.jar", started + 1)
    info = g.result_info(spec, ResultContext(started_at=started, ok=True, state="succeeded"))
    assert info["built"] == ["build/libs/app.jar"]


async def test_built_in_real_job_succeeded_and_failed(cfg, tmp_path):
    proj = _project(tmp_path)
    _jar(proj / "target", "old.jar", time.time() - 7200)
    mvn = _fake_mvn(tmp_path, [], ["[INFO] BUILD SUCCESS"], code=0)
    # el "mvn" falso también escribe un jar nuevo
    mvn.write_text(mvn.read_text().replace(
        "import sys\n", f"import sys, pathlib\npathlib.Path({str(proj / 'target' / 'new.jar')!r}).write_bytes(b'x')\n", 1))
    c = Config(**{**cfg.__dict__, "backends": {"maven": {"mvn_path": str(mvn)}}})
    m = JobManager(c, BUILTIN)
    job = await m.submit("maven", {"project_dir": str(proj), "goals": ["package"]})
    assert await m.wait(job.id, 30)
    assert job.state == JobState.SUCCEEDED, job.error
    assert job.report["info"]["built"] == ["target/new.jar"]
    assert job.report["info"]["stale"] == ["target/old.jar"]
    await m.shutdown()


# --- terceros: result_info(self, spec) con la firma antigua -------------------------------------
class _OldParams(BackendParams):
    pass


class _OldBackend(Backend[_OldParams]):
    name = "old"
    description = "firma antigua"
    Params = _OldParams

    def plan(self, spec: _OldParams) -> Plan:  # pragma: no cover - no se ejecuta
        raise NotImplementedError

    def result_info(self, spec):  # type: ignore[override]
        return {"viejo": True}


class _NewBackend(_OldBackend):
    def result_info(self, spec, context=None):  # type: ignore[override]
        return {"ok": context.ok if context else None}


class _KwBackend(_OldBackend):
    def result_info(self, spec, **kw):  # type: ignore[override]
        return {"ctx": kw["context"].started_at}


def test_result_info_signature_is_backwards_compatible(cfg):
    ctx = ResultContext(started_at=5.0, ok=True, state="succeeded")
    assert _result_info(_OldBackend(cfg), None, ctx) == {"viejo": True}
    assert _result_info(_NewBackend(cfg), None, ctx) == {"ok": True}
    assert _result_info(_KwBackend(cfg), None, ctx) == {"ctx": 5.0}
