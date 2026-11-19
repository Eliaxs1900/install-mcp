"""Backends de sistema: ``brew``, ``apt``, ``docker`` y ``run``.

* ``brew``: fórmulas y casks de Homebrew (macOS/Linux), sin auto-update ni prompts.
* ``apt``: paquetes Debian/Ubuntu/WSL. **Privilegiado**: el servidor lo rechaza salvo
  ``allow_privileged = true``; si no se ejecuta como root antepone ``sudo -n`` (nunca
  pide contraseña: si hace falta, falla en vez de colgarse).
* ``docker``: descarga imágenes (``docker pull``).
* ``run``: ejecuta un programa de una allowlist configurada (``[backends.run] programs``,
  o reglas ``commands`` con prefijo de argumentos y carpeta de trabajo) con argumentos
  explícitos, para builds, tests y scripts largos. Desactivado por defecto.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field

from ..envsource import check_env_names
from ..process import SERVER_ONLY_ENV, is_server_only
from .base import Backend, CommandStep, Plan, Probe
from .common import LimitParams, check_all, existing_dir, is_root

# --------------------------------------------------------------------------- brew
_BREW = re.compile(r"^[a-z0-9][a-z0-9@+._-]*(/[a-z0-9][a-z0-9@+._-]*){0,2}$")
BREW_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    "HOMEBREW_NO_ENV_HINTS": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
    "HOMEBREW_NO_COLOR": "1",
    "HOMEBREW_COLOR": "0",
    "NONINTERACTIVE": "1",
}


class BrewParams(LimitParams):
    formulae: list[str] = Field(default_factory=list, max_length=50, description="p. ej. ['binwalk', 'p7zip'].")
    casks: list[str] = Field(default_factory=list, max_length=20, description="Aplicaciones (macOS).")
    upgrade: bool = Field(False, description="Actualizar si ya están instaladas.")


class BrewBackend(Backend[BrewParams]):
    name = "brew"
    description = "Homebrew: fórmulas y casks, sin auto-update ni prompts."
    Params = BrewParams
    executables = ("brew",)
    default_stall_timeout_s = 600.0

    def check(self, spec: BrewParams) -> None:
        if not spec.formulae and not spec.casks:
            raise ValueError("indica 'formulae' o 'casks'")
        check_all([*spec.formulae, *spec.casks], _BREW, what="fórmula/cask")

    def availability(self) -> tuple[bool, str]:
        if is_root():
            return False, "Homebrew no se puede usar como root"
        return super().availability()

    def plan(self, spec: BrewParams) -> Plan:
        verb = "upgrade" if spec.upgrade else "install"
        steps = []
        if spec.formulae:
            steps.append(CommandStep("formulae", ["brew", verb, "--formula", "--", *spec.formulae], env=BREW_ENV))
        if spec.casks:
            steps.append(CommandStep("casks", ["brew", verb, "--cask", "--", *spec.casks], env=BREW_ENV))
        return Plan(
            summary=f"brew {verb} {' '.join([*spec.formulae, *spec.casks])}",
            steps=steps, lock_key="brew", **spec.limits(),
        )

    async def detect_versions(self, spec: BrewParams, probe: Probe) -> dict[str, str]:
        out: dict[str, str] = {}
        for kind, names in (("--formula", spec.formulae), ("--cask", spec.casks)):
            if not names:
                continue
            res = await probe(["brew", "list", "--versions", kind, *names], env=BREW_ENV, timeout_s=60)
            for line in res.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    out[parts[0]] = " ".join(parts[1:])
            out.update({n.split("/")[-1]: "NO ENCONTRADO" for n in names if n.split("/")[-1] not in out})
        return out


# --------------------------------------------------------------------------- apt
_APT = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}(=[A-Za-z0-9.:~+-]{1,100})?$")
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none", "NEEDRESTART_MODE": "a"}
APT_OPTS = ["-y", "-q", "-o", "Dpkg::Progress-Fancy=0", "-o", "APT::Color=0", "-o", "Dpkg::Use-Pty=0",
            "-o", "DPkg::Lock::Timeout=120"]


class AptParams(LimitParams):
    packages: list[str] = Field(..., min_length=1, max_length=100, description="p. ej. ['binwalk', 'squashfs-tools'].")
    update: bool = Field(True, description="apt-get update antes de instalar.")
    install_recommends: bool = False


class AptBackend(Backend[AptParams]):
    name = "apt"
    description = "apt-get (Debian/Ubuntu/WSL). Privilegiado: requiere allow_privileged = true."
    Params = AptParams
    executables = ("apt-get", "dpkg-query")
    default_stall_timeout_s = 600.0  # dpkg puede pasar minutos callado con triggers

    @classmethod
    def enable_snippet(cls) -> str:
        return "allow_privileged = true   # apt-get con `sudo -n`: nunca pide contraseña (falla si hace falta)"

    def check(self, spec: AptParams) -> None:
        check_all(spec.packages, _APT, what="paquete apt")

    def requires_privileges(self, spec: AptParams) -> bool:
        return True

    def plan(self, spec: AptParams) -> Plan:
        steps = []
        if spec.update:
            steps.append(CommandStep("update", ["apt-get", *APT_OPTS, "update"], env=APT_ENV, privileged=True))
        install = ["apt-get", *APT_OPTS, "install"]
        if not spec.install_recommends:
            install.append("--no-install-recommends")
        steps.append(CommandStep("install", [*install, *spec.packages], env=APT_ENV, privileged=True))
        return Plan(summary=f"apt-get install {' '.join(spec.packages)}", steps=steps, lock_key="apt",
                    **spec.limits())

    async def detect_versions(self, spec: AptParams, probe: Probe) -> dict[str, str]:
        names = [p.split("=")[0] for p in spec.packages]
        res = await probe(["dpkg-query", "-W", "-f", "${Package}\t${Version}\n", *names], timeout_s=30)
        found = dict(line.split("\t", 1) for line in res.stdout.splitlines() if "\t" in line)
        return {n: found.get(n, "NO ENCONTRADO") for n in names}


# --------------------------------------------------------------------------- docker
_IMAGE = re.compile(
    r"^([a-zA-Z0-9.-]+(:[0-9]{1,5})?/)?[a-z0-9]+([._-]{1,2}[a-z0-9]+)*(/[a-z0-9]+([._-]{1,2}[a-z0-9]+)*)*"
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(@sha256:[a-f0-9]{64})?$"
)


class DockerParams(LimitParams):
    images: list[str] = Field(..., min_length=1, max_length=20, description="p. ej. ['python:3.12-slim'].")
    platform: str | None = Field(None, description="p. ej. 'linux/amd64'.")


class DockerBackend(Backend[DockerParams]):
    name = "docker"
    description = "Descarga imágenes de contenedor (docker pull)."
    Params = DockerParams
    executables = ("docker",)

    def check(self, spec: DockerParams) -> None:
        check_all(spec.images, _IMAGE, what="imagen")
        if spec.platform and not re.fullmatch(r"[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?", spec.platform):
            raise ValueError(f"platform no válida: {spec.platform!r}")

    def plan(self, spec: DockerParams) -> Plan:
        extra = ["--platform", spec.platform] if spec.platform else []
        steps = [CommandStep(f"pull {i}", ["docker", "pull", *extra, "--", i]) for i in spec.images]
        return Plan(summary=f"docker pull {' '.join(spec.images)}", steps=steps, lock_key="docker", **spec.limits())

    async def detect_versions(self, spec: DockerParams, probe: Probe) -> dict[str, str]:
        out = {}
        for i in spec.images:
            res = await probe(["docker", "image", "inspect", "--format", "{{.Id}}", "--", i], timeout_s=30)
            out[i] = res.stdout.strip()[:19] if res.exit_code == 0 else "NO ENCONTRADA"
        return out


# --------------------------------------------------------------------------- run
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_ENV_FORBIDDEN = re.compile(r"^(LD_|DYLD_)|^(PATH|PYTHONPATH|PYTHONSTARTUP|BASH_ENV|ENV|IFS)$")
# Lista de PERMITIDAS: una lista de prohibidas siempre se queda corta (NODE_OPTIONS, JAVA_TOOL_OPTIONS,
# MAVEN_OPTS, PYTEST_ADDOPTS, GIT_CONFIG_*, PERL5OPT, HOME... inyectan código en programas
# "permitidos"). Se amplía con [backends.run] env_allow = ["MI_VAR", ...].
_ENV_ALLOWED = frozenset({
    "CI", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "NO_COLOR", "FORCE_COLOR",
    "COLUMNS", "LINES", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
})

RUN_ENABLE_SNIPPET = """\
[backends.run]
# Programas permitidos con cualquier argumento y en cualquier cwd dentro de allowed_roots:
programs = ["pytest", "make"]
# Y/o reglas más estrechas: programa + prefijo de argumentos + carpeta de trabajo.
# Un elemento de args_prefix que acaba en "/" casa con cualquier argumento que empiece así (sin "..").
commands = [
  { program = "uv", args_prefix = ["run", "python", "tools/"], cwd_under = "~/proyecto" },
]
# Variables que se copian por nombre (del entorno del servidor o de la shell de login); se redactan.
# inherit_env = ["GEMINI_API_KEY"]"""


@dataclass(frozen=True)
class CommandRule:
    """Regla ``[[backends.run.commands]]``: qué programa, con qué argumentos iniciales y dónde."""

    index: int
    program: str
    args_prefix: tuple[str, ...] = ()
    cwd_under: Path | None = None
    inherit_env: tuple[str, ...] = ()

    @classmethod
    def parse(cls, index: int, raw: Any) -> CommandRule:
        where = f"[backends.run] commands[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: debe ser una tabla {{program = ..., args_prefix = [...], cwd_under = ...}}")
        unknown = set(raw) - {"program", "args_prefix", "cwd_under", "inherit_env"}
        if unknown:
            raise ValueError(f"{where}: claves desconocidas {sorted(unknown)}")
        program = raw.get("program")
        if not isinstance(program, str) or not program or "/" in program:
            raise ValueError(f"{where}: 'program' debe ser el nombre de un programa en PATH")
        prefix = raw.get("args_prefix", [])
        if not isinstance(prefix, list) or not all(isinstance(a, str) and a for a in prefix):
            raise ValueError(f"{where}: 'args_prefix' debe ser una lista de textos no vacíos")
        cwd_under = raw.get("cwd_under")
        if cwd_under is not None:
            if not isinstance(cwd_under, str) or not Path(cwd_under).expanduser().is_absolute():
                raise ValueError(f"{where}: 'cwd_under' debe ser una ruta absoluta (o con ~)")
            cwd_under = Path(cwd_under).expanduser().resolve()
        inherit = check_env_names(raw.get("inherit_env", []), what=f"{where} inherit_env")
        return cls(index, program, tuple(prefix), cwd_under, tuple(inherit))

    def describe(self) -> str:
        text = " ".join([self.program, *self.args_prefix])
        if self.cwd_under is not None:
            text += f" (cwd bajo {self.cwd_under})"
        return f"commands[{self.index}] = {text}"

    def mismatch(self, program: str, args: list[str], cwd: Path) -> str | None:
        """None si la petición casa; si no, por qué no."""
        if program != self.program:
            return f"el programa es {program!r}, no {self.program!r}"
        if len(args) < len(self.args_prefix):
            return f"faltan argumentos: se esperaban al menos {list(self.args_prefix)}"
        for i, (want, got) in enumerate(zip(self.args_prefix, args, strict=False)):
            if want.endswith("/"):
                if not got.startswith(want) or ".." in PurePosixPath(got).parts:
                    return f"args[{i}] = {got!r} no está bajo {want!r}"
            elif got != want:
                return f"args[{i}] = {got!r}, se esperaba {want!r}"
        if self.cwd_under is not None and not (cwd == self.cwd_under or cwd.is_relative_to(self.cwd_under)):
            return f"cwd {str(cwd)!r} no está bajo {str(self.cwd_under)!r}"
        return None


class RunParams(LimitParams):
    program: str = Field(
        ..., description="Programa de [backends.run] programs, o el de una regla [backends.run] commands."
    )
    args: list[str] = Field(default_factory=list, max_length=200, description="Argumentos, sin shell.")
    cwd: str = Field(..., description="Directorio de trabajo absoluto dentro de las raíces permitidas.")
    env: dict[str, str] = Field(default_factory=dict, description="Variables extra (no PATH, LD_*, DYLD_*...).")
    env_from: list[str] = Field(
        default_factory=list, max_length=20,
        description=(
            "Nombres de variables a copiar del entorno del servidor MCP (p. ej. ['GEMINI_API_KEY']); el "
            "valor nunca va en los params y se redacta en logs y estado. Para leerlas de la shell de login, "
            "lístalas en inherit_env en la configuración."
        ),
    )
    io_mode: Literal["pipe", "pty", "stdbuf"] | None = Field(
        None, description="pty/stdbuf para programas que acumulan buffer en stdout."
    )
    ok_exit_codes: list[int] = Field(default_factory=lambda: [0], max_length=10)


class RunBackend(Backend[RunParams]):
    name = "run"
    description = (
        "Ejecuta un programa de una allowlist configurada ([backends.run] programs, o reglas commands con "
        "prefijo de argumentos y cwd) para builds/tests/scripts largos. Desactivado por defecto."
    )
    Params = RunParams

    @classmethod
    def enable_snippet(cls) -> str:
        return RUN_ENABLE_SNIPPET

    def setup_hint(self) -> str | None:
        return None if self.availability()[0] else RUN_ENABLE_SNIPPET

    def programs(self) -> list[str]:
        return [str(p) for p in self.options.get("programs", [])]

    def rules(self) -> list[CommandRule]:
        raw = self.options.get("commands", [])
        if not isinstance(raw, list):
            raise ValueError("[backends.run] commands debe ser una lista de tablas")
        return [CommandRule.parse(i, r) for i, r in enumerate(raw)]

    def inherit_env(self) -> list[str]:
        return check_env_names(self.options.get("inherit_env", []), what="[backends.run] inherit_env")

    def availability(self) -> tuple[bool, str]:
        try:
            rules = self.rules()
            self.inherit_env()
        except ValueError as exc:
            return False, f"configuración no válida: {exc}"
        progs = self.programs()
        if not progs and not rules:
            return False, "configura [backends.run] programs = [\"pytest\", ...] o commands = [{program = ...}]"
        parts = []
        if progs:
            parts.append("programas permitidos: " + ", ".join(progs))
        if rules:
            parts.append("reglas: " + "; ".join(r.describe() for r in rules))
        return True, " | ".join(parts)

    def match(self, spec: RunParams, cwd: Path) -> CommandRule | None:
        """La regla que permite la petición (None si basta con ``programs``). Si nada la permite,
        ValueError con el motivo de cada regla."""
        if spec.program in self.programs():
            return None
        rules = self.rules()
        reasons = []
        for rule in rules:
            why = rule.mismatch(spec.program, spec.args, cwd)
            if why is None:
                return rule
            reasons.append(f"{rule.describe()}: {why}")
        msg = f"programa no permitido: {spec.program!r}; [backends.run] programs = {self.programs()}"
        if reasons:
            msg += ". Ninguna regla casa — " + " | ".join(reasons)
        raise ValueError(msg)

    def check(self, spec: RunParams) -> None:
        for a in spec.args:
            if len(a) > 4096 or "\x00" in a:
                raise ValueError("argumento demasiado largo o con NUL")
        cwd = existing_dir(spec.cwd, self.config, what="cwd")
        self.match(spec, cwd)
        if not shutil.which(spec.program):
            raise ValueError(f"no se encuentra {spec.program!r} en PATH")
        allowed = _ENV_ALLOWED | {str(v) for v in self.options.get("env_allow", [])}
        for k in spec.env:
            if not _ENV_KEY.match(k) or _ENV_FORBIDDEN.search(k) or k not in allowed:
                raise ValueError(
                    f"variable de entorno no permitida: {k!r}; permitidas: {sorted(allowed)} "
                    "(amplíalas con [backends.run] env_allow)"
                )
        check_env_names(spec.env_from, what="env_from")
        for k in spec.env:  # un valor puesto a propósito para esta petición no es del servidor: solo la lista
            if k.upper() in SERVER_ONLY_ENV:
                raise ValueError(
                    f"env: {k!r} es una credencial del servidor MCP que los jobs no heredan; no se acepta como variable"
                )
        for k in spec.env_from:
            if is_server_only(k):
                raise ValueError(
                    f"env_from: {k!r} es una credencial del servidor MCP (la app o Anthropic) que los jobs no heredan; "
                    "solo se puede dar a propósito con inherit_env en la configuración"
                )

    def plan(self, spec: RunParams) -> Plan:
        cwd = existing_dir(spec.cwd, self.config, what="cwd")
        rule = self.match(spec, cwd)
        # [backends.run] inherit_env (y el global) los añade el núcleo; aquí, los de la regla.
        inherit = list(rule.inherit_env) if rule else []
        summary = f"{spec.program} {' '.join(spec.args)[:80]} (en {cwd})"
        if rule is not None:
            summary += f" [regla commands[{rule.index}]]"
        return Plan(
            summary=summary,
            steps=[
                CommandStep(
                    spec.program, [spec.program, *spec.args], env=dict(spec.env), cwd=str(cwd),
                    io_mode=spec.io_mode, ok_exit_codes=tuple(spec.ok_exit_codes),
                )
            ],
            lock_key=None,
            secrets=[v for v in spec.env.values() if len(v) >= 6],
            inherit_env=inherit,
            env_from=list(spec.env_from),
            **spec.limits(),
        )

    async def detect_versions(self, spec: RunParams, probe: Probe) -> dict[str, str]:
        res = await probe([spec.program, "--version"], timeout_s=15)
        first = (res.stdout or res.stderr).strip().splitlines()
        return {spec.program: first[0][:100]} if res.exit_code == 0 and first else {}

    def result_info(self, spec: RunParams) -> dict[str, Any]:
        return {"cwd": spec.cwd, "argv": [spec.program, *spec.args]}


__all__ = ["AptBackend", "BrewBackend", "DockerBackend", "RunBackend"]
