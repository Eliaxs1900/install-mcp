"""Backend pip: validación, plan y una instalación real offline (wheel local)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from install_mcp.backends.pip import PipBackend
from install_mcp.jobs import JobError, JobManager, JobState

from .conftest import build_wheel


@pytest.fixture
def pip(config) -> PipBackend:
    return PipBackend(config)


@pytest.mark.parametrize(
    "req",
    ["pypdf", "requests>=2.32", "rich[jupyter]==13.9.4", "a.b-c_d ~= 1.0, != 1.3", 'tomli; python_version < "3.11"'],
)
def test_valid_requirements(pip: PipBackend, req: str):
    pip.validate({"packages": [req]})


@pytest.mark.parametrize(
    "req",
    [
        "--index-url=https://evil.example",
        "-e .",
        "pkg @ https://example.org/pkg.whl",
        "git+https://github.com/x/y",
        "pkg; rm -rf /",
        "",
    ],
)
def test_rejected_requirements(pip: PipBackend, req: str):
    with pytest.raises(ValueError):
        pip.validate({"packages": [req]})


def test_rejects_insecure_and_outside_paths(pip: PipBackend, tmp_path: Path):
    with pytest.raises(ValueError, match="https"):
        pip.validate({"packages": ["x"], "index_url": "http://pypi.example/simple"})
    with pytest.raises(ValueError, match="raíces permitidas"):
        pip.validate({"packages": ["x"], "venv": "/etc/evil-venv"})
    with pytest.raises(ValueError, match="packages"):
        pip.validate({})
    with pytest.raises(ValueError):
        pip.validate({"packages": ["x"], "unknown": True})
    pip.validate({"packages": ["x"], "venv": str(tmp_path / "v")})


def test_plan_uses_argv_and_separator(pip: PipBackend, config):
    spec = pip.validate(
        {
            "packages": ["pypdf>=5"],
            "venv": "tools",
            "installer": "pip",
            "index_url": "https://user:hunter2pass@pypi.example/simple",
            "upgrade": True,
        }
    )
    plan = pip.plan(spec)
    create, install = plan.steps
    assert create.argv[1:3] == ["-m", "venv"] and create.argv[-1].endswith("venvs/tools")
    assert install.argv[-2:] == ["--", "pypdf>=5"]
    assert "--upgrade" in install.argv and "--progress-bar" in install.argv
    assert plan.lock_key == f"venv:{config.venvs_dir.resolve() / 'tools'}"
    assert "hunter2pass" in plan.secrets and "user" in plan.secrets


async def test_index_credentials_never_logged(config, registry):
    m = JobManager(config, registry)
    with pytest.raises(JobError):
        await m.submit("pip", {"packages": ["x"], "index_url": "http://user:pw@insecure/simple"})
    job = await m.submit(
        "pip",
        {"packages": ["nonexistent-pkg-xyz"], "index_url": "https://user:hunter2pass@127.0.0.1:9/simple",
         "installer": "pip", "job_timeout_s": 60},
    )
    await m.wait(job.id, 120)
    text = job.log_path.read_text()
    assert "hunter2pass" not in text and "hunter2pass" not in str(job.status()) and "***" in text
    await m.shutdown()


INSTALLERS = ["pip"] + (["uv"] if shutil.which("uv") else [])


@pytest.mark.integration
@pytest.mark.parametrize("installer", INSTALLERS)
async def test_real_offline_install(config, registry, tmp_path: Path, installer: str):
    wheels = tmp_path / "wheels"
    build_wheel(wheels)
    m = JobManager(config, registry)
    params = {
        "packages": ["demo-pkg==1.0.0"],
        "venv": f"t-{installer}",
        "installer": installer,
        "no_index": True,
        "find_links": [str(wheels)],
    }
    job = await m.submit("pip", params)
    assert await m.wait(job.id, 180), job.tail(30)
    assert job.state == JobState.SUCCEEDED, [ln.format() for ln in job.tail(40)]
    assert job.installed["demo-pkg"] == "1.0.0"
    assert job.report["info"]["installer"] == installer
    venv_py = Path(job.report["info"]["python"])
    assert venv_py.exists()

    # Segunda instalación en el mismo venv: el paso create-venv se omite.
    again = await m.submit("pip", params)
    assert await m.wait(again.id, 120)
    assert again.state == JobState.SUCCEEDED
    assert again.steps[0].state == "skipped"
    await m.shutdown()


@pytest.mark.integration
@pytest.mark.parametrize("installer", INSTALLERS)
async def test_real_offline_failure_report(config, registry, tmp_path: Path, installer: str):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    m = JobManager(config, registry)
    job = await m.submit(
        "pip",
        {"packages": ["definitely-missing-pkg"], "venv": f"f-{installer}", "installer": installer,
         "no_index": True, "find_links": [str(wheels)]},
    )
    assert await m.wait(job.id, 180)
    assert job.state == JobState.FAILED
    r = job.report
    assert r["exit_code"] not in (0, None) and r["steps"][0]["state"] in ("ok", "skipped")
    assert r["steps"][1]["state"] == "failed"
    assert r["relevant_lines"], r["log_tail"]
    assert any("definitely" in ln.lower() for ln in r["relevant_lines"] + r["log_tail"])
    await m.shutdown()
