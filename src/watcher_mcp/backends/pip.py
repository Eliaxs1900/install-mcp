"""Backend ``pip``: instala paquetes Python en un venv aislado, con ``uv`` si está.

Pasos del plan:

1. ``create-venv`` – ``uv venv <venv>`` o ``<python> -m venv <venv>`` (se omite si ya existe).
2. ``install``     – ``uv pip install --python <venv>/bin/python …`` o
   ``<venv>/bin/python -m pip install --progress-bar off …``.
3. (sonda) versiones instaladas leyendo ``importlib.metadata`` con el python del venv.

Nunca instala en el Python del sistema ni usa ``sudo``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import secrets as secrets_lib
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from ..security import reject_option_like, require_https, url_secrets
from .base import Backend, BackendParams, CommandStep, FuncStep, Plan, Probe, StepContext

if TYPE_CHECKING:
    from ..config import Config

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


# --- contenido de un requirements.txt --------------------------------------------------------------
# pip/uv interpretan opciones dentro del propio fichero (``-r otro``, ``-c``, ``--index-url http://…``,
# ``-e``, ``--find-links``…) que se saltan todas las validaciones de los parámetros: un ``-r ~/.netrc`` lee un
# fichero protegido y pip copia en su error la línea que no entiende.
#
# Validar el fichero y dejar que pip lo lea después NO basta: (1) validador y pip pueden interpretar de forma
# distinta lo mismo (un comentario que acaba en ``\`` no continúa en pip), y (2) el fichero puede cambiar
# entre que se valida (al enviar el job) y que pip lo lee (al salir de la cola). Por eso, justo antes de
# lanzar pip, se lee el fichero UNA vez, se normaliza (solo líneas validadas, los ``-r`` aplanados, los
# ``-c`` en ficheros aparte) y se escribe en una carpeta privada; pip recibe esa copia, nunca la original.
MAX_REQUIREMENTS_DEPTH = 8
MAX_REQUIREMENTS_BYTES = 1 << 20
MAX_REQUIREMENTS_LINES = 50_000
_COMMENT = re.compile(r"(^|\s+)#.*$")  # como COMMENT_RE de pip
_HASH_OPTION = re.compile(r"\s+--hash[= ]\S+")
_HASH_VALUE = re.compile(r"^(sha256|sha384|sha512):[0-9a-fA-F]{32,128}$")
# Opción larga -> tipo de valor.  ``file``: otro requirements; ``index``: URL https; ``links``: URL https o
# directorio dentro de las raíces; ``flag``: sin valor; ``free``: nombres de paquetes/formatos (``:all:``,
# ``:none:``, ``pkg``), validados con _REQ.
_REQ_OPTIONS: dict[str, str] = {
    "--requirement": "file", "--constraint": "file",
    "--index-url": "index", "--extra-index-url": "index",
    "--find-links": "links",
    "--no-index": "flag", "--pre": "flag", "--prefer-binary": "flag", "--require-hashes": "flag",
    "--only-binary": "free", "--no-binary": "free",
}
_SHORT_REQ_OPTIONS = {"-r": "--requirement", "-c": "--constraint", "-i": "--index-url", "-f": "--find-links"}


@dataclass
class NormalizedRequirements:
    """Resultado de normalizar un requirements.txt: contenido de la copia principal y de los ``-c``."""

    lines: list[str]
    constraints: list[list[str]]  # un fichero de constraints por cada ``-c`` (en orden de aparición)

    def render(self, constraint_paths: list[Path]) -> str:
        """Contenido de la copia principal (``-c`` apunta a las copias privadas)."""
        header = [f"-c {shlex.quote(str(p))}" for p in constraint_paths]
        return "\n".join([*header, *self.lines]) + "\n"


def _bad_line(path: Path, number: int, why: str, line: str) -> ValueError:
    shown = line if len(line) <= 60 else line[:57] + "…"
    return ValueError(f"{path}:{number}: {why}: {shown!r}")


def _join_lines(text: str) -> list[tuple[int, str]]:
    """Líneas lógicas con el MISMO criterio que pip (``join_lines``): una línea que acaba en ``\\`` se une a la
    siguiente salvo que sea un comentario (``# nota \\`` no continúa). Parte por ``str.splitlines`` (como pip),
    es decir, también por ``\\r`` suelto, ``\\x0c``, etc."""
    out: list[tuple[int, str]] = []
    pending: list[str] = []
    first = 0
    for number, line in enumerate(text.splitlines(), start=1):
        is_comment = _COMMENT.match(line) is not None
        if not line.endswith("\\") or is_comment:
            if is_comment:
                line = " " + line
            if pending:
                pending.append(line)
                out.append((first, "".join(pending)))
                pending = []
            else:
                out.append((number, line))
        else:
            if not pending:
                first = number
            pending.append(line.strip("\\"))
    if pending:
        out.append((first, "".join(pending)))
    return out


class _Normalizer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.constraints: list[list[str]] = []
        self.total = 0

    def read(self, path: Path) -> str:
        """Lee el fichero (ya validado como ruta) una sola vez y con tope de tamaño."""
        try:
            with open(path, "rb") as fh:
                data = fh.read(MAX_REQUIREMENTS_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"{path}: no se puede leer como requirements.txt ({exc.__class__.__name__})") from None
        if len(data) > MAX_REQUIREMENTS_BYTES:
            raise ValueError(f"{path}: más de {MAX_REQUIREMENTS_BYTES >> 10} KiB: no es un requirements.txt razonable")
        try:
            return data.decode("utf-8-sig")  # pip también ignora el BOM
        except UnicodeDecodeError:
            raise ValueError(f"{path}: no se puede leer como requirements.txt (no es UTF-8)") from None

    def process(self, path: Path, out: list[str], depth: int, active: frozenset[Path]) -> None:
        path = self.config.safe_path(path, what=f"requirements {path}")  # raíces + zonas protegidas, cada vez
        if path in active:
            return  # inclusión circular: ya se está procesando
        if depth > MAX_REQUIREMENTS_DEPTH:
            raise ValueError(f"{path}: demasiados niveles de -r/-c anidados (máximo {MAX_REQUIREMENTS_DEPTH})")
        active = active | {path}
        for number, raw in _join_lines(self.read(path)):
            line = _COMMENT.sub("", raw).strip()
            if not line:
                continue
            self.total += 1
            if self.total > MAX_REQUIREMENTS_LINES:
                raise ValueError(f"{path}: demasiadas líneas entre todos los requirements incluidos")
            self.line(path, number, line, out, depth, active)

    def line(self, path: Path, number: int, line: str, out: list[str], depth: int, active: frozenset[Path]) -> None:
        if "$" in line:
            raise _bad_line(path, number, "las variables de entorno (${VAR}) no se admiten", line)
        if not line.startswith("-"):
            requirement, hashes = line, []
            if _HASH_OPTION.search(line):
                requirement = _HASH_OPTION.sub("", line)
                hashes = re.findall(r"\s+--hash[= ](\S+)", line)
            if not _REQ.match(requirement) or not all(_HASH_VALUE.match(h) for h in hashes):
                raise _bad_line(path, number, "requisito no válido (las URL, rutas y VCS no se admiten)", line)
            out.append(" ".join([requirement.strip(), *[f"--hash={h}" for h in hashes]]))
            return
        try:
            tokens = shlex.split(line)
        except ValueError:
            raise _bad_line(path, number, "línea con comillas sin cerrar", line) from None
        option, value = _split_option(tokens[0], tokens[1:])
        kind = _REQ_OPTIONS.get(option)
        if kind is None:
            raise _bad_line(path, number, "opción no admitida en un requirements.txt", line)
        if kind == "flag":
            out.append(option)
            return
        if not value:
            raise _bad_line(path, number, f"{option} necesita un valor", line)
        if kind == "free":
            if not all(v in (":all:", ":none:") or _REQ.match(v) for v in value.split(",")):
                raise _bad_line(path, number, f"valor no válido para {option}", line)
            out.append(f"{option} {value}")
        elif kind == "index" or (kind == "links" and value.startswith(("http://", "https://"))):
            require_https(value, what=f"{option} en {path.name}:{number}")
            out.append(f"{option} {value}")
        else:  # file | links (directorio local)
            target = self.config.safe_path(_resolve_included(path.parent, value), what=f"{option} en {path}:{number}")
            if kind == "links":
                if not target.is_dir():
                    raise _bad_line(path, number, "find-links debe ser una URL https o un directorio", line)
                out.append(f"{option} {shlex.quote(str(target))}")
            elif not target.is_file():
                raise _bad_line(path, number, "el fichero incluido no existe", line)
            elif option == "--requirement":
                self.process(target, out, depth + 1, active)  # aplanado: sus líneas pasan a esta copia
            else:  # --constraint: copia aparte
                self.constraints.append([])
                index = len(self.constraints) - 1
                self.process(target, self.constraints[index], depth + 1, active)


def normalize_requirements(path: Path, config: Config) -> NormalizedRequirements:
    """Lee ``path`` (y lo que incluye) y devuelve su versión normalizada: solo líneas validadas (requisitos sin
    URL ni rutas, índices https, rutas dentro de las raíces y fuera de zonas protegidas, flags conocidos).
    ValueError si algo no se puede garantizar."""
    normalizer = _Normalizer(config)
    lines: list[str] = []
    normalizer.process(path, lines, 0, frozenset())
    return NormalizedRequirements(lines, normalizer.constraints)


def validate_requirements_file(path: Path, config: Config) -> None:
    """Valida pronto (al enviar el job); la copia que lee pip se genera después con ``normalize_requirements``."""
    normalize_requirements(path, config)


def write_private_copy(normalized: NormalizedRequirements, directory: Path) -> Path:
    """Escribe la copia normalizada en ``directory`` (0700; ficheros 0600) y devuelve la ruta del requirements."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    constraint_paths = []
    for i, lines in enumerate(normalized.constraints):
        target = directory / f"constraints-{i}.txt"
        _write_private(target, "\n".join(lines) + "\n")
        constraint_paths.append(target)
    main = directory / "requirements.txt"
    _write_private(main, normalized.render(constraint_paths))
    return main


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def prune_private_copies(root: Path, max_age_s: float = 86400) -> None:
    """Borra las copias privadas de más de un día (no hay gancho de fin de job donde hacerlo antes)."""
    now = time.time()
    try:
        for entry in root.iterdir():
            if entry.is_dir() and now - entry.stat().st_mtime > max_age_s:
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass


def _split_option(first: str, rest: list[str]) -> tuple[str, str | None]:
    """``(-r f | -rf | --requirement f | --requirement=f)`` -> (opción larga canónica, valor)."""
    if first.startswith("--"):
        name, eq, value = first.partition("=")
        if eq:
            return name, value or None
        return name, (rest[0] if rest else None)
    short = first[:2]
    if short in _SHORT_REQ_OPTIONS:
        attached = first[2:]
        return _SHORT_REQ_OPTIONS[short], attached or (rest[0] if rest else None)
    return first, None


def _resolve_included(base: Path, value: str) -> Path:
    """Ruta de un fichero incluido: ``~`` se expande (uv lo hace) y lo relativo cuelga del fichero que lo incluye."""
    expanded = Path(os.path.expanduser(value))
    return expanded if expanded.is_absolute() else base / expanded


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
            # También lo que el fichero incluye y sus opciones (-r, -c, --index-url…): ver validate_requirements_file.
            validate_requirements_file(p, self.config)
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
        private_dir: Path | None = None
        if spec.requirements:
            # Pip lee la copia normalizada y privada que genera el paso «requirements» justo antes de instalar,
            # no el fichero original (ver la nota sobre ``normalize_requirements``).
            private_dir = self.config.state_dir / "requirements" / secrets_lib.token_hex(8)
            opts += ["-r", str(private_dir / "requirements.txt")]

        secrets: list[str] = []
        for url in [spec.index_url, *spec.extra_index_urls, *spec.find_links]:
            if url:
                secrets += url_secrets(url)

        what = ", ".join(spec.packages) or f"-r {spec.requirements}"
        steps: list[Any] = [
            CommandStep("create-venv", create, env=env, env_remove=remove, skip_if=py.exists, timeout_s=600),
        ]
        if private_dir is not None and spec.requirements:
            steps.append(self._requirements_step(Path(spec.requirements), private_dir))
        # "--" separa opciones de requisitos: un requisito nunca se interpreta como opción.
        steps.append(CommandStep(
            "install",
            [*install, *opts, "--", *spec.packages] if spec.packages else [*install, *opts],
            env=env,
            env_remove=remove,
        ))
        return Plan(
            summary=f"pip install {what} en {venv} (vía {tool})",
            steps=steps,
            lock_key=f"venv:{venv}",
            secrets=secrets,
            timeout_s=spec.job_timeout_s,
            stall_timeout_s=spec.stall_timeout_s,
        )

    def _requirements_step(self, source: Path, private_dir: Path) -> FuncStep:
        """Lee el requirements UNA vez, lo valida y lo normaliza, y deja la copia privada que leerá pip."""
        config = self.config

        async def run(ctx: StepContext) -> None:
            def build() -> int:
                prune_private_copies(private_dir.parent)
                normalized = normalize_requirements(source, config)
                write_private_copy(normalized, private_dir)
                return len(normalized.lines) + sum(len(c) for c in normalized.constraints)

            count = await asyncio.to_thread(build)
            ctx.log(f"requirements validado ({count} líneas) y copiado a {private_dir}")

        return FuncStep("requirements", run)

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
