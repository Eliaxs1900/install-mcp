"""Variables de entorno heredadas por nombre (``inherit_env`` / ``env_from``).

Un job puede necesitar un secreto que no debe viajar en los parámetros (quedaría en el
registro del job): p. ej. ``GEMINI_API_KEY``. En vez del valor se pide el *nombre*, y el
servidor lo copia de una fuente de confianza:

1. el entorno del propio servidor (el ``env`` con el que lo registró el cliente MCP);
2. si no está ahí y ``login_shell_env = true``, el entorno de la shell de login del usuario
   (``$SHELL -lic 'env -0'``), capturado **una vez** y solo para nombres listados en la
   configuración (``inherit_env``), nunca para los que pide una petición.

Los valores se declaran como secretos del plan: se redactan en logs, ``status``,
notificaciones y en el registro del job, que solo guarda los nombres.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping

log = logging.getLogger(__name__)

ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Variables que cambian qué código se ejecuta: nunca se heredan "a propósito" por nombre.
ENV_FORBIDDEN = re.compile(r"^(LD_|DYLD_)|^(PATH|PYTHONPATH|PYTHONSTARTUP|BASH_ENV|ENV|IFS|HOME|SHELL)$")

_MARK = "__WATCHER_MCP_ENV__"


def check_env_names(names: Iterable[str], *, what: str) -> list[str]:
    out = []
    for name in names:
        name = str(name)
        if not ENV_KEY.match(name) or ENV_FORBIDDEN.search(name):
            raise ValueError(f"{what}: nombre de variable no válido o prohibido: {name!r}")
        out.append(name)
    return out


def default_login_shell() -> str:
    return os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/sh")


def capture_login_env(shell: str | None = None, timeout_s: float = 15.0) -> dict[str, str]:
    """Entorno de una shell de login interactiva (lee ``~/.zprofile`` y ``~/.zshrc``).

    Sin terminal (``stdin`` nulo, sesión propia) y con timeout: si el rc pregunta algo o se
    cuelga, se devuelve ``{}``. Un marcador separa lo que imprima el rc del ``env -0``.
    """
    shell = shell or default_login_shell()
    try:
        res = subprocess.run(
            [shell, "-l", "-i", "-c", f"printf '\\0{_MARK}\\0'; env -0"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, start_new_session=True, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("no se pudo leer el entorno de la shell de login (%s): %r", shell, exc)
        return {}
    out = res.stdout.decode("utf-8", errors="replace")
    _, sep, rest = out.partition(f"\0{_MARK}\0")
    if not sep:
        return {}
    env: dict[str, str] = {}
    for item in rest.split("\0"):
        key, eq, value = item.partition("=")
        if eq and ENV_KEY.match(key):
            env[key] = value
    return env


class EnvSource:
    """Resuelve nombres a valores: entorno del servidor y, como respaldo, la shell de login."""

    def __init__(self, *, login_shell_env: bool = True, login_shell: str | None = None,
                 server_env: Mapping[str, str] | None = None) -> None:
        self.login_shell_env = login_shell_env
        self.login_shell = login_shell
        self._server_env = server_env
        self._login: dict[str, str] | None = None
        self._lock = threading.Lock()

    @property
    def server_env(self) -> Mapping[str, str]:
        return os.environ if self._server_env is None else self._server_env

    def login_env(self) -> dict[str, str]:
        """Capturado una sola vez (bloqueante: llamar desde un hilo)."""
        with self._lock:
            if self._login is None:
                self._login = capture_login_env(self.login_shell) if self.login_shell_env else {}
            return self._login

    def resolve(self, names: Iterable[str], *, login_fallback: bool) -> tuple[dict[str, str], list[str]]:
        """Devuelve ``(valores encontrados, nombres que faltan)``."""
        found: dict[str, str] = {}
        missing: list[str] = []
        for name in dict.fromkeys(names):
            if name in self.server_env:
                found[name] = self.server_env[name]
            else:
                missing.append(name)
        if missing and login_fallback and self.login_shell_env:
            login = self.login_env()
            for name in list(missing):
                if name in login:
                    found[name] = login[name]
                    missing.remove(name)
        return found, missing
