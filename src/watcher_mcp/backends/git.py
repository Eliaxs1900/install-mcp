"""Backend ``git``: clona o actualiza un repositorio sin poder quedarse esperando una contraseña.

* Orígenes admitidos: ``https://``, ``ssh://``, ``git@host:ruta`` y rutas locales absolutas.
  Se rechazan transportes como ``ext::`` y cualquier valor que empiece por ``-``.
* ``GIT_TERMINAL_PROMPT=0`` y ``ssh -o BatchMode=yes``: si hacen falta credenciales, falla
  enseguida en vez de colgarse esperando una entrada que nunca llegará.
* ``--progress`` para que git escriba avance aunque su salida no sea un terminal.
* La configuración local de un repo puede ejecutar código (``core.fsmonitor``, hooks,
  ``credential.helper``, filtros): todos los comandos llevan ``-c`` que la neutraliza, y un
  repo existente que watcher-mcp no clonó solo se actualiza con ``trust_existing=true``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import Field

from ..security import url_secrets
from .base import Backend, CommandStep, FuncStep, Plan, Probe, StepContext
from .common import LimitParams

_SCP = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=20",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}
# Neutraliza la configuración del repo que ejecuta programas (hooks, fsmonitor, helpers de credenciales).
HARDENING = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper="]
# Marca que deja watcher-mcp en los clones que hace él: solo esos se actualizan sin más.
MARKER = "watcher-mcp-clone"


class GitParams(LimitParams):
    url: str = Field(..., description="https://…, ssh://…, git@host:org/repo.git o ruta local absoluta.")
    dest: str = Field(..., description="Directorio destino absoluto dentro de las raíces permitidas.")
    ref: str | None = Field(None, description="Rama, tag o commit a dejar activo.")
    depth: int | None = Field(None, ge=1, description="Clon superficial con N commits.")
    submodules: bool = Field(False, description="Incluir submódulos (recursivo).")
    update: bool = Field(True, description="Si dest ya es un clon, hacer fetch + fast-forward.")
    trust_existing: bool = Field(
        False,
        description="Permitir actualizar un repo existente que no clonó watcher-mcp (su configuración local "
        "podría ejecutar código: solo si confías en él).",
    )


class GitBackend(Backend[GitParams]):
    name = "git"
    description = "Clona o actualiza un repositorio git (https, ssh o local), sin prompts de credenciales."
    Params = GitParams
    executables = ("git",)

    def check(self, spec: GitParams) -> None:
        u = spec.url
        ok = (
            u.startswith(("https://", "ssh://"))
            or _SCP.match(u)
            or (Path(u).is_absolute() and Path(u).exists())
        )
        if Path(u).is_absolute():
            self.config.safe_path(u, what="url (ruta local)")
        if u.startswith("-") or "::" in u or not ok:
            raise ValueError(f"url de git no admitida: {u!r} (https, ssh, git@host:ruta o ruta local)")
        dest = self.config.safe_path(spec.dest, what="dest")
        if dest.exists() and not (dest / ".git").exists() and any(dest.iterdir()):
            raise ValueError(f"dest existe, no está vacío y no es un repositorio git: {dest}")
        if (dest / ".git").exists() and not (dest / ".git" / MARKER).exists() and not spec.trust_existing:
            raise ValueError(
                f"{dest} es un repositorio que watcher-mcp no clonó: su configuración local podría ejecutar "
                "código al actualizarlo. Pasa trust_existing=true solo si confías en él."
            )
        if spec.ref and (spec.ref.startswith("-") or not _REF.match(spec.ref) or ".." in spec.ref):
            raise ValueError(f"ref no válida: {spec.ref!r}")

    def plan(self, spec: GitParams) -> Plan:
        dest = self.config.safe_path(spec.dest, what="dest")
        is_repo = (dest / ".git").exists()
        git = ["git", "-c", "color.ui=never", *HARDENING]
        steps: list[CommandStep] = []
        if is_repo:
            if not spec.update and not spec.ref:
                raise ValueError(f"{dest} ya es un repositorio y update=false: nada que hacer")
            fetch = [*git, "-C", str(dest), "fetch", "--progress", "--tags", "origin"]
            if spec.depth:
                fetch += ["--depth", str(spec.depth)]
            steps.append(CommandStep("fetch", fetch, env=ENV))
            if spec.ref:
                steps.append(CommandStep("checkout", [*git, "-C", str(dest), "checkout", spec.ref, "--"], env=ENV))
            if spec.update:
                steps.append(
                    CommandStep("merge", [*git, "-C", str(dest), "merge", "--ff-only", "@{upstream}"], env=ENV,
                                skip_if=lambda: _detached(dest))
                )
            action = "actualizar"
        else:
            clone = [*git, "clone", "--progress"]
            if spec.depth:
                clone += ["--depth", str(spec.depth)]
                if spec.ref and not re.fullmatch(r"[0-9a-f]{7,40}", spec.ref):
                    clone += ["--branch", spec.ref]
            steps.append(CommandStep("clone", [*clone, "--", spec.url, str(dest)], env=ENV))
            steps.append(FuncStep("mark", _mark(dest)))
            if spec.ref and (not spec.depth or re.fullmatch(r"[0-9a-f]{7,40}", spec.ref)):
                steps.append(CommandStep("checkout", [*git, "-C", str(dest), "checkout", spec.ref, "--"], env=ENV))
            action = "clonar"
        if spec.submodules:
            steps.append(
                CommandStep(
                    "submodules",
                    [*git, "-C", str(dest), "submodule", "update", "--init", "--recursive", "--progress"],
                    env=ENV,
                )
            )
        return Plan(
            summary=f"git: {action} {_safe(spec.url)} -> {dest}",
            steps=steps,
            lock_key=f"path:{dest}",
            secrets=url_secrets(spec.url) if "://" in spec.url else [],
            **spec.limits(),
        )

    async def detect_versions(self, spec: GitParams, probe: Probe) -> dict[str, str]:
        dest = self.config.safe_path(spec.dest, what="dest")
        res = await probe(["git", "-C", str(dest), "describe", "--always", "--tags", "--dirty"], timeout_s=20)
        head = await probe(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"], timeout_s=20)
        if head.exit_code != 0:
            return {}
        return {dest.name: f"{res.stdout.strip() or head.stdout.strip()} ({head.stdout.strip()})"}

    def result_info(self, spec: GitParams) -> dict[str, Any]:
        return {"path": str(self.config.safe_path(spec.dest, what="dest"))}


def _mark(dest: Path):  # type: ignore[no-untyped-def]
    async def run(ctx: StepContext) -> None:
        (dest / ".git" / MARKER).write_text("clonado por watcher-mcp\n", encoding="utf-8")

    return run


def _detached(dest: Path) -> bool:
    head = dest / ".git" / "HEAD"
    try:
        return not head.read_text().startswith("ref:")
    except OSError:
        return False


def _safe(url: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", url)
