from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from install_mcp.backends import BUILTIN, Backend, BackendParams, CommandStep, Plan
from install_mcp.backends.base import Probe
from install_mcp.config import Config
from install_mcp.jobs import JobManager

# Script que simula un instalador: imprime líneas (sin flush: el núcleo debe forzar
# salida sin buffering), mezcla stdout/stderr, puede lanzar un nieto y fallar.
FAKE_SCRIPT = r"""
import json, os, subprocess, sys, time
p = json.loads(sys.argv[1])
if p.get("child_pidfile"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(p["child_pidfile"], "w").write(str(child.pid))
if p.get("secret"):
    print(f"Authenticating with token={p['secret']}")
    print(f"GET https://user:{p['secret']}@example.org/simple/")
for i in range(p["lines"]):
    stream = sys.stderr if i % 3 == 2 and not p["stdout_only"] else sys.stdout
    print(f"line {i}", file=stream)
    time.sleep(p["interval"])
    if i == 0 and p["pause_s"]:
        time.sleep(p["pause_s"])  # silencio: simula un instalador atascado
if p.get("progress_bar"):
    sys.stdout.write("10%\r50%\r100%\n")
if p["exit_code"]:
    print("ERROR: something broke badly", file=sys.stderr)
sys.exit(p["exit_code"])
"""


class FakeParams(BackendParams):
    lines: int = 5
    interval: float = 0.05
    exit_code: int = 0
    secret: str | None = None
    child_pidfile: str | None = None
    progress_bar: bool = False
    privileged: bool = False
    lock: str | None = None
    timeout_s: float | None = None  # por paso
    job_timeout_s: float | None = None
    stall_timeout_s: float | None = None
    pause_s: float = 0
    io_mode: str | None = None
    buffered: bool = False  # desactiva PYTHONUNBUFFERED: simula un programa C con buffer
    stdout_only: bool = False
    steps: int = Field(1, ge=1, le=5)


class FakeBackend(Backend[FakeParams]):
    name = "fake"
    description = "Backend de pruebas"
    Params = FakeParams

    def requires_privileges(self, spec: FakeParams) -> bool:
        return spec.privileged

    def plan(self, spec: FakeParams) -> Plan:
        payload = json.dumps(spec.model_dump())
        env = {"PYTHONUNBUFFERED": ""} if spec.buffered else {}
        steps = [
            CommandStep(
                f"step-{i + 1}",
                [sys.executable, "-c", FAKE_SCRIPT, payload],
                timeout_s=spec.timeout_s,
                env=env,
                io_mode=spec.io_mode,  # type: ignore[arg-type]
            )
            for i in range(spec.steps)
        ]
        return Plan(
            summary=f"fake x{spec.lines}",
            steps=steps,
            lock_key=spec.lock,
            secrets=[spec.secret] if spec.secret else [],
            timeout_s=spec.job_timeout_s,
            stall_timeout_s=spec.stall_timeout_s,
        )

    async def detect_versions(self, spec: FakeParams, probe: Probe) -> dict[str, str]:
        res = await probe([sys.executable, "-c", "print('1.2.3')"])
        return {"fake-tool": res.stdout.strip()}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        state_dir=tmp_path / "state",
        allowed_backends=["pip", "fake"],
        allowed_roots=[tmp_path],
        max_parallel=4,
        max_wait_s=60,
    )


@pytest.fixture
def registry() -> dict[str, type[Backend[Any]]]:
    return {**BUILTIN, "fake": FakeBackend}


@pytest.fixture
async def manager(config: Config, registry):
    m = JobManager(config, registry)
    yield m
    await m.shutdown(timeout_s=5)


def build_wheel(directory: Path, name: str = "demo_pkg", version: str = "1.0.0") -> Path:
    """Crea a mano un wheel mínimo y válido (sin red ni setuptools)."""
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f"__version__ = '{version}'\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name.replace('_', '-')}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(content.encode())}")
    record.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return wheel
