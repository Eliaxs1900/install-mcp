"""Informe de los trabajos que se callan: ``before_silence``, pistas de «esperando entrada» y causa probable.

Los casos salen de cinco jobs reales (Maven con el puerto de depuración ocupado, Maven offline,
espera al depurador con timeout y cancelada, pip correcto). Los fixtures son sintéticos: rutas y
nombres inventados, sin datos de nadie.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from watcher_mcp.backends import Backend, BackendParams, CommandStep, Plan
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager, JobState, LogLine
from watcher_mcp.logsummary import before_silence, probable_cause, wait_hint

JDWP = "[INFO] Listening for transport dt_socket at address: 5005"
MAVEN_HEAD = [
    "[INFO] Scanning for projects...",
    "[INFO] ------------------< com.example.demo:demo-tools >------------------",
    "[INFO] Building demo-tools 0.1.0-SNAPSHOT",
    "[INFO] --- surefire:3.6.1:test (default-test) @ demo-tools ---",
    "[INFO] Using auto detected provider org.apache.maven.surefire.junitplatform.JUnitPlatformProvider",
    "[INFO] -------------------------------------------------------",
    "[INFO]  T E S T S",
    "[INFO] -------------------------------------------------------",
]
BIND_FAILED = "ERROR: transport error 202: bind failed: Address already in use"

SCRIPT = r"""
import json, sys, time
p = json.loads(sys.argv[1])
for t in p["out"]:
    print(t, flush=True)
for t in p["err"]:
    print(t, file=sys.stderr, flush=True)
time.sleep(p["sleep_s"])
for t in p["late"]:
    print(t, flush=True)
sys.exit(p["exit_code"])
"""


class ScriptParams(BackendParams):
    out: list[str] = []
    err: list[str] = []
    late: list[str] = []  # salida tras el silencio (al morir el proceso o al reanudarse)
    sleep_s: float = 0
    exit_code: int = 0
    stall_timeout_s: float | None = None
    job_timeout_s: float | None = None


class ScriptBackend(Backend[ScriptParams]):
    name = "script"
    description = "imprime líneas, calla y sale"
    Params = ScriptParams

    def plan(self, spec: ScriptParams) -> Plan:
        payload = json.dumps(spec.model_dump())
        return Plan(summary="script de pruebas", steps=[CommandStep("run", [sys.executable, "-c", SCRIPT, payload])],
                    timeout_s=spec.job_timeout_s, stall_timeout_s=spec.stall_timeout_s)


@pytest.fixture
async def manager(tmp_path: Path):
    cfg = Config(state_dir=tmp_path / "state", allowed_backends=["script"], allowed_roots=[tmp_path],
                 max_parallel=2, max_wait_s=60)
    m = JobManager(cfg, {"script": ScriptBackend})
    yield m
    await m.shutdown(timeout_s=5)


def _ln(seq: int, text: str, stream: str = "stdout") -> LogLine:
    return LogLine(seq, 0.0, stream, text)


# --- tabla de pistas ---------------------------------------------------------------------------
@pytest.mark.parametrize("line,expected", [
    (JDWP, "puerto 5005"),
    ("Listening for transport dt_socket at address: 8000", "depurador"),
    ("Password:", "contraseña"),
    ("[sudo] password for usuario: ", "contraseña"),
    ("Enter passphrase for key '/home/x/.ssh/id_ed25519': ", "frase de paso"),
    ("Are you sure you want to continue connecting (yes/no/[fingerprint])? ", "huella"),
    ("Press any key to continue . . .", "tecla"),
    ("Proceed with installation? (y/n) ", "sí/no"),
    ("Do you want to continue? [Y/n]", "sí/no"),
    ("Overwrite existing file? [y/N] ", "sí/no"),
])
def test_wait_hint_recognises_prompts(line: str, expected: str):
    assert expected in (wait_hint(["[INFO] algo anterior", line]) or "")


def test_wait_hint_mentions_debug_options():
    hint = wait_hint([JDWP]) or ""
    assert "maven.surefire.debug" in hint and "suspend=y" in hint


def test_wait_hint_only_looks_at_the_last_lines():
    """La línea del depurador, 6 líneas antes del silencio, ya no cuenta."""
    lines = [JDWP] + [f"[INFO] línea {i}" for i in range(6)]
    assert wait_hint(lines) is None
    assert wait_hint(lines[:5]) is not None  # con 4 líneas posteriores sí


def test_wait_hint_no_false_positives_in_a_long_normal_log():
    normal = [
        "Collecting password-strength==0.0.3",
        "  Downloading password_strength-0.0.3-py3-none-any.whl (7.4 kB)",
        "[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0",
        "[INFO] Listening on port 8080",  # no es JDWP
        "The password: field is optional, see the docs",  # «password:» en medio de la línea
        "Use --yes to answer y/n questions automatically",
        "Press the button to continue",
        "Checking whether (y/n) prompts are enabled in the config file ... done",
        "Successfully installed password-strength-0.0.3",
        "Installing collected packages: pypdf, requests",
        "[INFO] BUILD SUCCESS",
    ]
    log = [normal[i % len(normal)] + (f" #{i}" if i % 3 else "") for i in range(3000)]
    for end in range(5, len(log), 7):  # en cualquier punto del log, nunca una pista
        assert wait_hint(log[end - 5:end]) is None, log[end - 5:end]


# --- before_silence y probable_cause (puros) ------------------------------------------------------
def test_before_silence_excludes_core_and_lines_after_the_stall():
    lines = [_ln(i, f"salida {i}") for i in range(1, 9)]
    lines.insert(3, _ln(99, "$ cmd", "core"))
    lines.append(_ln(20, "atasco: sin salida desde hace 10s", "core"))
    lines += [_ln(21, "después del atasco"), _ln(22, "más tarde", "stderr")]
    assert [ln.text for ln in before_silence(lines, stall_seq=20)] == [f"salida {i}" for i in range(4, 9)]
    assert [ln.text for ln in before_silence(lines, stall_seq=None, n=2)] == ["después del atasco", "más tarde"]
    assert before_silence([], 5) == []


def test_probable_cause_picks_first_relevant_stderr_line():
    lines = [_ln(1, "[ERROR] Failed to execute goal x"), _ln(2, "Traceback (most recent call last):", "stderr"),
             _ln(3, BIND_FAILED, "stderr"), _ln(4, "ERROR: JDWP Transport dt_socket failed to initialize", "stderr")]
    assert probable_cause(lines) == BIND_FAILED
    assert probable_cause(lines, error=f"el paso falló: {BIND_FAILED}") is None  # ya está en el error
    assert probable_cause([_ln(1, "[ERROR] solo stdout")]) is None
    assert probable_cause([_ln(1, "ERROR: " + "x" * 500, "stderr")]).endswith("…")
    assert len(probable_cause([_ln(1, "ERROR: " + "x" * 500, "stderr")])) == 200


# --- con jobs de verdad ----------------------------------------------------------------------------
async def _wait_event(events: list[Any], kind: str, timeout: float = 5.0) -> Any:
    t0 = time.monotonic()
    while not any(e.kind == kind for e in events):
        assert time.monotonic() - t0 < timeout, f"no llegó {kind}"
        await asyncio.sleep(0.05)
    return next(e for e in events if e.kind == kind)


async def test_timeout_waiting_for_debugger_case_c(manager: JobManager):
    """C: el job vence esperando al depurador; la clave es la última línea antes del silencio."""
    job = await manager.submit("script", {"out": [*MAVEN_HEAD, JDWP], "sleep_s": 30, "stall_timeout_s": 0.4,
                                          "job_timeout_s": 1.5, "late": ["[INFO] Results:"]})
    assert await manager.wait(job.id, 20)
    assert job.state == JobState.TIMED_OUT
    r = job.report
    assert len(r["before_silence"]) == 5 and r["before_silence"][-1].endswith(JDWP)
    assert any(JDWP in ln for ln in r["relevant_lines"])  # antes salía vacío
    assert "depurador" in r["hint"] and "5005" in r["hint"]
    assert r["stalls"] == 1


async def test_cancelled_waiting_for_debugger_case_d_hint_in_stalled_event(manager: JobManager):
    """D: la pista sale ya en el aviso ``stalled`` (evento y mensaje) y luego en el informe."""
    events: list[Any] = []

    async def listener(ev):
        events.append(ev)

    manager.listeners.append(listener)
    job = await manager.submit("script", {"out": [*MAVEN_HEAD, JDWP], "sleep_s": 30, "stall_timeout_s": 0.4})
    ev = await _wait_event(events, "stalled")
    assert "depurador" in ev.data["hint"] and "5005" in ev.data["hint"]
    assert "puerto 5005" in ev.message and "ATASCADO" in ev.message
    await manager.cancel(job.id)
    assert await manager.wait(job.id, 20)
    assert job.state == JobState.CANCELLED
    r = job.report
    assert r["before_silence"][-1].endswith(JDWP)
    assert any(JDWP in ln for ln in r["relevant_lines"])
    assert "depurador" in r["hint"]


async def test_stalled_without_prompt_has_no_hint(manager: JobManager):
    events: list[Any] = []

    async def listener(ev):
        events.append(ev)

    manager.listeners.append(listener)
    job = await manager.submit("script", {"out": MAVEN_HEAD, "sleep_s": 1.2, "stall_timeout_s": 0.4})
    ev = await _wait_event(events, "stalled")
    assert "hint" not in ev.data and ev.message.endswith(f"(pid {job.pid})")
    assert await manager.wait(job.id, 20)
    assert job.state == JobState.SUCCEEDED
    r = job.report
    assert r["stalls"] == 1 and len(r["before_silence"]) == 5 and "hint" not in r  # ok: sin pista


async def test_before_silence_uses_the_last_stall(manager: JobManager):
    """Con varios atascos cuenta el último: lo que se imprimió tras reanudarse, no antes."""
    job = await manager.submit("script", {"out": ["primera"], "sleep_s": 1.2, "stall_timeout_s": 0.4,
                                          "late": ["segunda"], "exit_code": 1})
    assert await manager.wait(job.id, 20)
    r = job.report
    assert r["stalls"] == 1
    assert [ln.split("| ")[-1] for ln in r["before_silence"]] == ["primera"]


async def test_failure_headline_has_probable_cause_case_a(manager: JobManager):
    """A: Maven con el puerto ocupado; el titular dice la causa, no solo «Failed to execute goal»."""
    job = await manager.submit("script", {
        "out": [*MAVEN_HEAD, "[INFO] BUILD FAILURE",
                "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.6.1:test "
                "(default-test) on project demo-tools:"],
        "err": [BIND_FAILED, "ERROR: JDWP Transport dt_socket failed to initialize, TRANSPORT_INIT(510)"],
        "exit_code": 1})
    assert await manager.wait(job.id, 20)
    assert job.state == JobState.FAILED
    r = job.report
    assert f"causa probable: {BIND_FAILED}" in r["headline"]
    assert r["headline"].count("Address already in use") == 1
    # sin silencio ni cancelación: nada de before_silence ni pistas
    assert "before_silence" not in r and "hint" not in r and r["stalls"] == 0
    assert r["relevant_lines"]


async def test_success_report_unchanged_case_e(manager: JobManager):
    """E: un job correcto sin silencio no gana ningún campo nuevo ni cambia el titular."""
    job = await manager.submit("script", {"out": ["Collecting pypdf", "Successfully installed pypdf-7.1.1"],
                                          "stall_timeout_s": 5})
    assert await manager.wait(job.id, 20)
    r = job.report
    assert r["ok"] and r["headline"].startswith(f"[{job.id}] OK en ") and "causa probable" not in r["headline"]
    assert "before_silence" not in r and "hint" not in r and "relevant_lines" not in r
    assert len(r["log_tail"]) == 5


async def test_cancel_without_stall_uses_the_end_of_the_log(manager: JobManager):
    job = await manager.submit("script", {"out": ["uno", "dos", "tres"], "sleep_s": 30})
    t0 = time.monotonic()
    while len([ln for ln in job.tail(50) if ln.stream == "stdout"]) < 3:
        assert time.monotonic() - t0 < 5
        await asyncio.sleep(0.05)
    await manager.cancel(job.id)
    assert await manager.wait(job.id, 20)
    r = job.report
    assert job.state == JobState.CANCELLED and r["stalls"] == 0
    assert [ln.split("| ")[-1] for ln in r["before_silence"]] == ["uno", "dos", "tres"]
