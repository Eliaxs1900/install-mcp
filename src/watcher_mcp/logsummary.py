"""Qué líneas del log explican un fallo (``relevant_lines`` del informe).

Una herramienta que falla suele imprimir la causa, luego una traza larga (Maven, Java,
Python, Node…) y por último texto de ayuda. Quedarse con las *últimas* líneas que casan con
``RELEVANT`` dejaba fuera la causa. Aquí se hace en tres pasos:

1. se descartan el ruido (marcos de traza, líneas ``[ERROR]`` vacías, textos de ayuda de
   Maven/Gradle/npm) y los duplicados;
2. se da prioridad a las **primeras** líneas relevantes (las de stderr primero), que suelen
   ser la causa;
3. se completa con las **últimas**, que suelen ser el resumen final. Todo en orden cronológico.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

# Líneas que suelen explicar un fallo; se destacan en el informe.
RELEVANT = re.compile(
    r"(?i)\b(error|failed|failure|fatal|exception|traceback|denied|not found|no matching|"
    r"could not|cannot|unable|conflict|timed? ?out|no solution|e:)\b"
    r"|\b\w+(?:Error|Exception)\b|\bnpm ERR!"
)

# Marcos de traza, líneas "[ERROR]" vacías y texto de ayuda que no explican nada.
NOISE = re.compile(
    # Marco de traza: Java "at pkg.Clase.metodo(Fichero.java:12)", JS "at /x.js:1:7"; no "at line 3: …".
    r"^\s*(?:\[ERROR\]\s*)?(?:at\s+\S.*(?:\([^)]*\)|:\d+(?::\d+)?)\s*$|\.\.\. \d+ more)"
    r"|^\s*\[ERROR\]\s*$"
    r"|-> \[Help|\[Help \d+\]|Re-run Maven|To see the full stack trace|For more information about the errors"
    r"|^\s*(?:\[ERROR\]\s*)?(?:\* Try:|Run with --stacktrace|Get more help at|> Run with --)"
    r"|npm ERR! A complete log of this run"
)

HEAD = 10
TAIL = 10


class _Line(Protocol):
    stream: str
    text: str


def is_noise(text: str) -> bool:
    return bool(NOISE.search(text))


def select_relevant(lines: Iterable[_Line], *, head: int = HEAD, tail: int = TAIL) -> list[_Line]:
    """Hasta ``head + tail`` líneas relevantes, sin ruido ni duplicados, en orden cronológico."""
    seen: set[str] = set()
    cands: list[_Line] = []
    for ln in lines:
        if ln.stream == "core" or not RELEVANT.search(ln.text) or is_noise(ln.text):
            continue
        key = ln.text.strip()
        if key in seen:
            continue
        seen.add(key)
        cands.append(ln)
    if len(cands) <= head + tail:
        return cands
    # Primeras: las de stderr tienen prioridad (suelen ser la causa); se completa con el resto.
    idx = [i for i, ln in enumerate(cands) if ln.stream == "stderr"][:head]
    if len(idx) < head:
        idx += [i for i in range(len(cands)) if i not in set(idx)][: head - len(idx)]
    chosen = set(idx)
    # Últimas: las que quedan más cerca del final.
    chosen.update([i for i in range(len(cands) - 1, -1, -1) if i not in chosen][:tail])
    return [cands[i] for i in sorted(chosen)]


def next_useful(lines: Sequence[str], start: int, prefix: str = "[ERROR]") -> str | None:
    """Primera línea desde ``start`` que empieza por ``prefix`` y no es ruido (sin el prefijo)."""
    for line in lines[start:]:
        s = line.strip()
        if s.startswith(prefix) and not is_noise(s):
            return s[len(prefix):].strip()
    return None
