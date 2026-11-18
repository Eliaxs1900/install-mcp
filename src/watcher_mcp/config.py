"""Configuración del servidor.

Orden de carga: valores por defecto < fichero TOML < variables de entorno.

* Fichero: ``--config RUTA``, o ``$WATCHER_MCP_CONFIG``, o
  ``~/.config/watcher-mcp/config.toml`` si existe.
* Entorno: ``WATCHER_MCP_STATE_DIR`` (dónde se guardan jobs y logs).

Ver ``examples/config.example.toml``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "watcher-mcp"


@dataclass
class Config:
    # Dónde se persisten jobs (``jobs/<id>/``) y los venvs por nombre (``venvs/<nombre>``).
    state_dir: Path = field(default_factory=_default_state_dir)
    # Allowlist: solo estos backends se pueden usar aunque haya más registrados. Por defecto,
    # los instaladores en espacio de usuario. Fuera quedan ``apt`` (necesita root) y ``run``
    # (ejecuta comandos de una lista de programas que hay que configurar).
    allowed_backends: list[str] = field(
        default_factory=lambda: [
            "pip", "git", "download", "binary", "npm", "cargo", "go", "maven", "gradle", "brew", "docker",
        ]
    )
    # Backends que declaran necesitar privilegios (apt-get, ...) se rechazan salvo que esto sea True.
    allow_privileged: bool = False
    # Prefijo usado para los pasos privilegiados cuando allow_privileged=True. ``-n``: nunca pide contraseña.
    privilege_command: list[str] = field(default_factory=lambda: ["sudo", "-n"])
    # Jobs ejecutándose a la vez; el resto queda en ``queued``.
    max_parallel: int = 4
    # --- límites: ninguna espera es infinita -----------------------------------
    # Tiempo máximo total de un job en ejecución (sin contar la cola). Al vencer: SIGTERM,
    # SIGKILL y estado ``timed_out``. Se puede fijar por backend ([backends.<n>] job_timeout_s)
    # o por petición si el backend lo expone.
    default_job_timeout_s: float = 3600.0
    # Sin salida durante este tiempo -> estado ``stalled`` + aviso (el proceso sigue vivo).
    # 0 desactiva el watchdog. También configurable por backend/paso/petición.
    default_stall_timeout_s: float = 300.0
    # Tiempo máximo en cola esperando turno; al vencer, ``timed_out``.
    max_queue_s: float = 3600.0
    # ``wait`` sin timeout usa este valor; nunca espera más de ``max_wait_s`` (tope duro).
    default_wait_s: float = 60.0
    max_wait_s: float = 300.0
    # Tiempo máximo que puede tardar un listener (envío de una notificación).
    listener_timeout_s: float = 5.0
    # Cómo se lee la salida: "pipe" (PYTHONUNBUFFERED=1), "pty" (los programas C ven un
    # terminal y no acumulan buffer) o "stdbuf" (stdbuf -oL -eL; si no existe, pty).
    default_io_mode: str = "pipe"
    # Líneas de log que se guardan en memoria por job (en disco se guarda todo).
    memory_log_lines: int = 5000
    # Jobs terminados que se conservan en disco; los más antiguos se borran.
    max_jobs_kept: int = 200
    # Raíces bajo las que los backends pueden escribir (venvs, clones, descargas) y leer
    # ficheros que indique la petición (requirements, repos locales...).
    allowed_roots: list[Path] = field(default_factory=lambda: [Path.home()])
    # Dentro de las raíces se protegen las rutas con algún componente oculto (``~/.zshrc``,
    # ``~/.ssh``, ``proyecto/.git/hooks``...) y, en HOME, estos directorios de primer nivel
    # (``~/Library/LaunchAgents`` se ejecuta al iniciar sesión en macOS). Para permitir una de
    # esas zonas a propósito, añádela como raíz explícita (p. ej. ``~/.local/bin``).
    protect_dot_paths: bool = True
    protected_home_dirs: list[str] = field(default_factory=lambda: ["Library"])
    # Tope duro del tiempo de un job, aunque la petición o la configuración pidan "sin límite"
    # (``job_timeout_s = 0``): ninguna espera es infinita. 0 = sin tope (no recomendado).
    max_job_timeout_s: float = 86400.0
    # Al cancelar un paso interno (p. ej. una descarga en un hilo), cuánto se espera a que
    # termine de verdad antes de liberar su lock: debe cubrir el timeout de lectura de red.
    func_cancel_grace_s: float = 75.0
    # Patrones regex adicionales a redactar en logs.
    redact_patterns: list[str] = field(default_factory=list)
    # Backends externos a cargar: ``"paquete.modulo:Clase"``.
    plugins: list[str] = field(default_factory=list)
    # Opciones por backend: ``[backends.pip] prefer_uv = true``.
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def venvs_dir(self) -> Path:
        return self.state_dir / "venvs"

    def backend_options(self, name: str) -> dict[str, Any]:
        return dict(self.backends.get(name, {}))

    @property
    def instances_dir(self) -> Path:
        return self.state_dir / "instances"

    def roots(self) -> list[Path]:
        """Raíces permitidas (siempre incluye ``state_dir``)."""
        roots = [p.expanduser().resolve() for p in self.allowed_roots]
        roots.append(self.state_dir.expanduser().resolve())
        return roots

    def safe_path(self, path: str | Path, *, what: str) -> Path:
        """Ruta dentro de las raíces y fuera de las zonas protegidas (ver ``ensure_safe_path``)."""
        from .security import ensure_safe_path

        return ensure_safe_path(
            path, self.roots(), what=what,
            protected_home_dirs=self.protected_home_dirs, protect_dot_paths=self.protect_dot_paths,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        data: dict[str, Any] = {}
        candidate = path or os.environ.get("WATCHER_MCP_CONFIG")
        if candidate is None:
            default = Path.home() / ".config" / "watcher-mcp" / "config.toml"
            candidate = default if default.exists() else None
        if candidate is not None:
            with open(Path(candidate).expanduser(), "rb") as fh:
                data = tomllib.load(fh)
        cfg = cls.from_dict(data)
        if env_state := os.environ.get("WATCHER_MCP_STATE_DIR"):
            cfg.state_dir = Path(env_state).expanduser()
        return cfg

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Claves de configuración desconocidas: {sorted(unknown)}")
        kwargs = dict(data)
        if "state_dir" in kwargs:
            kwargs["state_dir"] = Path(kwargs["state_dir"]).expanduser()
        if "allowed_roots" in kwargs:
            kwargs["allowed_roots"] = [Path(p).expanduser() for p in kwargs["allowed_roots"]]
        return cls(**kwargs)
