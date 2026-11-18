"""Backends de ecosistemas de lenguajes: ``npm``, ``cargo`` y ``go``.

Todos instalan en un directorio propio (prefijo/raíz/GOBIN) bajo ``<state_dir>`` o en una
ruta absoluta dentro de las raíces permitidas; nunca en ubicaciones globales del sistema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .base import Backend, CommandStep, Plan, Probe
from .common import LimitParams, check_all, existing_dir, named_or_absolute_dir

# --------------------------------------------------------------------------- npm
_NPM_SPEC = re.compile(
    r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*(@[A-Za-z0-9.^~<>=*|+_-]{1,64})?$"
)
NPM_ENV = {
    "npm_config_update_notifier": "false",
    "npm_config_fund": "false",
    "npm_config_audit": "false",
    "npm_config_progress": "false",
    "npm_config_color": "false",
    "npm_config_yes": "true",
    "NO_UPDATE_NOTIFIER": "1",
    "CI": "1",
}


class NpmParams(LimitParams):
    packages: list[str] = Field(
        default_factory=list, max_length=100,
        description="Paquetes npm ('typescript', '@scope/pkg@^2'). Se instalan en 'prefix'.",
    )
    prefix: str = Field("default", description="Nombre (<state_dir>/npm/<nombre>) o ruta absoluta.")
    project_dir: str | None = Field(
        None, description="Instalar las dependencias de un proyecto existente (npm ci / pnpm / yarn)."
    )
    manager: Literal["auto", "npm", "pnpm", "yarn"] = Field("auto", description="Solo en modo proyecto.")
    ignore_scripts: bool = Field(False, description="No ejecutar scripts de ciclo de vida (más seguro).")


class NpmBackend(Backend[NpmParams]):
    name = "npm"
    description = "Paquetes npm en un prefijo aislado, o dependencias de un proyecto (npm ci / pnpm / yarn)."
    Params = NpmParams
    executables = ("npm",)

    def check(self, spec: NpmParams) -> None:
        if bool(spec.packages) == bool(spec.project_dir):
            raise ValueError("indica 'packages' (instalar en un prefijo) o 'project_dir' (dependencias del proyecto)")
        check_all(spec.packages, _NPM_SPEC, what="paquete npm")
        if spec.project_dir:
            d = existing_dir(spec.project_dir, self.config.roots(), what="project_dir")
            if not (d / "package.json").exists():
                raise ValueError(f"{d} no tiene package.json")
        else:
            self.prefix(spec)

    def prefix(self, spec: NpmParams) -> Path:
        return named_or_absolute_dir(spec.prefix, self.config.state_dir / "npm", self.config.roots(), what="prefix")

    def _manager(self, d: Path, choice: str) -> str:
        if choice != "auto":
            return choice
        if (d / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (d / "yarn.lock").exists():
            return "yarn"
        return "npm"

    def plan(self, spec: NpmParams) -> Plan:
        if spec.project_dir:
            d = existing_dir(spec.project_dir, self.config.roots(), what="project_dir")
            mgr = self._manager(d, spec.manager)
            if mgr == "npm":
                argv = ["npm", "ci" if (d / "package-lock.json").exists() else "install", "--no-fund", "--no-audit"]
            elif mgr == "pnpm":
                argv = ["pnpm", "install", "--frozen-lockfile", "--reporter=append-only"]
            else:
                argv = ["yarn", "install", "--frozen-lockfile", "--non-interactive"]
            if spec.ignore_scripts:
                argv.append("--ignore-scripts")
            return Plan(
                summary=f"{mgr}: dependencias de {d}",
                steps=[CommandStep("install", argv, env=NPM_ENV, cwd=str(d))],
                lock_key=f"path:{d}/node_modules",
                **spec.limits(),
            )
        prefix = self.prefix(spec)
        argv = ["npm", "install", "--prefix", str(prefix), "--no-fund", "--no-audit", "--no-save"]
        if spec.ignore_scripts:
            argv.append("--ignore-scripts")
        return Plan(
            summary=f"npm install {', '.join(spec.packages)} en {prefix}",
            steps=[CommandStep("install", [*argv, "--", *spec.packages], env=NPM_ENV)],
            lock_key=f"path:{prefix}",
            **spec.limits(),
        )

    async def detect_versions(self, spec: NpmParams, probe: Probe) -> dict[str, str]:
        if spec.project_dir:
            d = existing_dir(spec.project_dir, self.config.roots(), what="project_dir")
            res = await probe(["npm", "ls", "--json", "--depth=0"], timeout_s=60, cwd=str(d))
        else:
            res = await probe(["npm", "ls", "--json", "--depth=0", "--prefix", str(self.prefix(spec))], timeout_s=60)
        try:
            deps = json.loads(res.stdout or "{}").get("dependencies", {})
        except ValueError:
            return {}
        wanted = {re.sub(r"(?<!^)@.*$", "", p) for p in spec.packages}
        return {k: v.get("version", "?") for k, v in deps.items() if not wanted or k in wanted}

    def result_info(self, spec: NpmParams) -> dict[str, Any]:
        if spec.project_dir:
            return {"project_dir": spec.project_dir}
        prefix = self.prefix(spec)
        return {"prefix": str(prefix), "bin": str(prefix / "node_modules" / ".bin")}


# --------------------------------------------------------------------------- cargo
_CRATE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}(@[A-Za-z0-9.^~<>=*+-]{1,40})?$")
CARGO_ENV = {"CARGO_TERM_COLOR": "never", "CARGO_TERM_PROGRESS_WHEN": "never"}


class CargoParams(LimitParams):
    crates: list[str] = Field(..., min_length=1, max_length=20, description="'ripgrep' o 'ripgrep@14.1.0'.")
    root: str = Field("default", description="Nombre (<state_dir>/cargo/<nombre>) o ruta absoluta; binarios en root/bin.")
    locked: bool = Field(True, description="Usar Cargo.lock del crate (--locked).")


class CargoBackend(Backend[CargoParams]):
    name = "cargo"
    description = "Herramientas Rust con cargo install en una raíz aislada (root/bin)."
    Params = CargoParams
    executables = ("cargo",)
    default_stall_timeout_s = 900.0  # el enlazado final de un binario grande puede ir callado varios minutos

    def check(self, spec: CargoParams) -> None:
        check_all(spec.crates, _CRATE, what="crate")
        self.root(spec)

    def root(self, spec: CargoParams) -> Path:
        return named_or_absolute_dir(spec.root, self.config.state_dir / "cargo", self.config.roots(), what="root")

    def plan(self, spec: CargoParams) -> Plan:
        root = self.root(spec)
        base = ["cargo", "install", "--root", str(root)] + (["--locked"] if spec.locked else [])
        steps = [CommandStep(f"install {c}", [*base, "--", c], env=CARGO_ENV) for c in spec.crates]
        return Plan(
            summary=f"cargo install {', '.join(spec.crates)} en {root}",
            steps=steps,
            lock_key=f"path:{root}",
            **spec.limits(),
        )

    async def detect_versions(self, spec: CargoParams, probe: Probe) -> dict[str, str]:
        res = await probe(["cargo", "install", "--list", "--root", str(self.root(spec))], timeout_s=30)
        found = dict(re.findall(r"^([A-Za-z0-9_-]+) v([^\s:]+)", res.stdout, re.M))
        wanted = [c.split("@")[0] for c in spec.crates]
        return {c: found.get(c, "NO ENCONTRADO") for c in wanted}

    def result_info(self, spec: CargoParams) -> dict[str, Any]:
        return {"root": str(self.root(spec)), "bin": str(self.root(spec) / "bin")}


# --------------------------------------------------------------------------- go
_GO_PKG = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(/[A-Za-z0-9._~-]+)+@(latest|v[0-9][A-Za-z0-9.+-]*|[0-9a-f]{7,40})$")


class GoParams(LimitParams):
    packages: list[str] = Field(
        ..., min_length=1, max_length=20, description="'golang.org/x/tools/gopls@latest' (versión obligatoria)."
    )
    gobin: str = Field("default", description="Nombre (<state_dir>/go/<nombre>/bin) o ruta absoluta para los binarios.")


class GoBackend(Backend[GoParams]):
    name = "go"
    description = "Herramientas Go con go install (paquete@versión) en un GOBIN aislado."
    Params = GoParams
    executables = ("go",)

    def check(self, spec: GoParams) -> None:
        check_all(spec.packages, _GO_PKG, what="paquete go (ruta@versión)")
        self.gobin(spec)

    def gobin(self, spec: GoParams) -> Path:
        d = named_or_absolute_dir(spec.gobin, self.config.state_dir / "go", self.config.roots(), what="gobin")
        return d / "bin" if spec.gobin == "default" or not Path(spec.gobin).is_absolute() else d

    def plan(self, spec: GoParams) -> Plan:
        gobin = self.gobin(spec)
        env = {"GOBIN": str(gobin), "GOFLAGS": "-modcacherw", "GOTOOLCHAIN": "auto"}
        return Plan(
            summary=f"go install {', '.join(spec.packages)} en {gobin}",
            steps=[CommandStep("install", ["go", "install", "-v", "--", *spec.packages], env=env)],
            lock_key=f"path:{gobin}",
            **spec.limits(),
        )

    async def detect_versions(self, spec: GoParams, probe: Probe) -> dict[str, str]:
        res = await probe(["go", "version", "-m", str(self.gobin(spec))], timeout_s=30)
        mods = dict(re.findall(r"^\s+mod\s+(\S+)\s+(\S+)", res.stdout, re.M))
        out = {}
        for p in spec.packages:
            path = p.split("@")[0]
            ver = next((v for m, v in mods.items() if path == m or path.startswith(m + "/")), None)
            out[path] = ver or "NO ENCONTRADO"
        return out

    def result_info(self, spec: GoParams) -> dict[str, Any]:
        return {"gobin": str(self.gobin(spec))}
