"""#105 de punta a punta con pip real: el rodeo del comentario con barra y el cambio del fichero en la cola."""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher_mcp.jobs import JobManager, JobState

from .conftest import build_wheel

# E2E: pip real (instalación offline desde un wheel local). Las pruebas puntuales se ejecutan con -m 'not e2e'.
pytestmark = pytest.mark.e2e

SECRET = "hunter2hunter2"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / ".netrc").write_text(f"machine api.example.com login admin password {SECRET}\n")
    build_wheel(tmp_path / "wheels", "demo_pkg", "1.0.0")
    return proj


def params(project: Path, venv: str) -> dict:
    return {"requirements": str(project / "requirements.txt"), "venv": venv, "installer": "pip", "no_index": True,
            "find_links": [str(project.parent / "wheels")]}


def everything_visible(job) -> str:
    parts = [job.log_path.read_text(), (job.dir / "job.json").read_text(), str(job.report), str(job.error)]
    return "\n".join(parts)


async def test_a_valid_requirements_file_installs_through_the_private_copy(config, registry, project):
    (project / "requirements.txt").write_text("# dependencias\ndemo_pkg==1.0.0  # la única\n")
    manager = JobManager(config, registry)
    try:
        job = await manager.submit("pip", params(project, "ok"))
        await manager.wait(job.id, 120)
        assert job.state == JobState.SUCCEEDED, job.error
        assert job.installed.get("demo-pkg") == "1.0.0"
        log = job.log_path.read_text()
        assert "requirements validado" in log and str(config.state_dir / "requirements") in log
    finally:
        await manager.shutdown(timeout_s=5)


@pytest.mark.parametrize("body", [
    "# nota \\\n-r {netrc}\n",       # el comentario con barra: pip NO continúa y lee el -r
    "# nota \\\r-r {netrc}\n",       # lo mismo con CR suelto
    "pytest\n-r {netrc}\n",          # el caso simple de la auditoría
])
async def test_the_netrc_never_reaches_the_log_or_the_report(config, registry, project, body):
    (project / "requirements.txt").write_text(body.format(netrc=project.parent / ".netrc"))
    manager = JobManager(config, registry)
    try:
        try:
            job = await manager.submit("pip", params(project, "mal"))
        except ValueError:
            return  # rechazado ya al enviar: tampoco hay nada que filtrar
        await manager.wait(job.id, 120)
        assert job.state == JobState.FAILED
        assert SECRET not in everything_visible(job)
        assert "machine api.example.com" not in everything_visible(job)
    finally:
        await manager.shutdown(timeout_s=5)


async def test_a_file_changed_while_the_job_waits_in_the_queue_is_not_trusted(config, registry, project):
    config.max_parallel = 1
    (project / "requirements.txt").write_text("demo_pkg==1.0.0\n")
    manager = JobManager(config, registry)
    try:
        blocker = await manager.submit("fake", {"lines": 25, "interval": 0.1})  # ocupa el único hueco
        job = await manager.submit("pip", params(project, "cola"))  # válido al enviarlo; se queda en cola
        assert job.state.value == "queued"
        (project / "requirements.txt").write_text(f"demo_pkg==1.0.0\n-r {project.parent / '.netrc'}\n")
        await manager.wait(job.id, 120)
        assert job.state == JobState.FAILED, job.error
        assert SECRET not in everything_visible(job)
        await manager.wait(blocker.id, 30)
    finally:
        await manager.shutdown(timeout_s=5)
