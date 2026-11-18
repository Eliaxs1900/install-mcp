"""Backends de sistema: ``brew``, ``apt``, ``docker`` y ``run``.

* ``brew``: fórmulas y casks de Homebrew (macOS/Linux), sin auto-update ni prompts.
* ``apt``: paquetes Debian/Ubuntu/WSL. **Privilegiado**: el servidor lo rechaza salvo
  ``allow_privileged = true``; si no se ejecuta como root antepone ``sudo -n`` (nunca
  pide contraseña: si hace falta, falla en vez de colgarse).
* ``docker``: descarga imágenes (``docker pull``).
* ``run``: ejecuta un programa de una allowlist configurada (``[backends.run] programs``)
  con argumentos explícitos, para builds y tests largos. Desactivado por defecto.
"""

from __future__ import annotations

import re
import shutil
from typing import Any, Literal

from pydantic import Field

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


class RunParams(LimitParams):
    program: str = Field(..., description="Nombre de un programa de la allowlist [backends.run] programs.")
    args: list[str] = Field(default_factory=list, max_length=200, description="Argumentos, sin shell.")
    cwd: str = Field(..., description="Directorio de trabajo absoluto dentro de las raíces permitidas.")
    env: dict[str, str] = Field(default_factory=dict, description="Variables extra (no PATH, LD_*, DYLD_*...).")
    io_mode: Literal["pipe", "pty", "stdbuf"] | None = Field(
        None, description="pty/stdbuf para programas que acumulan buffer en stdout."
    )
    ok_exit_codes: list[int] = Field(default_factory=lambda: [0], max_length=10)


class RunBackend(Backend[RunParams]):
    name = "run"
    description = (
        "Ejecuta un programa de una allowlist configurada ([backends.run] programs) para builds/tests largos. "
        "Desactivado por defecto."
    )
    Params = RunParams

    def programs(self) -> list[str]:
        return [str(p) for p in self.options.get("programs", [])]

    def availability(self) -> tuple[bool, str]:
        progs = self.programs()
        if not progs:
            return False, "configura [backends.run] programs = [\"mvn\", \"pytest\", ...]"
        return True, "programas permitidos: " + ", ".join(progs)

    def check(self, spec: RunParams) -> None:
        if spec.program not in self.programs():
            raise ValueError(f"programa no permitido: {spec.program!r}; permitidos: {self.programs()}")
        if not shutil.which(spec.program):
            raise ValueError(f"no se encuentra {spec.program!r} en PATH")
        for a in spec.args:
            if len(a) > 4096 or "\x00" in a:
                raise ValueError("argumento demasiado largo o con NUL")
        allowed = _ENV_ALLOWED | {str(v) for v in self.options.get("env_allow", [])}
        for k in spec.env:
            if not _ENV_KEY.match(k) or _ENV_FORBIDDEN.search(k) or k not in allowed:
                raise ValueError(
                    f"variable de entorno no permitida: {k!r}; permitidas: {sorted(allowed)} "
                    "(amplíalas con [backends.run] env_allow)"
                )
        existing_dir(spec.cwd, self.config, what="cwd")

    def plan(self, spec: RunParams) -> Plan:
        cwd = existing_dir(spec.cwd, self.config, what="cwd")
        return Plan(
            summary=f"{spec.program} {' '.join(spec.args)[:80]} (en {cwd})",
            steps=[
                CommandStep(
                    spec.program, [spec.program, *spec.args], env=dict(spec.env), cwd=str(cwd),
                    io_mode=spec.io_mode, ok_exit_codes=tuple(spec.ok_exit_codes),
                )
            ],
            lock_key=None,
            secrets=[v for v in spec.env.values() if len(v) >= 6],
            **spec.limits(),
        )

    async def detect_versions(self, spec: RunParams, probe: Probe) -> dict[str, str]:
        res = await probe([spec.program, "--version"], timeout_s=15)
        first = (res.stdout or res.stderr).strip().splitlines()
        return {spec.program: first[0][:100]} if res.exit_code == 0 and first else {}

    def result_info(self, spec: RunParams) -> dict[str, Any]:
        return {"cwd": spec.cwd, "argv": [spec.program, *spec.args]}


__all__ = ["AptBackend", "BrewBackend", "DockerBackend", "RunBackend"]
