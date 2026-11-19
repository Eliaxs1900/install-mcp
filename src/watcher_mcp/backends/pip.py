"""Backend ``pip``: instala paquetes Python en un venv aislado, con ``uv`` si está.

Pasos del plan:

1. ``create-venv`` – ``uv venv <venv>`` o ``<python> -m venv <venv>`` (se omite si ya existe).
2. ``install``     – ``uv pip install --python <venv>/bin/python …`` o
   ``<venv>/bin/python -m pip install --progress-bar off …``.
3. (sonda) versiones instaladas leyendo ``importlib.metadata`` con el python del venv.

Nunca instala en el Python del sistema ni usa ``sudo``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..security import reject_option_like, require_https, url_secrets
from .base import Backend, BackendParams, CommandStep, Plan, Probe

# Subconjunto de PEP 508 sin URLs: nombre[extras] (especificadores) (; marcadores)
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VER = r"(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+"
_REQ = re.compile(
    rf"^\s*({_NAME})\s*(\[\s*{_NAME}(?:\s*,\s*{_NAME})*\s*\])?\s*"
    rf"(\(?\s*{_VER}(?:\s*,\s*{_VER})*\s*\)?)?\s*"
    r"(;[A-Za-z0-9_.<>=!~'\" ()]+)?\s*$"
)
_VENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PY_VERSION = re.compile(r"^3(\.\d{1,2}){0,2}$")
_PY_EXE = re.compile(r"^python3(\.\d{1,2})?t?$")

# Imprime solo las distribuciones pedidas (nombres canónicos en argv): en un venv grande, listarlas
# todas (miles) pasaría de los megas para nada.
_PROBE_CODE = (
    "import json, re, importlib.metadata as m, sys; "
    "w = set(sys.argv[1:]); "
    "c = lambda n: re.sub(r'[-_.]+', '-', n).lower(); "
    "print(json.dumps({'python': sys.version.split()[0], "
    "'dists': {d.metadata['Name']: d.version for d in m.distributions() "
    "if d.metadata['Name'] and c(d.metadata['Name']) in w}}))"
)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class PipParams(BackendParams):
    packages: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="Requisitos PEP 508 sin URLs, p. ej. 'pypdf', 'requests>=2.32', 'rich[jupyter]==13.9.4'.",
    )
    requirements: str | None = Field(None, description="Ruta absoluta a un requirements.txt.")
    venv: str = Field(
        "default",
        description="Nombre (se crea en <state_dir>/venvs/<nombre>) o ruta absoluta dentro de las raíces permitidas.",
    )
    python: str | None = Field(
        None, description="Versión ('3.12', requiere uv o python3.12 en PATH) o ruta absoluta a un intérprete."
    )
    installer: Literal["auto", "uv", "pip"] = Field("auto", description="auto = uv si está disponible.")
    upgrade: bool = False
    pre: bool = Field(False, description="Permitir versiones preliminares.")
    index_url: str | None = Field(None, description="Índice alternativo (solo https). Las credenciales se redactan.")
    extra_index_urls: list[str] = Field(default_factory=list, max_length=10)
    find_links: list[str] = Field(
        default_factory=list, max_length=10, description="Directorios absolutos o URLs https con wheels."
    )
    no_index: bool = Field(False, description="No consultar PyPI (instalación offline con find_links).")
    job_timeout_s: float | None = Field(
        None, ge=0, description="Tiempo máximo del job; al vencer se mata y queda timed_out (0 = sin límite)."
    )
    stall_timeout_s: float | None = Field(
        None, ge=0, description="Sin salida durante N s -> estado stalled + aviso (0 = sin watchdog)."
    )


class PipBackend(Backend[PipParams]):
    name = "pip"
    description = "Paquetes Python en un venv aislado (uv pip si está instalado; si no, python -m venv + pip)."
    Params = PipParams
    executables = ()  # usa el Python del servidor como último recurso
    # Compilar un wheel desde el código fuente puede pasar minutos sin escribir nada.
    default_stall_timeout_s = 600.0

    # --- validación -----------------------------------------------------------
    def check(self, spec: PipParams) -> None:
        if not spec.packages and not spec.requirements:
            raise ValueError("indica 'packages' o 'requirements'")
        reject_option_like(spec.packages, what="packages")
        for req in spec.packages:
            if not _REQ.match(req):
                raise ValueError(f"requisito no válido (o con URL, no admitida): {req!r}")
        if spec.requirements:
            # Dentro de las raíces y fuera de zonas protegidas: pip/uv copian en su error las líneas
            # que no entienden, así que leer un fichero arbitrario (~/.ssh/...) lo filtraría al log.
            p = self.config.safe_path(spec.requirements, what="requirements")
            if not p.is_file():
                raise ValueError(f"requirements debe ser un fichero existente: {spec.requirements!r}")
        self.venv_path(spec)  # valida nombre/ruta
        if spec.python and not (_PY_VERSION.match(spec.python) or Path(spec.python).is_absolute()):
            raise ValueError("python debe ser una versión como '3.12' o una ruta absoluta")
        if spec.python and Path(spec.python).is_absolute() and not _PY_EXE.match(Path(spec.python).name):
            raise ValueError(f"python debe ser un intérprete (python3, python3.12...), no {Path(spec.python).name!r}")
        if spec.python and Path(spec.python).is_absolute() and not os.access(spec.python, os.X_OK):
            raise ValueError(f"el intérprete {spec.python!r} no existe o no es ejecutable")
        for url in [u for u in [spec.index_url, *spec.extra_index_urls] if u]:
            require_https(url, what="index_url")
        for link in spec.find_links:
            if link.startswith(("http://", "https://")):
                require_https(link, what="find_links")
            elif not (Path(link).is_absolute() and self.config.safe_path(link, what="find_links").is_dir()):
                raise ValueError(f"find_links debe ser una URL https o un directorio absoluto existente: {link!r}")
        if spec.installer == "uv" and not self.uv():
            raise ValueError("installer='uv' pero uv no está en PATH")
        if spec.python and _PY_VERSION.match(spec.python) and self.installer(spec) == "pip":
            if not shutil.which(f"python{spec.python}"):
                raise ValueError(f"sin uv hace falta python{spec.python} en PATH (o pasa una ruta absoluta)")

    # --- helpers ------------------------------------------------------------------
    def uv(self) -> str | None:
        configured = self.options.get("uv_path")
        if configured:
            return configured if os.access(configured, os.X_OK) else None
        if self.options.get("prefer_uv", True) is False:
            return None
        return shutil.which("uv")

    def installer(self, spec: PipParams) -> str:
        if spec.installer == "auto":
            return "uv" if self.uv() else "pip"
        return spec.installer

    def venv_path(self, spec: PipParams) -> Path:
        if _VENV_NAME.match(spec.venv):
            return (self.config.venvs_dir / spec.venv).expanduser().resolve()
        return self.config.safe_path(spec.venv, what="ruta del venv")

    @staticmethod
    def venv_python(venv: Path) -> Path:
        return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _base_python(self, spec: PipParams) -> str:
        if spec.python and Path(spec.python).is_absolute():
            return spec.python
        if spec.python:
            found = shutil.which(f"python{spec.python}")
            if found:
                return found
        return self.options.get("base_python") or sys.executable

    # --- plan -----------------------------------------------------------------------
    def plan(self, spec: PipParams) -> Plan:
        venv = self.venv_path(spec)
        py = self.venv_python(venv)
        tool = self.installer(spec)
        env = {"PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "UV_NO_PROGRESS": "1"}
        remove = ("VIRTUAL_ENV", "PYTHONHOME", "CONDA_PREFIX", "UV_PYTHON")

        if tool == "uv":
            uv = self.uv() or "uv"
            create = [uv, "venv", "--no-progress", str(venv)]
            if spec.python:
                create += ["--python", spec.python]
            install = [uv, "pip", "install", "--no-progress", "--python", str(py)]
        else:
            create = [self._base_python(spec), "-m", "venv", str(venv)]
            install = [str(py), "-m", "pip", "install", "--progress-bar", "off", "--no-input"]

        opts: list[str] = []
        if spec.upgrade:
            opts.append("--upgrade")
        if spec.pre:
            opts.append("--pre")
        if spec.index_url:
            opts += ["--index-url", spec.index_url]
        for url in spec.extra_index_urls:
            opts += ["--extra-index-url", url]
        for link in spec.find_links:
            opts += ["--find-links", link]
        if spec.no_index:
            opts.append("--no-index")
        if spec.requirements:
            opts += ["-r", str(Path(spec.requirements).expanduser())]

        secrets: list[str] = []
        for url in [spec.index_url, *spec.extra_index_urls, *spec.find_links]:
            if url:
                secrets += url_secrets(url)

        what = ", ".join(spec.packages) or f"-r {spec.requirements}"
        return Plan(
            summary=f"pip install {what} en {venv} (vía {tool})",
            steps=[
                CommandStep("create-venv", create, env=env, env_remove=remove, skip_if=py.exists, timeout_s=600),
                # "--" separa opciones de requisitos: un requisito nunca se interpreta como opción.
                CommandStep(
                    "install",
                    [*install, *opts, "--", *spec.packages] if spec.packages else [*install, *opts],
                    env=env,
                    env_remove=remove,
                ),
            ],
            lock_key=f"venv:{venv}",
            secrets=secrets,
            timeout_s=spec.job_timeout_s,
            stall_timeout_s=spec.stall_timeout_s,
        )

    # --- resultado ---------------------------------------------------------------------
    async def detect_versions(self, spec: PipParams, probe: Probe) -> dict[str, str]:
        py = self.venv_python(self.venv_path(spec))
        wanted = [canonical(m.group(1)) for r in spec.packages if (m := _REQ.match(r))]
        if spec.requirements:
            wanted += self._names_in_requirements(Path(spec.requirements))
        res = await probe([str(py), "-I", "-c", _PROBE_CODE, *dict.fromkeys(wanted)])
        if res.exit_code != 0:
            return {}
        try:
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}
        dists: dict[str, str] = {canonical(k): v for k, v in data.get("dists", {}).items()}
        found = {name: dists[name] for name in dict.fromkeys(wanted) if name in dists}
        missing = [n for n in dict.fromkeys(wanted) if n not in dists]
        if missing:
            found.update({n: "NO ENCONTRADO" for n in missing})
        found["(python)"] = data.get("python", "?")
        return found

    @staticmethod
    def _names_in_requirements(path: Path) -> list[str]:
        names = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-") and (m := _REQ.match(line)):
                    names.append(canonical(m.group(1)))
        except OSError:
            pass
        return names

    def result_info(self, spec: PipParams) -> dict[str, Any]:
        venv = self.venv_path(spec)
        return {
            "venv": str(venv),
            "python": str(self.venv_python(venv)),
            "installer": self.installer(spec),
            "activate": f"source {venv}/bin/activate",
        }
