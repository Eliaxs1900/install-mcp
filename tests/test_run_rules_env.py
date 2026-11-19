"""Issue #15: how_to_enable en backends(), reglas commands (args_prefix/cwd_under) e inherit_env/env_from."""

from __future__ import annotations

import json
import re
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from watcher_mcp.backends import BUILTIN
from watcher_mcp.config import Config
from watcher_mcp.envsource import EnvSource, capture_login_env
from watcher_mcp.jobs import JobError, JobManager, JobState

PY = Path(sys.executable).name
SECRET = "sk-test-0123456789-very-secret-value"
LOGIN_SECRET = "from-login-shell-abcdef123456"


async def finish(m: JobManager, job, timeout: float = 30):
    assert await m.wait(job.id, timeout), [ln.format() for ln in job.tail(30)]
    return job


@pytest.fixture
def fake_shell(tmp_path):
    """Shell de login falsa: imprime ruido (como un .zshrc) y define una variable solo suya."""
    sh = tmp_path / "fakeshell"
    sh.write_text(
        "#!/bin/sh\n"
        'echo "bienvenido desde el rc"\n'
        f'LOGIN_ONLY_KEY="{LOGIN_SECRET}" OTHER_LOGIN_KEY=xyz-not-listed-9999 export LOGIN_ONLY_KEY OTHER_LOGIN_KEY\n'
        f'LOGIN_ONLY_KEY="{LOGIN_SECRET}" OTHER_LOGIN_KEY=xyz-not-listed-9999 exec /bin/sh -c "$4"\n'
    )
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return sh


def make_mgr(tmp_path: Path, **kw) -> JobManager:
    work = tmp_path / "proj"
    (work / "tools").mkdir(parents=True, exist_ok=True)
    (work / "tools" / "hello.py").write_text(
        "import os, sys\nprint('ARGS', sys.argv[1:])\n"
        "for k in ('GEMINI_API_KEY', 'LOGIN_ONLY_KEY', 'OTHER_LOGIN_KEY'):\n"
        "    print(k, os.environ.get(k, '<none>'))\n"
    )
    backends = kw.pop("backends", None)
    cfg = Config(
        state_dir=tmp_path / "state",
        allowed_backends=kw.pop("allowed_backends", list(BUILTIN)),
        allowed_roots=[tmp_path],
        backends=backends if backends is not None else {
            "run": {"commands": [{"program": PY, "args_prefix": ["-u", "tools/"], "cwd_under": str(work)}]},
        },
        **kw,
    )
    return JobManager(cfg, BUILTIN)


@pytest.fixture
async def mgr(tmp_path):
    m = make_mgr(tmp_path)
    yield m
    await m.shutdown(timeout_s=5)


# ------------------------------------------------------------------ how_to_enable
async def test_how_to_enable_for_disallowed_backend(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('allowed_roots = ["~"]\n')
    m = make_mgr(tmp_path, allowed_backends=["pip", "git"], backends={})
    m.config.source_path = cfg_file
    try:
        info = {b["name"]: b for b in m.describe_backends()}
        run = info["run"]
        assert run["allowed"] is False
        how = run["how_to_enable"]
        assert how["config_file"] == str(cfg_file) and how["config_file_exists"] is True
        assert 'allowed_backends = ["pip", "git", "run"]' in how["toml"]
        assert "[backends.run]" in how["toml"] and "args_prefix" in how["toml"]
        # El fragmento es TOML válido y una configuración aceptada.
        parsed = tomllib.loads(how["toml"])
        cfg = Config.from_dict(parsed)
        assert "run" in cfg.allowed_backends and cfg.backends["run"]["commands"][0]["program"] == "uv"
        assert "allow_privileged = true" in info["apt"]["how_to_enable"]["toml"]
        assert "how_to_enable" not in info["pip"]
        with pytest.raises(JobError, match=r"config\.toml.*how_to_enable"):
            await m.submit("run", {"program": PY, "cwd": str(tmp_path)})
    finally:
        await m.shutdown(timeout_s=5)


async def test_how_to_enable_when_allowed_but_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCHER_MCP_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    m = make_mgr(tmp_path, backends={})
    try:
        run = next(b for b in m.describe_backends() if b["name"] == "run")
        assert run["allowed"] is True and run["available"] is False
        how = run["how_to_enable"]
        assert how["config_file"].endswith(".config/watcher-mcp/config.toml") and how["config_file_exists"] is False
        assert "allowed_backends" not in how["toml"] and "[backends.run]" in how["toml"]
    finally:
        await m.shutdown(timeout_s=5)


def test_config_load_records_source_path(tmp_path, monkeypatch):
    f = tmp_path / "c.toml"
    f.write_text('inherit_env = ["GEMINI_API_KEY"]\n')
    cfg = Config.load(f)
    assert cfg.config_file() == f.resolve() and cfg.inherit_env == ["GEMINI_API_KEY"]
    with pytest.raises(ValueError, match="desconocidas"):
        Config.from_dict({"source_path": "/x"})
    with pytest.raises(ValueError, match="PATH"):
        Config.from_dict({"inherit_env": ["PATH"]})


# ------------------------------------------------------------------ commands
async def test_command_rule_allows_matching_request(mgr, tmp_path):
    work = tmp_path / "proj"
    job = await finish(mgr, await mgr.submit("run", {
        "program": PY, "args": ["-u", "tools/hello.py", "judge"], "cwd": str(work)}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert "ARGS ['judge']" in job.log_path.read_text()
    assert "commands[0]" in job.summary
    # Un subdirectorio de cwd_under también vale.
    (work / "sub").mkdir()
    (work / "sub" / "tools").mkdir()
    (work / "sub" / "tools" / "x.py").write_text("print('sub ok')\n")
    job = await finish(mgr, await mgr.submit("run", {"program": PY, "args": ["-u", "tools/x.py"],
                                                     "cwd": str(work / "sub")}))
    assert job.state == JobState.SUCCEEDED and "sub ok" in job.log_path.read_text()


@pytest.mark.parametrize(
    ("args", "cwd_rel", "reason"),
    [
        (["-c", "print(1)"], "proj", r"args\[0\] = '-c', se esperaba '-u'"),
        (["-u", "other/x.py"], "proj", r"args\[1\] = 'other/x.py' no está bajo 'tools/'"),
        (["-u", "tools/../../evil.py"], "proj", r"no está bajo 'tools/'"),
        (["-u"], "proj", "faltan argumentos"),
        (["-u", "tools/hello.py"], "elsewhere", "no está bajo"),
    ],
)
async def test_command_rule_rejects(mgr, tmp_path, args, cwd_rel, reason):
    (tmp_path / "elsewhere").mkdir(exist_ok=True)
    with pytest.raises(JobError, match=r"commands\[0\]") as exc:
        await mgr.submit("run", {"program": PY, "args": args, "cwd": str(tmp_path / cwd_rel)})
    assert re.search(reason, str(exc.value)), str(exc.value)


async def test_command_rule_other_program_rejected(mgr, tmp_path):
    with pytest.raises(JobError, match=r"programa no permitido: 'sh'.*el programa es 'sh', no"):
        await mgr.submit("run", {"program": "sh", "args": ["-u", "tools/x"], "cwd": str(tmp_path / "proj")})


async def test_programs_and_commands_coexist(tmp_path):
    m = make_mgr(tmp_path, backends={"run": {"programs": [PY], "commands": [{"program": "uv", "args_prefix": ["run"]}]}})
    try:
        job = await finish(m, await m.submit("run", {"program": PY, "args": ["-c", "print('free')"],
                                                     "cwd": str(tmp_path)}))
        assert job.state == JobState.SUCCEEDED
        run = next(b for b in m.describe_backends() if b["name"] == "run")
        assert run["available"] and "commands[0] = uv run" in run["availability"]
    finally:
        await m.shutdown(timeout_s=5)


async def test_invalid_rule_is_reported(tmp_path):
    m = make_mgr(tmp_path, backends={"run": {"commands": [{"program": "uv", "cwd_under": "relativo"}]}})
    try:
        run = next(b for b in m.describe_backends() if b["name"] == "run")
        assert run["available"] is False and "cwd_under" in run["availability"]
    finally:
        await m.shutdown(timeout_s=5)


# ------------------------------------------------------------------ inherit_env / env_from
def assert_no_secret(job, *values: str) -> None:
    texts = [
        job.log_path.read_text(),
        (job.dir / "job.json").read_text(),
        json.dumps(job.status(tail=100), ensure_ascii=False),
        json.dumps(job.report or {}, ensure_ascii=False),
    ]
    if (job.dir / "report.json").exists():
        texts.append((job.dir / "report.json").read_text())
    for v in values:
        for t in texts:
            assert v not in t


async def test_inherit_env_from_server_env_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    m = make_mgr(tmp_path, inherit_env=["GEMINI_API_KEY"], login_shell_env=False)
    events: list[str] = []

    async def listener(ev) -> None:
        events.append(json.dumps({"msg": ev.message, "data": ev.data}, default=str))

    m.listeners.append(listener)
    try:
        job = await finish(m, await m.submit("run", {"program": PY, "args": ["-u", "tools/hello.py"],
                                                     "cwd": str(tmp_path / "proj")}))
        assert job.state == JobState.SUCCEEDED, job.error
        log = job.log_path.read_text()
        assert "GEMINI_API_KEY ***" in log  # el programa la recibió (y se redactó al imprimirla)
        assert "entorno heredado por nombre" in log
        assert_no_secret(job, SECRET)
        assert events and not any(SECRET in e for e in events)
    finally:
        await m.shutdown(timeout_s=5)


async def test_inherit_env_from_login_shell_only_listed_names(tmp_path, fake_shell, monkeypatch):
    monkeypatch.delenv("LOGIN_ONLY_KEY", raising=False)
    monkeypatch.delenv("OTHER_LOGIN_KEY", raising=False)
    m = make_mgr(tmp_path, login_shell=str(fake_shell), backends={"run": {
        "inherit_env": ["NOT_ANYWHERE_KEY"],
        "commands": [{"program": PY, "args_prefix": ["-u", "tools/"], "inherit_env": ["LOGIN_ONLY_KEY"]}],
    }})
    try:
        job = await finish(m, await m.submit("run", {"program": PY, "args": ["-u", "tools/hello.py"],
                                                     "cwd": str(tmp_path / "proj")}))
        assert job.state == JobState.SUCCEEDED, job.error
        log = job.log_path.read_text()
        assert "LOGIN_ONLY_KEY ***" in log
        assert "OTHER_LOGIN_KEY <none>" in log  # no listada: no se copia
        assert "inherit_env sin valor" in log and "NOT_ANYWHERE_KEY" in log
        assert_no_secret(job, LOGIN_SECRET)
        # env_from no puede tirar de la shell de login.
        with pytest.raises(JobError, match="env_from: no están en el entorno del servidor"):
            await m.submit("run", {"program": PY, "args": ["-u", "tools/hello.py"], "cwd": str(tmp_path / "proj"),
                                   "env_from": ["OTHER_LOGIN_KEY"]})
    finally:
        await m.shutdown(timeout_s=5)


async def test_env_from_param(mgr, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    job = await finish(mgr, await mgr.submit("run", {"program": PY, "args": ["-u", "tools/hello.py"],
                                                     "cwd": str(tmp_path / "proj"), "env_from": ["GEMINI_API_KEY"]}))
    assert job.state == JobState.SUCCEEDED, job.error
    assert job.params["env_from"] == ["GEMINI_API_KEY"]
    assert "GEMINI_API_KEY ***" in job.log_path.read_text()
    assert_no_secret(job, SECRET)
    with pytest.raises(JobError, match="env_from"):
        await mgr.submit("run", {"program": PY, "args": ["-u", "tools/hello.py"], "cwd": str(tmp_path / "proj"),
                                 "env_from": ["DYLD_INSERT_LIBRARIES"]})


def test_capture_login_env_parses_and_survives_bad_shell(fake_shell, tmp_path):
    env = capture_login_env(str(fake_shell))
    assert env["LOGIN_ONLY_KEY"] == LOGIN_SECRET and "bienvenido" not in "".join(env)
    assert capture_login_env(str(tmp_path / "no-such-shell")) == {}
    src = EnvSource(login_shell=str(fake_shell), server_env={"A_KEY": "1"})
    found, missing = src.resolve(["A_KEY", "LOGIN_ONLY_KEY", "NOPE_KEY"], login_fallback=True)
    assert found == {"A_KEY": "1", "LOGIN_ONLY_KEY": LOGIN_SECRET} and missing == ["NOPE_KEY"]
    found, missing = src.resolve(["LOGIN_ONLY_KEY"], login_fallback=False)
    assert found == {} and missing == ["LOGIN_ONLY_KEY"]
