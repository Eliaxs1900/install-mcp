"""Plantilla de backend: copia este fichero para añadir un instalador nuevo.

Esqueleto mínimo (NO registrado). Los backends reales de este paquete (git.py, pip.py,
download.py, ecosystems.py, jvm.py, system.py) sirven de ejemplos completos.
Ver docs/design.md, "Cómo añadir un backend".

Para activarlo sin tocar el paquete:

    # config.toml
    plugins = ["mi_paquete.mi_backend:MiBackend"]
    allowed_backends = ["pip", "mi-backend"]
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from .base import Backend, CommandStep, Plan, Probe
from .common import LimitParams, check_all, named_or_absolute_dir

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class MiParams(LimitParams):  # LimitParams aporta job_timeout_s y stall_timeout_s
    packages: list[str] = Field(..., min_length=1, description="Qué instalar.")
    dest: str = Field("default", description="Nombre (<state_dir>/mi/<nombre>) o ruta absoluta.")


class MiBackend(Backend[MiParams]):
    name = "mi-backend"
    description = "Describe en una línea qué instala."
    Params = MiParams
    executables = ("mi-herramienta",)       # comprobados por availability()
    default_stall_timeout_s = 300.0          # si la herramienta pasa tiempo callada, súbelo
    default_io_mode = None                   # "pty" si es un programa C que bufferiza stdout

    def check(self, spec: MiParams) -> None:
        check_all(spec.packages, _NAME, what="paquete")   # regex estricta, nada que empiece por "-"
        self.dest(spec)                                   # valida la ruta contra allowed_roots

    def dest(self, spec: MiParams):  # noqa: ANN201
        return named_or_absolute_dir(spec.dest, self.config.state_dir / "mi", self.config.roots(), what="dest")

    def plan(self, spec: MiParams) -> Plan:
        d = self.dest(spec)
        return Plan(
            summary=f"mi-herramienta install {' '.join(spec.packages)} en {d}",
            steps=[CommandStep("install", ["mi-herramienta", "install", "--into", str(d), "--", *spec.packages],
                               env={"MI_NO_PROMPT": "1"})],
            lock_key=f"path:{d}",
            **spec.limits(),
        )

    async def detect_versions(self, spec: MiParams, probe: Probe) -> dict[str, str]:
        res = await probe(["mi-herramienta", "list", "--into", str(self.dest(spec))], timeout_s=30)
        return dict(line.split(maxsplit=1) for line in res.stdout.splitlines() if " " in line)

    def result_info(self, spec: MiParams) -> dict[str, Any]:
        return {"dest": str(self.dest(spec))}
