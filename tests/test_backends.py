"""Validación y planes de todos los backends (sin ejecutar nada ni usar la red)."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.backends import BUILTIN
from watcher_mcp.backends.base import CommandStep
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobError, JobManager


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        state_dir=tmp_path / "state",
        allowed_backends=list(BUILTIN),
        allowed_roots=[tmp_path],
        backends={"run": {"programs": ["python3"]}, "maven": {"mvn_path": "/bin/echo"}},
    )


def backend(cfg: Config, name: str):
    return BUILTIN[name](cfg)


def argv(plan, i: int = -1) -> list[str]:
    step = plan.steps[i]
    assert isinstance(step, CommandStep)
    return step.argv


# ------------------------------------------------------------------ git
def test_git_sources(cfg, tmp_path):
    git = backend(cfg, "git")
    for url in ["https://github.com/o/r.git", "ssh://git@host/o/r.git", "git@github.com:o/r.git", str(tmp_path)]:
        git.validate({"url": url, "dest": str(tmp_path / "clone")})
    for url in ["ext::sh -c touch% /tmp/x", "-uhttps://x", "http://insecure/r.git", "file:///etc", "/no/such/dir"]:
        with pytest.raises(ValueError):
            git.validate({"url": url, "dest": str(tmp_path / "clone")})
    with pytest.raises(ValueError, match="raíces"):
        git.validate({"url": "https://h/r.git", "dest": "/etc/x"})
    with pytest.raises(ValueError, match="ref"):
        git.validate({"url": "https://h/r.git", "dest": str(tmp_path / "c"), "ref": "--upload-pack=x"})


def test_git_plan_never_prompts(cfg, tmp_path):
    git = backend(cfg, "git")
    plan = git.plan(git.validate({"url": "https://u:tok3n-secret@h/r.git", "dest": str(tmp_path / "c"),
                                  "depth": 1, "ref": "v1.2"}))
    clone = argv(plan, 0)
    assert clone[-3:] == ["--", "https://u:tok3n-secret@h/r.git", str(tmp_path / "c")]
    assert ["--branch", "v1.2"] == clone[clone.index("--branch"):clone.index("--branch") + 2]
    env = plan.steps[0].env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "tok3n-secret" in plan.secrets and "tok3n-secret" not in plan.summary


# ------------------------------------------------------------------ npm / cargo / go
def test_npm(cfg, tmp_path):
    npm = backend(cfg, "npm")
    plan = npm.plan(npm.validate({"packages": ["typescript", "@types/node@^20", "left-pad@1.3.0"]}))
    a = argv(plan)
    assert a[:2] == ["npm", "install"] and a[-4:] == ["--", "typescript", "@types/node@^20", "left-pad@1.3.0"]
    assert str(cfg.state_dir / "npm" / "default") in a
    for bad in ["--registry=http://evil", "../x", "git+https://x", "UPPER"]:
        with pytest.raises(ValueError):
            npm.validate({"packages": [bad]})
    proj = tmp_path / "web"
    proj.mkdir()
    (proj / "package.json").write_text("{}")
    (proj / "package-lock.json").write_text("{}")
    assert argv(npm.plan(npm.validate({"project_dir": str(proj)})))[:2] == ["npm", "ci"]
    (proj / "package-lock.json").unlink()
    (proj / "pnpm-lock.yaml").write_text("")
    assert argv(npm.plan(npm.validate({"project_dir": str(proj), "ignore_scripts": True})))[:2] == ["pnpm", "install"]
    with pytest.raises(ValueError):
        npm.validate({"packages": ["x"], "project_dir": str(proj)})


def test_cargo_and_go(cfg):
    cargo = backend(cfg, "cargo")
    plan = cargo.plan(cargo.validate({"crates": ["ripgrep", "fd-find@10.2.0"]}))
    assert len(plan.steps) == 2 and argv(plan, 1)[-2:] == ["--", "fd-find@10.2.0"] and "--locked" in argv(plan, 0)
    with pytest.raises(ValueError):
        cargo.validate({"crates": ["--git=https://evil"]})
    go = backend(cfg, "go")
    plan = go.plan(go.validate({"packages": ["golang.org/x/tools/gopls@latest"]}))
    assert argv(plan)[-2:] == ["--", "golang.org/x/tools/gopls@latest"]
    assert plan.steps[0].env["GOBIN"].endswith("go/default/bin")
    for bad in ["golang.org/x/tools/gopls", "-toolexec=sh", "../../x@latest"]:
        with pytest.raises(ValueError):
            go.validate({"packages": [bad]})


# ------------------------------------------------------------------ maven / gradle
def test_maven(cfg, tmp_path):
    mvn = backend(cfg, "maven")
    plan = mvn.plan(mvn.validate({"artifacts": ["org.slf4j:slf4j-api:2.0.16"]}))
    assert argv(plan)[-2:] == ["dependency:get", "-Dartifact=org.slf4j:slf4j-api:2.0.16"] and "-B" in argv(plan)
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pom.xml").write_text("<project/>")
    (proj / "mvnw").write_text("#!/bin/sh\n")
    plan = mvn.plan(mvn.validate({"project_dir": str(proj), "goals": ["package"], "skip_tests": True,
                                  "profiles": ["ci"], "properties": ["java.version=21"]}))
    a = argv(plan)
    assert a[0] == str(proj / "mvnw") and "-DskipTests" in a and a[-1] == "package" and "-Djava.version=21" in a
    for bad in [{"goals": ["exec:exec"]}, {"properties": ["x=$(rm -rf /)"]}, {"profiles": ["-X"]}]:
        with pytest.raises(ValueError):
            mvn.validate({"project_dir": str(proj), **bad})


def test_gradle(cfg, tmp_path):
    gradle = backend(cfg, "gradle")
    proj = tmp_path / "g"
    proj.mkdir()
    (proj / "settings.gradle.kts").write_text("")
    (proj / "gradlew").write_text("#!/bin/sh\n")
    a = argv(gradle.plan(gradle.validate({"project_dir": str(proj), "tasks": [":app:build"], "exclude_test": True})))
    assert a[0] == str(proj / "gradlew") and "--console=plain" in a and a[-1] == ":app:build" and "-x" in a
    with pytest.raises(ValueError):
        gradle.validate({"project_dir": str(proj), "tasks": ["publish"]})


# ------------------------------------------------------------------ brew / apt / docker / run
def test_brew_apt_docker(cfg):
    brew = backend(cfg, "brew")
    plan = brew.plan(brew.validate({"formulae": ["binwalk", "p7zip", "homebrew/core/jq"], "casks": ["wireshark"]}))
    assert argv(plan, 0)[:3] == ["brew", "install", "--formula"] and argv(plan, 1)[2] == "--cask"
    assert plan.steps[0].env["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    with pytest.raises(ValueError):
        brew.validate({"formulae": ["--HEAD"]})

    apt = backend(cfg, "apt")
    spec = apt.validate({"packages": ["binwalk", "squashfs-tools", "p7zip-full=16.02+dfsg-8"]})
    plan = apt.plan(spec)
    assert apt.requires_privileges(spec) and all(s.privileged for s in plan.steps)
    assert plan.steps[0].env["DEBIAN_FRONTEND"] == "noninteractive" and plan.lock_key == "apt"
    with pytest.raises(ValueError):
        apt.validate({"packages": ["-o APT::Update::Pre-Invoke::=sh"]})

    docker = backend(cfg, "docker")
    plan = docker.plan(docker.validate({"images": ["python:3.12-slim", "ghcr.io/astral-sh/uv:0.13.8",
                                                   "localhost:5000/team/app@sha256:" + "a" * 64]}))
    assert argv(plan, 0) == ["docker", "pull", "--", "python:3.12-slim"]
    with pytest.raises(ValueError):
        docker.validate({"images": ["--all-tags"]})


def test_run_allowlist(cfg, tmp_path):
    run = backend(cfg, "run")
    spec = run.validate({"program": "python3", "args": ["-c", "print(1)"], "cwd": str(tmp_path),
                         "env": {"API_TOKEN": "supersecret-value"}})
    plan = run.plan(spec)
    assert argv(plan) == ["python3", "-c", "print(1)"] and "supersecret-value" in plan.secrets
    for bad in [{"program": "sh"}, {"cwd": "/etc"}, {"env": {"LD_PRELOAD": "x"}}, {"env": {"PATH": "/tmp"}}]:
        with pytest.raises(ValueError):
            run.validate({"program": "python3", "cwd": str(tmp_path), **bad})


# ------------------------------------------------------------------ política del gestor
async def test_manager_policy(cfg, tmp_path):
    m = JobManager(cfg, BUILTIN)
    if "apt-get" in str(m.backend("apt").availability()) or not m.backend("apt").availability()[0]:
        with pytest.raises(JobError, match="no disponible|privilegios"):
            await m.submit("apt", {"packages": ["jq"]})
    else:
        with pytest.raises(JobError, match="privilegios"):
            await m.submit("apt", {"packages": ["jq"]})
    default = Config(state_dir=tmp_path / "s2", allowed_roots=[tmp_path])
    assert "apt" not in default.allowed_backends and "run" not in default.allowed_backends
    assert {"pip", "git", "download", "binary", "npm", "brew", "docker"} <= set(default.allowed_backends)
    m2 = JobManager(Config(state_dir=tmp_path / "s3", allowed_roots=[tmp_path], allowed_backends=["run"]), BUILTIN)
    with pytest.raises(JobError, match="programs"):
        await m2.submit("run", {"program": "python3", "cwd": str(tmp_path)})
