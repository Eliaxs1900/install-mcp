"""Modo CLI (#2): ``run``, ``status``, ``logs``, ``list`` y el alias ``install-mcp`` (#8)."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from watcher_mcp import __main__ as entry
from watcher_mcp.cli import EXIT_CANCELLED, EXIT_NOT_FOUND, EXIT_TIMEOUT, dispatch, parse_args

PY = sys.executable
GRANDCHILD = (
    "import subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(c.pid))\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def cli(capsys, state: Path, *argv: str, config: Path | None = None) -> tuple[int, str, str]:
    """Ejecuta la CLI en proceso y devuelve (código, stdout, stderr)."""
    extra = ["--config", str(config)] if config else []
    code = dispatch(parse_args([*argv[:1], "--state-dir", str(state), *extra, *argv[1:]]))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def job_ids(state: Path) -> list[str]:
    return sorted(p.name for p in (state / "jobs").iterdir())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- run
def test_run_success_streams_output_and_reports(capsys, state):
    code, out, err = cli(capsys, state, "run", "--", PY, "-c", "print('hola'); import sys; print('aviso', file=sys.stderr)")
    assert code == 0
    assert out.strip() == "hola"  # stdout del comando a stdout: se puede encadenar con pipes
    assert "aviso" in err and " OK en " in err
    (job_id,) = job_ids(state)
    assert job_id in err and str(state / "jobs" / job_id / "output.log") in err


def test_run_failure_returns_the_command_exit_code_and_relevant_lines(capsys, state):
    code, out, err = cli(capsys, state, "run", "--", PY, "-c",
                         "import sys; print('compilando'); print('ERROR: algo se rompió', file=sys.stderr); sys.exit(3)")
    assert code == 3
    assert "FALLÓ (código 3)" in err
    assert "ERROR: algo se rompió" in err  # líneas relevantes en el informe


def test_run_signal_death_maps_to_128_plus_n(capsys, state):
    code, _, _ = cli(capsys, state, "run", "--", PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    assert code == 128 + 9


def test_run_quiet_shows_only_warnings_and_report(capsys, state):
    code, out, err = cli(capsys, state, "run", "--quiet", "--", PY, "-c", "print('ru' + 'ido')")
    assert code == 0
    assert out == "" and "ruido" not in err  # la salida no se muestra (el comando sí sale en el titular)
    assert " OK en " in err


def test_run_stall_warning_with_short_idle(capsys, state):
    # Tras 'dos' el comando sigue vivo un rato (< --idle): el vigilante comprueba cada ~0,1 s y así ve
    # la reanudación siempre; si el proceso acabara justo al escribir, el job terminaría antes de notarla.
    code, out, err = cli(capsys, state, "run", "--idle", "0.4", "--", PY, "-c",
                         "import time; print('uno', flush=True); time.sleep(1.5); print('dos', flush=True); time.sleep(0.25)")
    assert code == 0  # un atasco avisa, no mata
    assert "ATASCADO" in err and "se reanuda la salida" in err
    assert "atascos detectados: 1" in err
    assert out.split() == ["uno", "dos"]


def test_run_quiet_still_prints_stall_warnings(capsys, state):
    _, out, err = cli(capsys, state, "run", "-q", "--idle", "0.4", "--", PY, "-c", "import time; time.sleep(1.2)")
    assert "ATASCADO" in err and out == ""


def test_run_timeout_exits_124_and_kills_the_process_tree(capsys, state, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    started = time.monotonic()
    code, _, err = cli(capsys, state, "run", "--timeout", "1", "-q", "--", PY, "-c", GRANDCHILD, str(pidfile))
    assert code == EXIT_TIMEOUT == 124
    assert time.monotonic() - started < 15
    assert "TIEMPO AGOTADO" in err
    assert wait_dead(int(pidfile.read_text()))  # el nieto muere con el grupo, no solo el proceso de arriba


def test_run_command_not_found_exits_127(capsys, state):
    code, _, err = cli(capsys, state, "run", "--", "no-existe-este-programa-xyz")
    assert code == EXIT_NOT_FOUND == 127
    assert "no se encuentra el ejecutable" in err


def test_run_relative_program_is_resolved_against_cwd(capsys, state, tmp_path):
    work = tmp_path / "proyecto"
    work.mkdir()
    script = work / "build.sh"
    script.write_text("#!/bin/sh\necho desde-cwd $PWD\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    code, out, _ = cli(capsys, state, "run", "--cwd", str(work), "--", "./build.sh")
    assert code == 0 and "desde-cwd" in out and "proyecto" in out


def test_run_io_mode_pty(capsys, state):
    # El comando sigue vivo un instante tras imprimir: un proceso que escribe y sale al instante puede perder
    # su salida con el pty (carrera del núcleo en process.py, ajena a la CLI; ~5 % de las veces).
    code, out, _ = cli(capsys, state, "run", "--io-mode", "pty", "--", PY, "-c",
                       "import sys, time; print(sys.stdout.isatty(), flush=True); time.sleep(0.3)")
    assert code == 0 and out.strip() == "True"


def test_run_json_report_on_stdout_and_live_output_on_stderr(capsys, state):
    code, out, err = cli(capsys, state, "run", "--json", "--", PY, "-c", "print('hola')")
    report = json.loads(out)  # stdout es solo el informe
    assert code == 0 and report["state"] == "succeeded" and report["cli_exit_code"] == 0
    assert report["backend"] == "run" and "hola" in err


def test_run_does_not_probe_the_command_with_version(capsys, state):
    cli(capsys, state, "run", "--json", "--", PY, "-c", "print(1)")
    (job_id,) = job_ids(state)
    log = (state / "jobs" / job_id / "output.log").read_text()
    assert "sonda de versión" not in log


def test_run_requires_a_command_after_double_dash():
    with pytest.raises(SystemExit) as exc:
        parse_args(["run", "--idle", "5"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        parse_args(["run", "--"])
    with pytest.raises(SystemExit):
        parse_args(["status", "abc", "--", "x"])


def test_run_command_options_are_not_parsed_as_watcher_options():
    args = parse_args(["run", "--idle", "7", "--", "mvn", "--timeout", "5", "-q"])
    assert args.cmd == ["mvn", "--timeout", "5", "-q"] and args.idle == 7 and args.timeout is None


def test_run_password_pairs_are_redacted_in_summary_params_and_job_json(capsys, state):
    cli(capsys, state, "run", "-q", "--", "true", "--password", "hunter2hunter2")  # programa corto: el resumen se corta a 80
    (job_id,) = job_ids(state)
    for name in ("job.json", "report.json", "output.log"):
        assert "hunter2hunter2" not in (state / "jobs" / job_id / name).read_text(), name
    assert "--password ***" in json.loads((state / "jobs" / job_id / "job.json").read_text())["summary"]


def test_run_secrets_in_arguments_are_redacted(capsys, state):
    _, _, err = cli(capsys, state, "run", "--", PY, "-c", "print('token=abc123def456ghi')")
    (job_id,) = job_ids(state)
    log = (state / "jobs" / job_id / "output.log").read_text()
    assert "abc123def456ghi" not in log


# --------------------------------------------------------------------------- seguridad: allowlist
RUN_CONFIG = """\
[backends.run]
commands = [
  {{ program = "{program}", args_prefix = ["-c", "print('ok')"] }},
]
"""


def test_run_ignores_the_allowlist_by_default(capsys, state, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(RUN_CONFIG.format(program="otro-programa"))
    code, out, _ = cli(capsys, state, "run", "--", PY, "-c", "print('libre')", config=config)
    assert code == 0 and out.strip() == "libre"


def test_run_strict_applies_the_run_allowlist_and_roots(capsys, state, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(PY).parent}{os.pathsep}{os.environ['PATH']}")
    program = Path(PY).name  # las reglas hablan de nombres en PATH
    config = tmp_path / "config.toml"
    config.write_text(f'allowed_roots = ["{tmp_path}"]\n' + RUN_CONFIG.format(program=program))
    strict = ("run", "--strict", "--cwd", str(tmp_path), "--")
    # No casa con ninguna regla (otros argumentos) -> rechazado antes de ejecutar nada.
    code, out, err = cli(capsys, state, *strict, program, "-c", "print('otro')", config=config)
    assert code == 2 and "no permitido" in err and "otro" not in out
    # Casa con la regla (programa + prefijo de argumentos): se ejecuta.
    code, out, _ = cli(capsys, state, *strict, program, "-c", "print('ok')", config=config)
    assert code == 0 and out.strip() == "ok"
    # Casa con la regla, pero el cwd está fuera de allowed_roots.
    code, _, err = cli(capsys, state, "run", "--strict", "--cwd", str(tmp_path.parent), "--", program, "-c", "print('ok')",
                       config=config)
    assert code == 2 and "raíces permitidas" in err


def test_run_with_invalid_config_exits_2(capsys, state, tmp_path):
    config = tmp_path / "roto.toml"
    config.write_text("esto no es [toml")
    code, _, err = cli(capsys, state, "run", "--", "true", config=config)
    assert code == 2 and "watcher-mcp doctor" in err


# --------------------------------------------------------------------------- status / logs / list
def test_status_logs_and_list_see_a_cli_job(capsys, state):
    cli(capsys, state, "run", "-q", "--", PY, "-c", "print('linea-uno'); print('linea-dos')")
    (job_id,) = job_ids(state)

    code, out, _ = cli(capsys, state, "list")
    assert code == 0 and job_id in out and "succeeded" in out

    code, out, _ = cli(capsys, state, "status", job_id)
    assert code == 0 and "succeeded" in out and "linea-dos" in out

    code, out, _ = cli(capsys, state, "status", job_id[:5], "--json")  # prefijo único
    data = json.loads(out)
    assert data["job_id"] == job_id and data["state"] == "succeeded" and data["report"]["ok"] is True

    code, out, _ = cli(capsys, state, "logs", job_id, "--tail", "2", "--stream", "stdout")
    assert code == 0 and [ln.split("| ")[1] for ln in out.splitlines()] == ["linea-uno", "linea-dos"]


def test_status_of_unknown_job_exits_1(capsys, state):
    (state / "jobs").mkdir(parents=True)
    code, _, err = cli(capsys, state, "status", "nada")
    assert code == 1 and "No existe el job" in err


def test_list_filters_by_state_and_handles_empty_state_dir(capsys, state):
    code, out, _ = cli(capsys, state, "list")
    assert code == 0 and "sin jobs" in out
    cli(capsys, state, "run", "-q", "--", PY, "-c", "pass")
    cli(capsys, state, "run", "-q", "--", PY, "-c", "import sys; sys.exit(1)")
    _, out, _ = cli(capsys, state, "list", "--state", "failed", "--json")
    rows = json.loads(out)
    assert len(rows) == 1 and rows[0]["state"] == "failed"


def _fake_running_job(state: Path, owner: str) -> Path:
    directory = state / "jobs" / "aabbccddeeff"
    directory.mkdir(parents=True)
    now = "2026-01-01T00:00:00.000Z"
    (directory / "job.json").write_text(json.dumps({
        "job_id": "aabbccddeeff", "backend": "run", "summary": "build largo", "state": "running", "owner": owner,
        "created_at": now, "started_at": now, "pid": 1, "steps": [],
    }))
    (directory / "output.log").write_text(f"{now} stdout | compilando\n")
    return directory


def test_job_running_in_another_live_instance_is_shown_read_only(capsys, state):
    (state / "instances").mkdir(parents=True)
    with open(state / "instances" / "deadbeef.lock", "w") as lock:  # otro servidor vivo: mantiene su flock
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = _fake_running_job(state, "deadbeef")
        before = (directory / "job.json").read_text()
        code, out, _ = cli(capsys, state, "status", "aabbccddeeff", "--json")
        data = json.loads(out)
        assert code == 0 and data["state"] == "running" and data["foreign"] is True
        _, out, _ = cli(capsys, state, "list")
        assert "running*" in out and "solo lectura" in out
        assert (directory / "job.json").read_text() == before  # ni se marca ni se toca


def test_job_of_a_dead_instance_shows_as_interrupted_without_touching_disk(capsys, state):
    (state / "instances").mkdir(parents=True)
    directory = _fake_running_job(state, "cafecafe")  # su lock no existe: murió
    before = (directory / "job.json").read_text()
    code, out, _ = cli(capsys, state, "status", "aabbccddeeff")
    assert code == 0 and "failed" in out and "interrumpido" in out
    assert (directory / "job.json").read_text() == before  # solo lectura: el servidor lo recoge al arrancar
    assert (directory / "output.log").read_text().count("\n") == 1


# --------------------------------------------------------------------------- arranque como servidor y alias
def test_without_subcommand_it_still_serves(monkeypatch):
    served: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(entry, "serve", lambda config, state_dir: served.append((config, state_dir)))
    entry.main([])
    entry.main(["--config", "x.toml", "--state-dir", "/tmp/y"])
    entry.main(["--state-dir=/tmp/z"])
    assert served == [(None, None), ("x.toml", "/tmp/y"), (None, "/tmp/z")]


def test_global_options_are_accepted_before_and_after_the_subcommand():
    before = parse_args(["--config", "a.toml", "--state-dir", "/s", "list"])
    after = parse_args(["list", "--config", "a.toml", "--state-dir", "/s"])
    assert (before.config, before.state_dir) == (after.config, after.state_dir) == ("a.toml", "/s")
    only_before = parse_args(["--state-dir", "/s", "status", "abc"])
    assert only_before.state_dir == "/s"  # el subcomando no lo pisa


def test_legacy_main_warns_on_stderr_and_then_serves(monkeypatch, capsys):
    called: list[list[str]] = []
    monkeypatch.setattr(entry, "main", lambda argv=None: called.append(list(argv)))
    monkeypatch.setattr(sys, "argv", ["/venv/bin/install-mcp", "--config", "/etc/w.toml"])
    entry.legacy_main()
    err = capsys.readouterr()
    assert err.out == ""  # stdout es el canal MCP: el aviso va solo por stderr
    assert "install-mcp" in err.err and "obsoleto" in err.err
    assert "claude mcp remove install-mcp" in err.err
    assert "claude mcp add --scope user watcher-mcp --" in err.err
    assert "/etc/w.toml" in err.err  # conserva los argumentos con los que estaba registrado
    assert "reinícialas" in err.err
    assert called == [["--config", "/etc/w.toml"]]  # y sigue sirviendo con normalidad


def test_install_mcp_entry_point_is_declared():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert scripts["watcher-mcp"] == "watcher_mcp.__main__:main"
    assert scripts["install-mcp"] == "watcher_mcp.__main__:legacy_main"
    assert callable(entry.legacy_main)
