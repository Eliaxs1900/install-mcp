"""Utilidades de seguridad compartidas por los backends.

* Redacción de secretos en texto (logs, argv mostrados, informes).
* Validación de URLs (solo HTTPS) y de checksums para descargas.
* Comprobación de que una ruta cae dentro de las raíces permitidas.
* Rechazo de argumentos que empiezan por ``-`` (inyección de opciones).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REDACTED = "***"
# Claves cuyo valor es un secreto en estructuras (parámetros de un job): se ocultan por nombre,
# porque los patrones de texto (``clave=valor``) no casan con JSON (``"clave": "valor"``). Se compara por
# PALABRAS (separadas por ``_``, ``-``, ``.`` o mayúsculas de camelCase), no por subcadena: ``api_token`` y
# ``accessKey`` sí, pero ``oauthlib``, ``tokenizers`` o ``author`` no.
_SECRET_WORDS = frozenset({
    "token", "tokens", "password", "passwords", "passwd", "pwd", "secret", "secrets", "credential", "credentials",
    "auth", "authorization", "apikey", "accesskey", "privatekey", "passphrase",
})
_SECRET_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key")})
# Mapas cuyas claves son datos y no nombres de campo (paquete -> versión): ahí NO se oculta por clave
# (``google-auth: 2.30`` no es un secreto), solo se redactan los valores como texto.
PLAIN_MAPS = frozenset({"installed", "dists", "versions"})


def is_secret_key(name: str) -> bool:
    words = re.findall(r"[a-z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower())
    return any(w in _SECRET_WORDS for w in words) or any(pair in _SECRET_PAIRS for pair in zip(words, words[1:]))


# Palabras que delatan una clave secreta (``token``, ``password``...).
_KEYWORDS = r"token|password|passwd|pwd|secret|api[_-]?key|access[_-]?key"
_KEYWORD = re.compile(rf"(?i){_KEYWORDS}")
# Los patrones de clave/valor empiezan SOLO al principio de un identificador (lookbehind) y acotan su
# longitud: con ``\b`` y ``[a-z0-9_.-]*`` se reintentaba desde cada ``-``/``.`` y una línea «a-a-a-…» de
# 8 KiB costaba O(n²) (~1 s de CPU en el bucle de eventos, ReDoS). Además cada patrón lleva una guarda
# barata: si la línea no contiene su palabra clave, ni se prueba.
_IDENT_START = r"(?<![a-z0-9_.-])"
_VALUE = r"\"[^\"\n]*\"|'[^'\n]*'|[^\s'\",;&]+"  # entrecomillado (con espacios) o hasta separador

# Patrones genéricos de secretos: (patrón, sustitución, guarda). Se aplican a cada línea de log.
_DEFAULT_PATTERNS: list[tuple[re.Pattern[str], str, Callable[[str], bool]]] = [
    # Credenciales en URLs: https://usuario:token@host -> https://***@host
    (
        re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,31}://)[^/\s:@]+(?::[^/\s@]*)?@"),
        r"\1" + REDACTED + "@",
        lambda t: "://" in t,
    ),
    # Cabeceras y asignaciones: Authorization: Bearer ... (sin \s* encadenados: eran cuadráticos)
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;]+"
        ),
        r"\1\2" + REDACTED,
        lambda t: "authorization" in t.lower(),
    ),
    # token=..., password: "a b", {"password": "x"}, API_KEY='a b c'
    (
        re.compile(
            rf"(?i){_IDENT_START}([a-z0-9_.-]{{0,64}}(?:{_KEYWORDS})[a-z0-9_.-]{{0,64}}[\"']?)"
            rf"(\s*[:=]\s*)({_VALUE})"
        ),
        r"\1\2" + REDACTED,
        lambda t: _KEYWORD.search(t) is not None,
    ),
    # Tokens con formato conocido.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED, lambda t: "gh" in t),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED, lambda t: "github_pat_" in t),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), REDACTED, lambda t: "glpat-" in t),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED, lambda t: "AKIA" in t),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED, lambda t: "xox" in t),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), REDACTED, lambda t: "sk-" in t),
]

# Opciones de línea de comandos cuyo VALOR (el argumento siguiente) es un secreto: ``--password x``,
# ``--api-key x``. No las que solo nombran un fichero o una ruta (``--token-file x``).
_SECRET_OPTION = re.compile(rf"(?i)^--?[a-z0-9_.-]*(?:{_KEYWORDS})[a-z0-9_.-]*$")
_PATH_OPTION = re.compile(r"(?i)(?:file|path|dir|url|name|env|from)$")
# ``-p``/``-u`` son demasiado ambiguos para redactar en cualquier programa: solo en estos.
_SHORT_SECRET_FLAGS: dict[str, frozenset[str]] = {
    "sshpass": frozenset({"-p"}),
    "curl": frozenset({"-u"}),
}
_ATTACHED_SECRET_PROGRAMS = frozenset({"mysql", "mysqldump", "mysqladmin", "mariadb"})  # ``-pSECRETO``


class Redactor:
    """Sustituye secretos por ``***``.

    Combina patrones genéricos, patrones extra de la configuración y secretos
    literales concretos que declara cada job (p. ej. la contraseña de un índice).
    """

    def __init__(self, extra_patterns: Iterable[str] = (), literals: Iterable[str] = ()) -> None:
        self._patterns = list(_DEFAULT_PATTERNS)
        self._patterns += [(re.compile(p), REDACTED, lambda _t: True) for p in extra_patterns]
        self._literals = sorted({s for s in literals if s and len(s) >= 4}, key=len, reverse=True)

    def with_literals(self, literals: Iterable[str]) -> Redactor:
        clone = Redactor()
        clone._patterns = self._patterns
        clone._literals = sorted({*self._literals, *(s for s in literals if s and len(s) >= 4)}, key=len, reverse=True)
        return clone

    def __call__(self, text: str) -> str:
        for lit in self._literals:
            text = text.replace(lit, REDACTED)
        for pattern, repl, guard in self._patterns:
            if guard(text):
                text = pattern.sub(repl, text)
        return text

    def argv(self, argv: Iterable[str]) -> list[str]:
        """Redacta cada argumento y, además, el valor que sigue a ``--password``/``--token``... (y a
        ``-p``/``-u`` en los programas que los usan para eso: ``sshpass``, ``curl``)."""
        args = list(argv)
        program = Path(args[0]).name.lower() if args else ""
        short = _SHORT_SECRET_FLAGS.get(program, frozenset())
        out: list[str] = []
        hide: str | None = None  # "known": se oculta siempre; "option": salvo que lo siguiente sea otra opción
        for arg in args:
            if hide and (hide == "known" or not arg.startswith("-")):
                out.append(REDACTED)
                hide = None
                continue
            hide = None
            if program in _ATTACHED_SECRET_PROGRAMS and arg.startswith("-p") and not arg.startswith("--") and len(arg) > 2:
                out.append("-p" + REDACTED)
                continue
            out.append(self(arg))
            if arg in short:
                hide = "known"
            elif "=" not in arg and _SECRET_OPTION.match(arg) and not _PATH_OPTION.search(arg):
                hide = "option"
        return out

    def redact_obj(self, obj: Any, *, _by_key: bool = True) -> Any:
        """Copia de una estructura (dict/list/str) con los secretos ocultos: por nombre de clave, en las
        listas de argumentos (``argv``/``command``/``args``) también el valor que sigue a ``--password``, y
        en el resto de textos con los mismos patrones y literales que los logs."""
        if isinstance(obj, dict):
            program = obj.get("program") if isinstance(obj.get("program"), str) else ""
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and isinstance(v, list) and all(isinstance(a, str) for a in v):
                    if k in ("argv", "command"):
                        out[k] = self.argv(v)
                        continue
                    if k == "args":  # sin el programa: el de al lado (``program``), si lo hay
                        out[k] = self.argv([program, *v])[1:]
                        continue
                if _by_key and isinstance(k, str) and isinstance(v, str) and v and is_secret_key(k):
                    out[k] = REDACTED
                else:
                    out[k] = self.redact_obj(v, _by_key=_by_key and not (isinstance(k, str) and k in PLAIN_MAPS))
            return out
        if isinstance(obj, list):
            return [self.redact_obj(v, _by_key=_by_key) for v in obj]
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
    clave privada como ``requirements`` o dejar un ``LaunchAgent``. Se rechaza, relativo a la
    raíz permitida más profunda que contiene la ruta:

    * cualquier componente que empiece por ``.`` (dotfiles, ``.ssh``, ``.config``, ``.git/hooks``...);
    * si esa raíz es ``home``, las subrutas de ``protected_home_dirs``: rutas relativas a HOME
      con comodines por componente (``Library/LaunchAgents``,
      ``Library/Application Support/com.apple.*``). Se protegen subrutas concretas y no
      ``~/Library`` entero: ahí viven también carpetas de trabajo legítimas, como las de las
      sesiones de Claude Desktop (``~/Library/Application Support/Claude/...``).

    Para permitir una de esas zonas a propósito, se añade como raíz explícita en
    ``allowed_roots`` (p. ej. ``~/.local/bin``): entonces lo relativo a ella ya no la incluye.
    El error dice dónde se configura y da la línea exacta para pegar.
    """
    roots = [Path(r).expanduser().resolve() for r in roots]
    resolved = ensure_within_roots(path, roots, what=what)
    containing = [r for r in roots if resolved == r or resolved.is_relative_to(r)]
    root = max(containing, key=lambda r: len(r.parts))
    relative = resolved.relative_to(root).parts
    if protect_dot_paths:
        hidden = _hidden_component(relative)
        if hidden is not None:
            raise ValueError(
                f"La {what} {str(resolved)!r} pasa por {hidden!r}, una zona protegida (ficheros y carpetas "
                f"ocultos: configuración, claves, hooks). {_allow_hint(resolved)}"
            )
    home_resolved = (home or Path.home()).expanduser().resolve()
    # Las subrutas protegidas de HOME valen para TODA ruta que caiga en HOME, también si la raíz permitida
    # es un ancestro (``/Users``, ``/``). La excepción documentada es una raíz explícita DENTRO de HOME
    # (``~/.local/bin``): ahí la zona se ha permitido a propósito.
    in_home = resolved == home_resolved or resolved.is_relative_to(home_resolved)
    root_inside_home = root != home_resolved and root.is_relative_to(home_resolved)
    if in_home and not root_inside_home:
        home_relative = resolved.relative_to(home_resolved).parts
        for pattern in protected_home_dirs:
            parts = PurePosixPath(pattern).parts
            if len(home_relative) >= len(parts) and all(
                # Sin distinguir mayúsculas: el disco de macOS (APFS por defecto) tampoco lo hace, y
                # ~/library/launchagents es la misma carpeta que ~/Library/LaunchAgents.
                fnmatch.fnmatchcase(rel.lower(), pat.lower()) for rel, pat in zip(home_relative, parts, strict=False)
            ):
                raise ValueError(
                    f"La {what} {str(resolved)!r} está en ~/{'/'.join(home_relative[:len(parts)])}, una zona "
                    f"protegida (p. ej. LaunchAgents se ejecutan al iniciar sesión; Keychains o Cookies guardan "
                    f"secretos). {_allow_hint(resolved)}"
                )
    return resolved


def _hidden_component(relative: tuple[str, ...]) -> str | None:
    """Primer componente oculto de ``relative``, salvo los worktrees de Claude Code.

    Excepción estrecha: ``.claude/worktrees/<nombre>`` (las sesiones con worktree viven en
    ``<repo>/.claude/worktrees/<nombre>``). Solo esos tres componentes se saltan: lo que cuelga del
    worktree se sigue comprobando (``.git``, ``.env``, ``.claude/settings.json``...), y ``.claude`` fuera de
    ``worktrees/<nombre>`` (``.claude/settings.json``, ``.claude/hooks``) sigue protegido."""
    i = 0
    while i < len(relative):
        part = relative[i]
        if part.startswith("."):
            is_worktree = (
                part.lower() == ".claude" and i + 2 < len(relative)
                and relative[i + 1].lower() == "worktrees" and not relative[i + 2].startswith(".")
            )
            if not is_worktree:
                return part
            i += 3
            continue
        i += 1
    return None


# Nombres ocultos que un archivo no puede crear dentro de su destino aunque el destino sea una carpeta de
# trabajo: credenciales y configuración que se aplican solas (``.npmrc`` con registro, ``.yarnrc.yml`` con
# ``yarnPath``, ``.claude/settings*``...). Los demás ocultos de un tarball normal (``.gitignore``,
# ``.github/``, ``.editorconfig``...) sí se extraen.
SENSITIVE_MEMBER_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config", ".netrc", ".npmrc", ".pypirc", ".yarnrc",
    ".yarnrc.yml", ".pgpass", ".my.cnf", ".claude",
})
_SENSITIVE_GIT_PARTS = frozenset({"hooks", "config"})  # ``.git/hooks/*`` se ejecutan; ``.git/config`` define filtros


def ensure_safe_member(
    path: str | Path,
    dest: str | Path,
    roots: Iterable[Path],
    *,
    what: str = "miembro del archivo",
    home: Path | None = None,
    protected_home_dirs: Iterable[str] = (),
    protect_dot_paths: bool = True,
) -> Path:
    """Valida el destino final de un miembro de un archivo extraído en ``dest`` (ya validado como ruta).

    Criterio, por zonas y no por «cualquier punto»:

    * siempre: dentro de las raíces y fuera de ``protected_home_dirs`` (``Library/LaunchAgents``...);
    * si ``dest`` es HOME o un ancestro de HOME: ningún componente oculto (cualquier ``.zshrc``,
      ``.gitconfig``, ``.ssh``... de HOME es configuración personal o se ejecuta);
    * si ``dest`` está dentro de HOME o fuera de él (una carpeta de trabajo): los ocultos por debajo de
      ``dest`` se aceptan (``.gitignore``, ``.github/``) salvo ``SENSITIVE_MEMBER_NAMES`` y ``.git/hooks``,
      ``.git/config``.
    """
    roots = list(roots)
    resolved = ensure_safe_path(path, roots, what=what, home=home, protected_home_dirs=protected_home_dirs,
                                protect_dot_paths=False)
    if not protect_dot_paths:
        return resolved
    dest_resolved = Path(dest).expanduser().resolve()
    home_resolved = (home or Path.home()).expanduser().resolve()
    relative = resolved.relative_to(dest_resolved).parts if (
        resolved == dest_resolved or resolved.is_relative_to(dest_resolved)) else resolved.parts
    if dest_resolved == home_resolved or home_resolved.is_relative_to(dest_resolved):
        blocked = _hidden_component(relative)
        why = "un componente oculto de HOME (configuración personal o que se ejecuta al iniciar sesión)"
    else:
        blocked = _sensitive_member_component(relative)
        why = "una zona sensible (credenciales, configuración o hooks que se aplican solos)"
    if blocked is not None:
        raise ValueError(
            f"El {what} {str(resolved)!r} pasa por {blocked!r}, {why}. Extrae el archivo en una carpeta de "
            f"trabajo (no en HOME) o sin ese miembro."
        )
    return resolved


def _sensitive_member_component(relative: tuple[str, ...]) -> str | None:
    lowered = [part.lower() for part in relative]
    for i, part in enumerate(lowered):
        if part in SENSITIVE_MEMBER_NAMES:
            return relative[i]
        if part == ".git" and i + 1 < len(lowered) and lowered[i + 1] in _SENSITIVE_GIT_PARTS:
            return f"{relative[i]}/{relative[i + 1]}"
    return None


CONFIG_LOCATIONS = "~/.config/watcher-mcp/config.toml (o el fichero de --config / $WATCHER_MCP_CONFIG)"


def _allow_hint(resolved: Path) -> str:
    """Cómo permitir una zona protegida: dónde se configura y la línea exacta para pegar."""
    folder = resolved if resolved.is_dir() else resolved.parent
    return (
        f"Si de verdad la necesitas, añádela como raíz explícita en {CONFIG_LOCATIONS}: "
        f'allowed_roots = ["~", "{folder}"]'
    )


def reject_option_like(values: Iterable[str], *, what: str) -> None:
    for v in values:
        if v.strip().startswith("-"):
            raise ValueError(f"{what}: no se admiten opciones ({v!r}); usa los parámetros del backend")
