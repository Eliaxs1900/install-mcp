"""Utilidades de seguridad compartidas por los backends.

* Redacción de secretos en texto (logs, argv mostrados, informes).
* Validación de URLs (solo HTTPS) y de checksums para descargas.
* Comprobación de que una ruta cae dentro de las raíces permitidas.
* Rechazo de argumentos que empiezan por ``-`` (inyección de opciones).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit

REDACTED = "***"
# Claves cuyo valor es un secreto en estructuras (parámetros de un job): se ocultan por nombre,
# porque los patrones de texto (``clave=valor``) no casan con JSON (``"clave": "valor"``).
SECRET_KEY = re.compile(r"(?i)(token|password|passwd|pwd|secret|api[_-]?key|access[_-]?key|credential|auth)")

# Patrones genéricos de secretos. Se aplican a cada línea de log antes de guardarla.
_DEFAULT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Credenciales en URLs: https://usuario:token@host -> https://***@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@"), r"\1" + REDACTED + "@"),
    # Cabeceras y asignaciones: token=..., password: ..., Authorization: Bearer ...
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(bearer|basic|token)?\s*[^\s,;]+"
        ),
        r"\1\2" + REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b([a-z0-9_.-]*(?:token|password|passwd|pwd|secret|api[_-]?key|access[_-]?key)[a-z0-9_.-]*)"
            r"(\s*[:=]\s*)(['\"]?)[^\s'\",;&]+\3"
        ),
        r"\1\2" + REDACTED,
    ),
    # Tokens con formato conocido.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), REDACTED),
]


class Redactor:
    """Sustituye secretos por ``***``.

    Combina patrones genéricos, patrones extra de la configuración y secretos
    literales concretos que declara cada job (p. ej. la contraseña de un índice).
    """

    def __init__(self, extra_patterns: Iterable[str] = (), literals: Iterable[str] = ()) -> None:
        self._patterns = list(_DEFAULT_PATTERNS)
        self._patterns += [(re.compile(p), REDACTED) for p in extra_patterns]
        self._literals = sorted({s for s in literals if s and len(s) >= 4}, key=len, reverse=True)

    def with_literals(self, literals: Iterable[str]) -> Redactor:
        clone = Redactor()
        clone._patterns = self._patterns
        clone._literals = sorted({*self._literals, *(s for s in literals if s and len(s) >= 4)}, key=len, reverse=True)
        return clone

    def __call__(self, text: str) -> str:
        for lit in self._literals:
            text = text.replace(lit, REDACTED)
        for pattern, repl in self._patterns:
            text = pattern.sub(repl, text)
        return text

    def argv(self, argv: Iterable[str]) -> list[str]:
        return [self(a) for a in argv]

    def redact_obj(self, obj: Any) -> Any:
        """Copia de una estructura (dict/list/str) con los secretos ocultos: por nombre de clave
        y, en el resto de textos, con los mismos patrones y literales que los logs."""
        if isinstance(obj, dict):
            return {
                k: (REDACTED if isinstance(k, str) and SECRET_KEY.search(k) and isinstance(v, str) and v
                    else self.redact_obj(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        if isinstance(obj, str):
            return self(obj)
        return obj


def url_secrets(url: str) -> list[str]:
    """Devuelve usuario/contraseña embebidos en una URL, para redactarlos como literales."""
    parts = urlsplit(url)
    return [s for s in (parts.username, parts.password) if s]


def require_https(url: str, *, what: str = "URL") -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{what} debe ser https://… (recibido: {Redactor()(url)!r})")
    return url


@dataclass(frozen=True)
class Checksum:
    algorithm: str
    hexdigest: str

    _ALGOS = ("sha256", "sha384", "sha512")

    @classmethod
    def parse(cls, value: str) -> Checksum:
        """Acepta ``sha256:<hex>`` (también sha384/sha512)."""
        algo, sep, digest = value.partition(":")
        algo = algo.lower().strip()
        digest = digest.lower().strip()
        if not sep or algo not in cls._ALGOS:
            raise ValueError(f"Checksum inválido {value!r}: usa 'sha256:<hex>' (o sha384/sha512)")
        expected_len = hashlib.new(algo).digest_size * 2
        if len(digest) != expected_len or not re.fullmatch(r"[0-9a-f]+", digest):
            raise ValueError(f"Checksum {algo} debe tener {expected_len} caracteres hexadecimales")
        return cls(algo, digest)

    def verify_file(self, path: Path) -> bool:
        h = hashlib.new(self.algorithm)
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == self.hexdigest


def ensure_within_roots(path: str | Path, roots: Iterable[Path], *, what: str = "ruta") -> Path:
    """Resuelve ``path`` (debe ser absoluta) y exige que esté dentro de alguna raíz permitida."""
    roots = list(roots)
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(f"La {what} debe ser absoluta: {path!r}")
    resolved = p.resolve()
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise ValueError(f"La {what} {str(resolved)!r} está fuera de las raíces permitidas ({', '.join(map(str, roots))})")


def ensure_safe_path(
    path: str | Path,
    roots: Iterable[Path],
    *,
    what: str = "ruta",
    home: Path | None = None,
    protected_home_dirs: Iterable[str] = (),
    protect_dot_paths: bool = True,
) -> Path:
    """Como :func:`ensure_within_roots`, y además rechaza las zonas sensibles.

    Un cliente MCP puede recibir instrucciones inyectadas (un README, una web...): sin esta
    regla, una petición podría escribir ``~/.zshrc`` o ``~/.ssh/authorized_keys``, leer una
    clave privada como ``requirements`` o dejar un ``LaunchAgent`` en ``~/Library``. Se rechaza,
    relativo a la raíz permitida más profunda que contiene la ruta:

    * cualquier componente que empiece por ``.`` (dotfiles, ``.ssh``, ``.config``, ``.git/hooks``...);
    * si esa raíz es ``home``, los directorios de primer nivel de ``protected_home_dirs`` (``Library``).

    Para permitir una de esas zonas a propósito, se añade como raíz explícita en
    ``allowed_roots`` (p. ej. ``~/.local/bin``): entonces lo relativo a ella ya no la incluye.
    """
    roots = [Path(r).expanduser().resolve() for r in roots]
    resolved = ensure_within_roots(path, roots, what=what)
    containing = [r for r in roots if resolved == r or resolved.is_relative_to(r)]
    root = max(containing, key=lambda r: len(r.parts))
    relative = resolved.relative_to(root).parts
    if protect_dot_paths:
        hidden = next((part for part in relative if part.startswith(".")), None)
        if hidden is not None:
            raise ValueError(
                f"La {what} {str(resolved)!r} pasa por {hidden!r}, una zona protegida (ficheros y carpetas "
                f"ocultos: configuración, claves, hooks). Si de verdad la necesitas, añade esa carpeta "
                f"como raíz explícita en allowed_roots."
            )
    home_resolved = (home or Path.home()).expanduser().resolve()
    if root == home_resolved and relative and relative[0] in set(protected_home_dirs):
        raise ValueError(
            f"La {what} {str(resolved)!r} está en ~/{relative[0]}, una zona protegida (p. ej. LaunchAgents se "
            f"ejecutan al iniciar sesión). Si de verdad la necesitas, añádela como raíz explícita en allowed_roots."
        )
    return resolved


def reject_option_like(values: Iterable[str], *, what: str) -> None:
    for v in values:
        if v.strip().startswith("-"):
            raise ValueError(f"{what}: no se admiten opciones ({v!r}); usa los parámetros del backend")
