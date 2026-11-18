"""Piezas compartidas por los backends: límites por petición, directorios y validadores."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import Field

from ..security import ensure_within_roots
from .base import BackendParams

NAME_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LimitParams(BackendParams):
    """Parámetros de límites que exponen todos los backends integrados."""

    job_timeout_s: float | None = Field(
        None, ge=0, description="Tiempo máximo del job; al vencer se mata y queda timed_out (0 = sin límite)."
    )
    stall_timeout_s: float | None = Field(
        None, ge=0, description="Sin salida durante N s -> estado stalled + aviso (0 = sin watchdog)."
    )

    def limits(self) -> dict[str, Any]:
        return {"timeout_s": self.job_timeout_s, "stall_timeout_s": self.stall_timeout_s}


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def named_or_absolute_dir(value: str, base: Path, roots: Iterable[Path], *, what: str) -> Path:
    """``"nombre"`` -> ``base/nombre``; ruta absoluta -> debe caer dentro de las raíces permitidas."""
    if NAME_DIR.match(value):
        return (base / value).expanduser().resolve()
    return ensure_within_roots(value, roots, what=what)


def existing_dir(value: str, roots: Iterable[Path], *, what: str) -> Path:
    p = ensure_within_roots(value, roots, what=what)
    if not p.is_dir():
        raise ValueError(f"{what} no existe o no es un directorio: {str(p)!r}")
    return p


def check_all(values: Iterable[str], pattern: re.Pattern[str], *, what: str) -> None:
    for v in values:
        if not isinstance(v, str) or v.startswith("-") or not pattern.match(v):
            raise ValueError(f"{what} no válido: {v!r}")


def tool_path(name: str, options: dict[str, Any]) -> str | None:
    """Ruta de un ejecutable: ``[backends.<n>] <name>_path`` o PATH."""
    configured = options.get(f"{name}_path")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which(name)
