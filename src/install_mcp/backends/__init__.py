"""Registro de backends.

Los backends integrados se registran aquí; los externos se cargan desde la
configuración (``plugins = ["paquete.modulo:Clase"]``). Que un backend esté
registrado no basta para usarlo: además debe figurar en ``allowed_backends``.
"""

from __future__ import annotations

import importlib
from typing import Any

from .base import Backend, BackendParams, CommandStep, FuncStep, Plan, ProbeResult, StepContext
from .download import BinaryBackend, DownloadBackend
from .ecosystems import CargoBackend, GoBackend, NpmBackend
from .git import GitBackend
from .jvm import GradleBackend, MavenBackend
from .pip import PipBackend
from .system import AptBackend, BrewBackend, DockerBackend, RunBackend

BUILTIN: dict[str, type[Backend[Any]]] = {
    b.name: b
    for b in (
        PipBackend, GitBackend, DownloadBackend, BinaryBackend, NpmBackend, CargoBackend, GoBackend,
        MavenBackend, GradleBackend, BrewBackend, AptBackend, DockerBackend, RunBackend,
    )
}


def load_registry(plugins: list[str] | None = None) -> dict[str, type[Backend[Any]]]:
    registry = dict(BUILTIN)
    for ref in plugins or []:
        module_name, _, attr = ref.partition(":")
        if not attr:
            raise ValueError(f"plugin {ref!r}: usa el formato 'paquete.modulo:Clase'")
        cls = getattr(importlib.import_module(module_name), attr)
        if not (isinstance(cls, type) and issubclass(cls, Backend)):
            raise ValueError(f"plugin {ref!r} no es una subclase de Backend")
        registry[cls.name] = cls
    return registry


__all__ = [
    "BUILTIN",
    "Backend",
    "BackendParams",
    "CommandStep",
    "FuncStep",
    "Plan",
    "PipBackend",
    "ProbeResult",
    "StepContext",
    "load_registry",
]
