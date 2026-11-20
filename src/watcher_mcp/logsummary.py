"""Qué líneas del log explican un fallo (``relevant_lines`` del informe).

Una herramienta que falla suele imprimir la causa, luego una traza larga (Maven, Java,
Python, Node…) y por último texto de ayuda. Quedarse con las *últimas* líneas que casan con
``RELEVANT`` dejaba fuera la causa. Aquí se hace en tres pasos:

1. se descartan el ruido (marcos de traza, líneas ``[ERROR]`` vacías, textos de ayuda de
   Maven/Gradle/npm) y los duplicados;
2. se da prioridad a las **primeras** líneas relevantes (las de stderr primero), que suelen
   ser la causa;
3. se completa con las **últimas**, que suelen ser el resumen final. Todo en orden cronológico.

Además, cuando el job se quedó en silencio (atasco, tiempo agotado o cancelación):

* ``before_silence``: las últimas líneas de salida anteriores al silencio (la clave suele ser la
  última cosa que imprimió el proceso antes de callarse);
* ``wait_hint``: reconoce en esas líneas que el proceso **espera algo** (un depurador, una
  contraseña, un «yes/no»…) y lo convierte en una pista legible;
* ``probable_cause``: la primera línea relevante de stderr, para el titular de un fallo.
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
# Cuántas líneas anteriores al silencio se guardan (``before_silence``) y se examinan (``wait_hint``).
SILENCE_LINES = 5
# Tope de caracteres de una línea en el titular («causa probable») y en la pista.
CAUSE_MAX = 200

# «El proceso espera algo»: (patrón, plantilla de la pista). Solo se aplican a las últimas líneas
# anteriores a un silencio, nunca a un log completo. Las preguntas de sí/no y las contraseñas deben
# estar al **final** de la línea (el aviso de un prompt es lo último que se imprime antes de callar).
_PROMPT_END = r"[\s:?]*$"
WAIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Listening for transport dt_socket at address:\s*(\d+)"),
     "la JVM espera a que se conecte un depurador en el puerto {0} "
     "(p. ej. `maven.surefire.debug` o `-agentlib:jdwp=…suspend=y`)"),
    (re.compile(r"(?i)(?:\[sudo\] password for\b|\bpassword(?: for \S+)?:" + _PROMPT_END + ")"),
     "el proceso pide una contraseña por la entrada estándar y nadie puede escribirla"),
    (re.compile(r"(?i)\benter passphrase\b.*:" + _PROMPT_END),
     "el proceso pide una frase de paso (passphrase) y nadie puede escribirla"),
    (re.compile(r"Are you sure you want to continue connecting \(yes/no"),
     "ssh espera que se confirme la huella del servidor (yes/no); usa `-o StrictHostKeyChecking=accept-new` "
     "o añade el host a known_hosts"),
    (re.compile(r"(?i)\bpress (?:any key|enter)\b"),
     "el proceso espera que se pulse una tecla"),
    (re.compile(r"(?i)(?:\(y/n\)|\[y/n\]|\(yes/no\)|\[yes/no\])" + _PROMPT_END),
     "el proceso espera una respuesta de sí/no (y/n); lanza la orden en modo no interactivo (p. ej. `-y`)"),
]


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


def before_silence(lines: Sequence[_Line], stall_seq: int | None = None, n: int = SILENCE_LINES) -> list[_Line]:
    """Las últimas ``n`` líneas de salida (stdout/stderr) anteriores al silencio.

    ``stall_seq`` es el ``seq`` de la nota «atasco» del último atasco; sin él (cancelado o tiempo
    agotado sin atasco previo) se toma el final del log. Las líneas ``core`` nunca cuentan."""
    out = [ln for ln in lines if ln.stream != "core" and (stall_seq is None or ln.seq < stall_seq)]
    return out[-n:] if n > 0 else []


def wait_hint(lines: Sequence[_Line | str], n: int = SILENCE_LINES) -> str | None:
    """Pista legible si las últimas ``n`` líneas antes del silencio indican que el proceso espera algo.

    Gana la coincidencia más reciente. Sin coincidencia, ``None``."""
    texts = [ln if isinstance(ln, str) else ln.text for ln in lines][-n:]
    for text in reversed(texts):
        for pattern, template in WAIT_PATTERNS:
            if m := pattern.search(text):
                return template.format(*m.groups())
    return None


def probable_cause(lines: Iterable[_Line], error: str = "") -> str | None:
    """Primera línea relevante de stderr (sin ruido), recortada, o ``None`` si no hay o ya está en ``error``.

    Se salta la cabecera ``Traceback (most recent call last):``, que no dice nada por sí sola."""
    for ln in lines:
        if ln.stream != "stderr" or not RELEVANT.search(ln.text) or is_noise(ln.text):
            continue
        text = ln.text.strip()
        if not text or text.startswith("Traceback (most recent call last)"):
            continue
        if text in error:
            return None
        return text if len(text) <= CAUSE_MAX else text[: CAUSE_MAX - 1] + "…"
    return None
