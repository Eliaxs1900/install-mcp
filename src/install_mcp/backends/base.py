"""Interfaz de backend (adaptador por tipo de instalador).

Un backend NO ejecuta nada por sí mismo: traduce unos parámetros validados a un
``Plan`` (lista de pasos con argv explícitos) que el núcleo ejecuta, registra y
notifica. Así toda instalación comparte cola, logs, cancelación, redacción de
secretos y notificaciones.

Lo que declara cada backend:

* ``name`` / ``description``: identificador para la allowlist y la tool ``install``.
* ``Params``: modelo pydantic de parámetros (``extra="forbid"``). Su JSON Schema se
  publica en la tool ``backends`` para que el cliente sepa qué pasar.
* ``validate(params)``: validación semántica adicional (rutas, URLs, specs...).
* ``requires_privileges(spec)``: si algún paso necesita root (apt-get...). El núcleo
  rechaza el job salvo ``allow_privileged = true`` en la configuración.
* ``plan(spec)``: los comandos a ejecutar (``CommandStep``) o pasos internos en
  Python (``FuncStep``, p. ej. verificar un checksum).
* ``detect_versions(spec, probe)``: qué quedó instalado y en qué versión.
* ``result_info(spec)``: datos útiles para el informe (ruta del venv, del clon...).
* ``availability()``: si los ejecutables necesarios están presentes.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..config import Config


class BackendParams(BaseModel):
    """Base de los parámetros de un backend: rechaza claves desconocidas."""

    model_config = ConfigDict(extra="forbid")


P = TypeVar("P", bound=BackendParams)
IoMode = Literal["pipe", "pty", "stdbuf"]


@dataclass
class ProbeResult:
    exit_code: int
    stdout: str
    stderr: str


# probe(argv, env=None, timeout_s=60, cwd=None) -> ProbeResult. Ejecuta un comando corto capturando salida
# (sin shell, con timeout). Lo proporciona el núcleo a ``detect_versions``.
Probe = Callable[..., Awaitable[ProbeResult]]


@dataclass
class StepContext:
    """Lo que recibe un ``FuncStep``: función para escribir en el log del job."""

    # Escribe en el log del job; cuenta como actividad para el watchdog de atasco.
    log: Callable[[str], None]
    cancelled: Callable[[], bool]


@dataclass
class CommandStep:
    name: str
    argv: list[str]
    env: Mapping[str, str] = field(default_factory=dict)
    # Variables a eliminar del entorno heredado (p. ej. VIRTUAL_ENV).
    env_remove: tuple[str, ...] = ()
    cwd: str | None = None
    timeout_s: float | None = None
    ok_exit_codes: tuple[int, ...] = (0,)
    # Sin salida durante este tiempo -> ``stalled`` (None = el límite del job; 0 = desactivado).
    stall_timeout_s: float | None = None
    # "pipe" | "pty" | "stdbuf" (None = el del backend/configuración).
    io_mode: IoMode | None = None
    # True -> el núcleo antepone ``privilege_command`` (solo si la config lo permite).
    privileged: bool = False
    # Si devuelve True en el momento de ejecutar, el paso se salta (p. ej. el venv ya existe).
    skip_if: Callable[[], bool] | None = None


@dataclass
class FuncStep:
    name: str
    func: Callable[[StepContext], Awaitable[None]]
    skip_if: Callable[[], bool] | None = None


Step = CommandStep | FuncStep


@dataclass
class Plan:
    summary: str
    steps: list[Step]
    # Jobs con la misma clave se serializan (mismo venv, lock de dpkg, mismo clon...).
    lock_key: str | None = None
    # Valores literales a redactar en los logs de este job (contraseñas en URLs...).
    secrets: list[str] = field(default_factory=list)
    # Límites pedidos para este job (p. ej. por parámetros de la petición). None = el
    # de la configuración del backend, luego el por defecto del backend y luego el global.
    timeout_s: float | None = None
    stall_timeout_s: float | None = None


class Backend(ABC, Generic[P]):
    name: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[BackendParams]]
    # Ejecutables que deben existir en PATH (al menos uno de cada tupla si es tupla).
    executables: ClassVar[tuple[str | tuple[str, ...], ...]] = ()
    # Límites por defecto del backend (None = los globales de la configuración).
    # Prioridad: Plan (petición) > [backends.<nombre>] en config > estos > globales.
    default_job_timeout_s: ClassVar[float | None] = None
    default_stall_timeout_s: ClassVar[float | None] = None
    default_io_mode: ClassVar[IoMode | None] = None

    def __init__(self, config: Config) -> None:
        self.config = config
        self.options: dict[str, Any] = config.backend_options(self.name)

    # --- descubrimiento -------------------------------------------------
    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        return cls.Params.model_json_schema()

    def availability(self) -> tuple[bool, str]:
        missing = []
        for exe in self.executables:
            alternatives = exe if isinstance(exe, tuple) else (exe,)
            if not any(shutil.which(a) for a in alternatives):
                missing.append(" o ".join(alternatives))
        if missing:
            return False, "faltan ejecutables: " + ", ".join(missing)
        return True, "ok"

    # --- ciclo de vida --------------------------------------------------
    def validate(self, params: Mapping[str, Any]) -> P:
        """Valida tipos con ``Params`` y deja a ``check`` la validación semántica."""
        spec = self.Params.model_validate(dict(params))
        self.check(spec)  # type: ignore[arg-type]
        return spec  # type: ignore[return-value]

    def check(self, spec: P) -> None:  # noqa: B027 - hook opcional
        """Validación semántica adicional; lanza ``ValueError`` si algo no es aceptable."""

    def requires_privileges(self, spec: P) -> bool:
        return False

    @abstractmethod
    def plan(self, spec: P) -> Plan: ...

    async def detect_versions(self, spec: P, probe: Probe) -> dict[str, str]:
        return {}

    def result_info(self, spec: P) -> dict[str, Any]:
        return {}
