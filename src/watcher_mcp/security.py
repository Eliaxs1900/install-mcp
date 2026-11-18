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
from pathlib import Path
from urllib.parse import urlsplit

REDACTED = "***"

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
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(f"La {what} debe ser absoluta: {path!r}")
    resolved = p.resolve()
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise ValueError(f"La {what} {str(resolved)!r} está fuera de las raíces permitidas ({', '.join(map(str, roots))})")


def reject_option_like(values: Iterable[str], *, what: str) -> None:
    for v in values:
        if v.strip().startswith("-"):
            raise ValueError(f"{what}: no se admiten opciones ({v!r}); usa los parámetros del backend")
