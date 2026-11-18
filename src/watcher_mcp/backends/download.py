"""Backends web: ``download`` (fichero o archivo comprimido) y ``binary`` (ejecutable suelto).

La descarga se hace en Python (sin curl/wget), en un hilo, para controlar todo lo que
importa aquí:

* solo ``https://``, también tras redirecciones;
* progreso en el log cada ~2 s (bytes, %, velocidad), así el watchdog ve actividad;
* cancelación y timeout del job comprobados en cada bloque;
* tamaño máximo (``[backends.download] max_bytes``, 5 GiB por defecto);
* checksum ``sha256:…`` (opcional en ``download``, obligatorio en ``binary``);
* escritura en ``.part`` y renombrado atómico al final;
* extracción segura de tar/zip (sin rutas absolutas, ``..`` ni enlaces fuera del destino).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import ssl
import stat
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import Field

from ..security import Checksum, require_https, url_secrets
from .base import Backend, FuncStep, Plan, Probe, StepContext
from .common import LimitParams

CHUNK = 256 * 1024
# Límites de extracción por defecto ([backends.download] max_extract_bytes / max_extract_files):
# un archivo pequeño puede expandirse hasta llenar el disco (bomba de descompresión).
MAX_EXTRACT_BYTES = 20 << 30
MAX_EXTRACT_FILES = 100_000
ARCHIVES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar", ".zip")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")


class DownloadCancelled(Exception):
    pass


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlsplit(newurl).scheme != "https":
            raise ValueError(f"redirección rechazada: no es https ({newurl.split('?')[0]})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GiB"


def fetch(
    url: str,
    dest: Path,
    *,
    log: Any,
    cancelled: Any,
    ca_bundle: str | None = None,
    max_bytes: int = 5 << 30,
    timeout_s: float = 60,
) -> tuple[int, str]:
    """Descarga ``url`` en ``dest`` (bloqueante; se llama en un hilo). Devuelve (bytes, sha256)."""
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _HttpsOnlyRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": "watcher-mcp", "Accept-Encoding": "identity"})
    digest = hashlib.sha256()
    with opener.open(req, timeout=timeout_s) as resp:
        final = resp.geturl()
        require_https(final, what="URL final")
        total = int(resp.headers.get("Content-Length") or 0)
        if total > max_bytes:
            raise ValueError(f"el fichero ocupa {_human(total)}, más que el máximo permitido ({_human(max_bytes)})")
        log(f"descargando {_human(total) if total else 'tamaño desconocido'} desde {urlsplit(final).hostname}")
        got, t0, last = 0, time.monotonic(), 0.0
        with open(dest, "wb") as fh:
            while True:
                if cancelled():
                    raise DownloadCancelled()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    raise ValueError(f"se superó el tamaño máximo ({_human(max_bytes)})")
                fh.write(chunk)
                digest.update(chunk)
                now = time.monotonic()
                if now - last >= 2:
                    last = now
                    pct = f" ({got * 100 // total}%)" if total else ""
                    log(f"  {_human(got)}{' de ' + _human(total) if total else ''}{pct} a {_human(got / max(now - t0, 1e-3))}/s")
        if total and got != total:
            raise ValueError(f"descarga incompleta: {got} de {total} bytes")
    log(f"descargado {_human(got)} en {time.monotonic() - t0:.1f}s; sha256 {digest.hexdigest()}")
    return got, digest.hexdigest()


def _strip(name: str, n: int) -> str | None:
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".")]
    if n:
        parts = parts[n:]
    return "/".join(parts) if parts else None


def _safe_target(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if not (target == root or target.is_relative_to(root)) or PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
        raise ValueError(f"entrada insegura en el archivo: {rel!r}")
    return target


class _Budget:
    """Cuenta bytes y ficheros extraídos y corta al superar los límites."""

    def __init__(self, max_bytes: int, max_files: int, cancelled: Any) -> None:
        self.max_bytes, self.max_files, self.cancelled = max_bytes, max_files, cancelled
        self.bytes = 0
        self.files = 0

    def new_file(self) -> None:
        self.files += 1
        if self.max_files and self.files > self.max_files:
            raise ValueError(f"el archivo tiene más de {self.max_files} ficheros (max_extract_files)")

    def copy(self, src: Any, dst: Any) -> None:
        while True:
            if self.cancelled():
                raise DownloadCancelled()
            chunk = src.read(CHUNK)
            if not chunk:
                return
            self.bytes += len(chunk)
            if self.max_bytes and self.bytes > self.max_bytes:
                raise ValueError(f"lo extraído supera {_human(self.max_bytes)} (max_extract_bytes): ¿bomba de descompresión?")
            dst.write(chunk)


def extract(
    archive: Path,
    dest: Path,
    *,
    strip_components: int = 0,
    member: str | None = None,
    log: Any,
    overwrite: bool = True,
    max_bytes: int = MAX_EXTRACT_BYTES,
    max_files: int = MAX_EXTRACT_FILES,
    cancelled: Any = lambda: False,
) -> list[Path]:
    """Extrae ``archive`` en ``dest`` de forma segura. Con ``member`` extrae solo ese fichero.

    Límites de tamaño total y de número de ficheros; si se superan (o se cancela) se borran
    los ficheros ya escritos. Con ``overwrite=False`` no se pisa ningún fichero existente.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written: list[Path] = []
    budget = _Budget(max_bytes, max_files, cancelled)
    try:
        _extract_into(archive, root, strip_components, member, overwrite, budget, written, log)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    if member and not written:
        raise ValueError(f"el archivo no contiene {member!r}")
    log(f"extraídos {len(written)} ficheros ({_human(budget.bytes)}) en {dest}")
    return written


def _claim(target: Path, overwrite: bool) -> None:
    if not overwrite and target.exists():
        raise ValueError(f"{target} ya existe (overwrite=false)")
    target.parent.mkdir(parents=True, exist_ok=True)


def _extract_into(
    archive: Path, root: Path, strip_components: int, member: str | None, overwrite: bool,
    budget: _Budget, written: list[Path], log: Any,
) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                rel = _strip(info.filename, strip_components)
                if rel is None or info.is_dir() or (member and rel != member):
                    continue
                target = _safe_target(root, rel)
                budget.new_file()
                _claim(target, overwrite)
                written.append(target)
                with zf.open(info) as src, open(target, "wb") as dst:
                    budget.copy(src, dst)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)
    else:
        with tarfile.open(archive) as tf:
            for ti in tf.getmembers():
                rel = _strip(ti.name, strip_components)
                if rel is None or (member and rel != member):
                    continue
                if not (ti.isfile() or ti.isdir()):
                    log(f"  omitido {ti.name!r}: solo se extraen ficheros y directorios")
                    continue
                target = _safe_target(root, rel)
                if ti.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                budget.new_file()
                _claim(target, overwrite)
                written.append(target)
                src = tf.extractfile(ti)
                assert src is not None
                with src, open(target, "wb") as dst:
                    budget.copy(src, dst)
                target.chmod(ti.mode & 0o755 or 0o644)


def _filename_from_url(url: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    return name if _FILENAME.match(name or "") else "download.bin"


class DownloadParams(LimitParams):
    url: str = Field(..., description="URL https:// del fichero.")
    dest: str = Field(
        ...,
        description="Ruta absoluta (dentro de las raíces permitidas). Si es un directorio existente o "
        "acaba en '/', el fichero conserva su nombre; con extract=true es el directorio destino.",
    )
    checksum: str | None = Field(None, description="'sha256:<hex>' (también sha384/sha512). Muy recomendado.")
    extract: bool = Field(False, description="Extraer tar.gz/tgz/tar.xz/tar.bz2/tar/zip en dest.")
    strip_components: int = Field(0, ge=0, le=10, description="Como tar --strip-components.")
    overwrite: bool = Field(
        False, description="Sustituir el destino (o los ficheros extraídos) si ya existen. Por defecto no se pisa nada."
    )


class DownloadBackend(Backend[DownloadParams]):
    name = "download"
    description = "Descarga un fichero por HTTPS (checksum opcional) y, si se pide, lo extrae de forma segura."
    Params = DownloadParams
    default_stall_timeout_s = 120.0  # el progreso se anota cada ~2 s: 2 min sin bytes es un atasco real

    # --- validación ---------------------------------------------------------------
    def check(self, spec: DownloadParams) -> None:
        require_https(spec.url, what="url")
        self.config.safe_path(spec.dest, what="dest")
        if spec.checksum:
            Checksum.parse(spec.checksum)
        if spec.extract and not spec.url.split("?")[0].lower().endswith(ARCHIVES):
            raise ValueError(f"extract=true necesita un archivo {', '.join(ARCHIVES)}")

    def target(self, spec: DownloadParams) -> Path:
        dest = self.config.safe_path(spec.dest, what="dest")
        if spec.extract or spec.dest.endswith("/") or dest.is_dir():
            return dest / _filename_from_url(spec.url) if not spec.extract else dest
        return dest

    # --- pasos reutilizables ---------------------------------------------------------
    def _fetch_step(self, url: str, part: Path, holder: dict[str, Any]) -> FuncStep:
        opts = self.options

        async def run(ctx: StepContext) -> None:
            part.parent.mkdir(parents=True, exist_ok=True)
            ctx.log(f"GET {url.split('?')[0]}")
            try:
                size, sha = await asyncio.to_thread(
                    fetch, url, part, log=ctx.log, cancelled=ctx.cancelled,
                    ca_bundle=opts.get("ca_bundle"), max_bytes=int(opts.get("max_bytes", 5 << 30)),
                    timeout_s=float(opts.get("read_timeout_s", 60)),
                )
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            holder.update(size=size, sha256=sha)

        return FuncStep("download", run)

    @staticmethod
    def _verify_step(checksum: str, part: Path, holder: dict[str, Any]) -> FuncStep:
        expected = Checksum.parse(checksum)

        async def run(ctx: StepContext) -> None:
            if expected.algorithm == "sha256" and "sha256" in holder:
                ok = holder["sha256"] == expected.hexdigest
            else:
                ok = await asyncio.to_thread(expected.verify_file, part)
            if not ok:
                part.unlink(missing_ok=True)
                raise ValueError(f"checksum {expected.algorithm} no coincide (esperado {expected.hexdigest})")
            ctx.log(f"checksum {expected.algorithm} correcto")

        return FuncStep("verify", run)

    # --- plan ------------------------------------------------------------------------
    def plan(self, spec: DownloadParams) -> Plan:
        target = self.target(spec)
        # Nombre único por job: si un job cancelado aún no ha soltado su hilo, nunca borra el .part de otro.
        part = (target.parent if not spec.extract else target) / f".{_filename_from_url(spec.url)}.{secrets.token_hex(4)}.part"
        holder: dict[str, Any] = {}
        steps = [self._fetch_step(spec.url, part, holder)]
        if spec.checksum:
            steps.append(self._verify_step(spec.checksum, part, holder))

        async def place(ctx: StepContext) -> None:
            if spec.extract:
                archive = part.with_name(_filename_from_url(spec.url))
                part.replace(archive)
                try:
                    await asyncio.to_thread(
                        extract, archive, target, strip_components=spec.strip_components, log=ctx.log,
                        overwrite=spec.overwrite, cancelled=ctx.cancelled, **self._extract_limits(),
                    )
                finally:
                    archive.unlink(missing_ok=True)
            else:
                if target.exists() and not spec.overwrite:
                    part.unlink(missing_ok=True)
                    raise ValueError(f"{target} ya existe (overwrite=false)")
                part.replace(target)
                ctx.log(f"guardado en {target}")

        steps.append(FuncStep("extract" if spec.extract else "place", place))
        return Plan(
            summary=f"descargar {spec.url.split('?')[0]} -> {target}",
            steps=steps,
            lock_key=f"path:{target}",
            secrets=url_secrets(spec.url) + _query_secrets(spec.url),
            **spec.limits(),
        )

    def _extract_limits(self) -> dict[str, int]:
        return {
            "max_bytes": int(self.options.get("max_extract_bytes", MAX_EXTRACT_BYTES)),
            "max_files": int(self.options.get("max_extract_files", MAX_EXTRACT_FILES)),
        }

    async def detect_versions(self, spec: DownloadParams, probe: Probe) -> dict[str, str]:
        target = self.target(spec)
        if spec.extract:
            files = [p for p in target.rglob("*") if p.is_file()]
            return {target.name: f"{len(files)} ficheros"}
        if target.is_file():
            h = await asyncio.to_thread(_sha256_file, target)
            return {target.name: f"{_human(target.stat().st_size)} sha256:{h[:16]}…"}
        return {}

    def result_info(self, spec: DownloadParams) -> dict[str, Any]:
        return {"path": str(self.target(spec))}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _query_secrets(url: str) -> list[str]:
    """Valores de parámetros con pinta de credencial en la query (token=, sig=, key=...)."""
    out = []
    for part in urlsplit(url).query.split("&"):
        k, _, v = part.partition("=")
        if v and re.search(r"(?i)token|sig|signature|key|secret|password|auth|credential", k):
            out.append(unquote(v))
    return out


class BinaryParams(LimitParams):
    url: str = Field(..., description="URL https:// del binario o de un archivo que lo contiene.")
    checksum: str = Field(..., description="'sha256:<hex>' del fichero descargado (obligatorio).")
    name: str = Field(..., description="Nombre del ejecutable instalado, p. ej. 'jq'.")
    member: str | None = Field(None, description="Ruta del binario dentro del archivo (si la URL es tar/zip).")
    bin_dir: str | None = Field(
        None, description="Directorio destino absoluto; por defecto [backends.binary] bin_dir o <state_dir>/bin."
    )
    version_args: list[str] = Field(default_factory=lambda: ["--version"], max_length=4)
    overwrite: bool = Field(False, description="Sustituir el ejecutable si ya existe en bin_dir.")


class BinaryBackend(DownloadBackend):
    name = "binary"
    description = "Instala un ejecutable suelto descargado por HTTPS con checksum obligatorio (opcionalmente desde tar/zip)."
    Params = BinaryParams  # type: ignore[assignment]

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # Hereda ca_bundle, max_bytes... de [backends.download]; [backends.binary] manda.
        self.options = {**config.backend_options("download"), **config.backend_options("binary")}

    def bin_dir(self, spec: BinaryParams) -> Path:  # type: ignore[override]
        raw = spec.bin_dir or self.options.get("bin_dir") or str(self.config.state_dir / "bin")
        return self.config.safe_path(os.path.expanduser(raw), what="bin_dir")

    def check(self, spec: BinaryParams) -> None:  # type: ignore[override]
        require_https(spec.url, what="url")
        Checksum.parse(spec.checksum)
        if not _FILENAME.match(spec.name):
            raise ValueError(f"name no válido: {spec.name!r}")
        if spec.member and (spec.member.startswith("/") or ".." in PurePosixPath(spec.member).parts):
            raise ValueError(f"member no válido: {spec.member!r}")
        if spec.member and not spec.url.split("?")[0].lower().endswith(ARCHIVES):
            raise ValueError("member solo tiene sentido si la URL es un archivo tar/zip")
        for a in spec.version_args:
            if not re.match(r"^-{0,2}[A-Za-z0-9][A-Za-z0-9_-]*$", a):
                raise ValueError(f"version_args no válido: {a!r}")
        self.bin_dir(spec)

    def plan(self, spec: BinaryParams) -> Plan:  # type: ignore[override]
        bin_dir = self.bin_dir(spec)
        target = bin_dir / spec.name
        part = bin_dir / f".{spec.name}.{secrets.token_hex(4)}.download.part"
        holder: dict[str, Any] = {}
        steps = [self._fetch_step(spec.url, part, holder), self._verify_step(spec.checksum, part, holder)]

        async def install(ctx: StepContext) -> None:
            if target.exists() and not spec.overwrite:
                part.unlink(missing_ok=True)
                raise ValueError(f"{target} ya existe (overwrite=false)")
            if spec.member:
                tmp = bin_dir / f".{spec.name}.extract"
                archive = tmp.with_name(f".{spec.name}" + _archive_suffix(spec.url))
                part.replace(archive)
                try:
                    files = await asyncio.to_thread(
                        extract, archive, tmp, member=spec.member, log=ctx.log, cancelled=ctx.cancelled,
                        **self._extract_limits(),
                    )
                    files[0].replace(target)
                finally:
                    archive.unlink(missing_ok=True)
                    shutil.rmtree(tmp, ignore_errors=True)
            else:
                part.replace(target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRUSR)
            ctx.log(f"instalado {target} (ejecutable)")

        steps.append(FuncStep("install", install))
        return Plan(
            summary=f"binario {spec.name} desde {spec.url.split('?')[0]} -> {target}",
            steps=steps,
            lock_key=f"path:{target}",
            secrets=url_secrets(spec.url) + _query_secrets(spec.url),
            **spec.limits(),
        )

    async def detect_versions(self, spec: BinaryParams, probe: Probe) -> dict[str, str]:  # type: ignore[override]
        target = self.bin_dir(spec) / spec.name
        if not target.exists():
            return {}
        if spec.version_args:
            res = await probe([str(target), *spec.version_args], timeout_s=15)
            text = (res.stdout or res.stderr).strip().splitlines()
            if res.exit_code == 0 and text:
                return {spec.name: text[0][:120]}
        return {spec.name: "instalado"}

    def result_info(self, spec: BinaryParams) -> dict[str, Any]:  # type: ignore[override]
        bin_dir = self.bin_dir(spec)
        return {"path": str(bin_dir / spec.name), "bin_dir": str(bin_dir),
                "hint": f"añade {bin_dir} al PATH si no lo está"}


def _archive_suffix(url: str) -> str:
    low = url.split("?")[0].lower()
    return next(s for s in ARCHIVES if low.endswith(s))
