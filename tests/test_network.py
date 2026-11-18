"""Instalaciones reales contra internet. Solo con INSTALL_MCP_NETWORK_TESTS=1 (pensado para Docker):

    docker build --target integration -t install-mcp:integration .
    docker run --rm install-mcp:integration
"""

from __future__ import annotations

import os
import shutil

import pytest

from install_mcp.backends import BUILTIN
from install_mcp.config import Config
from install_mcp.jobs import JobManager, JobState

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("INSTALL_MCP_NETWORK_TESTS") != "1", reason="INSTALL_MCP_NETWORK_TESTS != 1"),
]


@pytest.fixture
async def net(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", allowed_backends=list(BUILTIN), allowed_roots=[tmp_path],
                 allow_privileged=True)
    m = JobManager(cfg, BUILTIN)
    yield m
    await m.shutdown()


async def run(m: JobManager, backend: str, params: dict, timeout: float = 600):
    job = await m.submit(backend, params)
    assert await m.wait(job.id, timeout)
    assert job.state == JobState.SUCCEEDED, (job.error, [ln.format() for ln in job.tail(25)])
    return job


async def test_pip_from_pypi(net):
    job = await run(net, "pip", {"packages": ["six==1.16.0"], "venv": "net"})
    assert job.installed["six"] == "1.16.0"


@pytest.mark.skipif(not shutil.which("git"), reason="sin git")
async def test_git_https_clone(net, tmp_path):
    job = await run(net, "git", {"url": "https://github.com/octocat/Hello-World.git",
                                 "dest": str(tmp_path / "hello"), "depth": 1})
    assert (tmp_path / "hello" / "README").exists() and job.installed["hello"]


async def test_download_real_file(net, tmp_path):
    job = await run(net, "download", {"url": "https://raw.githubusercontent.com/octocat/Hello-World/master/README",
                                      "dest": str(tmp_path / "README")})
    assert (tmp_path / "README").read_text().startswith("Hello World")
    assert "README" in job.installed


@pytest.mark.skipif(not (shutil.which("apt-get") and hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="necesita apt-get y root (contenedor)")
async def test_apt_install(net):
    job = await run(net, "apt", {"packages": ["jq"]})
    assert job.installed["jq"] != "NO ENCONTRADO"
    assert all("sudo" not in (s.command or [""])[0] for s in job.steps)  # como root no hace falta sudo


@pytest.mark.skipif(not shutil.which("npm"), reason="sin npm")
async def test_npm_into_prefix(net):
    job = await run(net, "npm", {"packages": ["is-number@7.0.0"], "prefix": "net"})
    assert job.installed == {"is-number": "7.0.0"}


@pytest.mark.skipif(not shutil.which("go"), reason="sin go")
async def test_go_install(net):
    job = await run(net, "go", {"packages": ["golang.org/x/example/hello@latest"]})
    assert job.installed["golang.org/x/example/hello"] != "NO ENCONTRADO"
