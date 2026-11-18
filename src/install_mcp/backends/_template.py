"""Plantilla de backend: copia este fichero para añadir un instalador nuevo.

Ejemplo ilustrativo (NO registrado): ``git`` clone/pull. Muestra las piezas que
el núcleo espera. Ver docs/design.md, sección "Cómo añadir un backend".

Para activarlo sin tocar el paquete:

    # config.toml
    plugins = ["mi_paquete.git_backend:GitBackend"]
    allowed_backends = ["pip", "git"]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import Field

from ..security import ensure_within_roots, require_https, url_secrets
from .base import Backend, BackendParams, CommandStep, Plan, Probe

_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class GitParams(BackendParams):
    url: str = Field(..., description="Repositorio https://")
    dest: str = Field(..., description="Directorio destino absoluto dentro de las raíces permitidas")
    ref: str | None = Field(None, description="Rama, tag o commit")
    depth: int | None = Field(1, ge=1)


class GitBackend(Backend[GitParams]):
    name = "git"
    description = "Clona o actualiza un repositorio git (solo https)."
    Params = GitParams
    executables = ("git",)

    def check(self, spec: GitParams) -> None:
        require_https(spec.url, what="url")
        ensure_within_roots(spec.dest, self.config.roots(), what="dest")
        if spec.ref and (spec.ref.startswith("-") or not _REF.match(spec.ref)):
            raise ValueError(f"ref no válida: {spec.ref!r}")

    def requires_privileges(self, spec: GitParams) -> bool:
        return False  # apt-get devolvería True

    def plan(self, spec: GitParams) -> Plan:
        dest = ensure_within_roots(spec.dest, self.config.roots(), what="dest")
        env = {"GIT_TERMINAL_PROMPT": "0"}  # nunca pedir credenciales por terminal
        exists = (dest / ".git").exists()
        if exists:
            steps = [CommandStep("pull", ["git", "-C", str(dest), "pull", "--ff-only", "--progress"], env=env)]
        else:
            clone = ["git", "clone", "--progress"]
            if spec.depth:
                clone += ["--depth", str(spec.depth)]
            if spec.ref:
                clone += ["--branch", spec.ref]
            steps = [CommandStep("clone", [*clone, "--", spec.url, str(dest)], env=env)]
        return Plan(
            summary=f"git {'pull' if exists else 'clone'} {spec.url} -> {dest}",
            steps=steps,
            lock_key=f"path:{dest}",
            secrets=url_secrets(spec.url),
        )

    async def detect_versions(self, spec: GitParams, probe: Probe) -> dict[str, str]:
        res = await probe(["git", "-C", spec.dest, "rev-parse", "--short", "HEAD"])
        return {Path(spec.dest).name: res.stdout.strip()} if res.exit_code == 0 else {}

    def result_info(self, spec: GitParams) -> dict[str, Any]:
        return {"path": spec.dest}
