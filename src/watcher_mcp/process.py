"""Ejecución de un comando con salida en vivo, sin shell, cancelable y siempre acotada.

* ``asyncio.create_subprocess_exec`` con argv explícito (nunca ``shell=True``).
* stdout y stderr se leen por separado en trozos y se cortan por ``\\n`` y ``\\r``
  (las barras de progreso que reescriben la línea también llegan como líneas).
* Modos de lectura de stdout (``io_mode``):
  - ``pipe``: tubería normal + ``PYTHONUNBUFFERED=1`` (suficiente para Python, uv, git...).
  - ``pty``: stdout es un pseudo-terminal; la libc de los programas C (y Python) pasa a
    buffer por líneas, así que la salida llega aunque el programa no haga flush.
  - ``stdbuf``: antepone ``stdbuf -oL -eL`` (o ``gstdbuf`` en macOS); si no existe, ``pty``.
  stderr va siempre por tubería (la libc no lo bufferiza).
* El proceso arranca en su propio grupo (``start_new_session``) para que cancelar
  mate también a los hijos (pip -> compilador, git -> ssh, ...).
* Ninguna espera es infinita: tras SIGKILL se espera un tiempo acotado y, si el proceso
  sigue sin poder recogerse (estado D), se abandona y se informa.
* Al cancelar o agotar el tiempo también se persigue a los descendientes que escaparon del
  grupo (``setsid``, daemons): se fotografía el árbol antes de señalar (después quedarían
  adoptados por init y no habría forma fiable de saber de quién eran).
* El entorno de los jobs no hereda las credenciales del servidor (``SERVER_ONLY_ENV``).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fnmatch
import os
import shutil
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

MAX_LINE = 8192
TERM_GRACE_S = 5.0
KILL_GRACE_S = 5.0
DRAIN_S = 3.0
# Tope de cada flujo (stdout y stderr) en ``run_capture`` (sondas de versión): al pasarlo se mata.
# Generoso (una sonda legítima, como ``npm ls --json`` de un proyecto grande, pasa de 64 KiB) pero
# acotado: ``yes --version`` se corta en milisegundos. Se puede cambiar por llamada (``max_bytes``).
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
# Una línea sin terminar se emite troceada al pasar de MAX_LINE; se retienen estos caracteres de más
# para que un secreto que cruza el corte se redacte entero antes de salir (ver ``_pump``).
HOLD_BACK = 512

# Variables del servidor que los jobs NO heredan: credenciales con las que Claude Code deja que el
# servidor MCP hable con la app (mensajería entre sesiones, OAuth) y datos personales de la cuenta.
# Ningún instalador las necesita y un ``setup.py``/script cualquiera las leería. Se puede dar una a
# propósito con ``inherit_env`` de la configuración (``extra`` de ``build_env`` manda). El resto de
# ``CLAUDE_CODE_*`` (ids de sesión, flags) y PATH, HOME, proxies, etc. se mantienen.
SERVER_ONLY_ENV: tuple[str, ...] = (
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_ACCOUNT_UUID",
    "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_USER_EMAIL",
)
# Además de la lista, estos patrones (sobre el nombre en mayúsculas) cubren credenciales de la app y de
# Anthropic que no se conocen de antemano (``CLAUDE_CODE_SESSION_ACCESS_TOKEN``, ``ANTHROPIC_API_KEY``,
# ``ANTHROPIC_AUTH_TOKEN``...). No hay un ``*_TOKEN`` genérico: ``GH_TOKEN``, ``NPM_TOKEN``, ``HF_TOKEN``
# son las que git, npm o pip pueden necesitar de verdad y sus dueños deben decidirlo con ``inherit_env``.
SERVER_ONLY_PATTERNS: tuple[str, ...] = (
    "CLAUDE_CODE_*TOKEN*",
    "CLAUDE_CODE_*SECRET*",
    "CLAUDE_CODE_*API_KEY*",
    "CLAUDE_CODE_*CREDENTIAL*",
    "ANTHROPIC_*",
)


def is_server_only(name: str) -> bool:
    """¿Es una variable que los jobs no heredan del servidor (lista o patrón)?"""
    upper = name.upper()
    return upper in SERVER_ONLY_ENV or any(fnmatch.fnmatchcase(upper, pat) for pat in SERVER_ONLY_PATTERNS)


@dataclass
class ProcResult:
    exit_code: int | None
    cancelled: bool = False
    timed_out: bool = False
    pid: int | None = None
    abandoned: bool = False  # ni SIGKILL lo terminó a tiempo
    # Procesos del grupo que siguieron vivos tras SIGTERM y hubo que matar con SIGKILL
    # (p. ej. un JVM de surefire suspendido por JDWP, que ignora SIGTERM).
    killed: list[int] = field(default_factory=list)
    # Procesos (del grupo o que escaparon de él) que ni siquiera SIGKILL eliminó (estado D, otro
    # usuario...). El núcleo los informa como ``orphans``.
    survivors: list[int] = field(default_factory=list)
    # Descendientes que habían salido del grupo (setsid) y se terminaron al cancelar/agotar el tiempo.
    escaped: list[int] = field(default_factory=list)


def build_env(extra: Mapping[str, str], remove: tuple[str, ...] = ()) -> dict[str, str]:
    env = dict(os.environ)
    for key in [k for k in env if is_server_only(k)]:
        env.pop(key)
    for key in remove:
        env.pop(key, None)
    # Salida sin buffering y sin colores ni prompts interactivos, para cualquier herramienta.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    env.update(extra)
    return env


def stdbuf_path() -> str | None:
    return shutil.which("stdbuf") or shutil.which("gstdbuf")


def _line_redactor(on_line: Callable[[str, str], None]) -> Callable[[str], str] | None:
    """El redactor del dueño de ``on_line`` si es un método (``Job.add_line`` -> ``Job.redact``)."""
    redact = getattr(getattr(on_line, "__self__", None), "redact", None)
    return redact if callable(redact) else None


async def _pump(
    stream: asyncio.StreamReader, name: str, on_line: Callable[[str, str], None],
    redact: Callable[[str], str] | None = None,
) -> None:
    """Lee ``stream`` y emite líneas (cortadas por ``\\n`` y ``\\r``; las de más de ``MAX_LINE`` en trozos).

    Si hay redactor (``redact`` o el del objeto de ``on_line``), se aplica a la línea ENTERA antes de
    trocearla: un secreto que cruza el corte de 8 KiB no puede quedar partido en dos trozos que ya no
    casan con los patrones.
    """
    redact = redact or _line_redactor(on_line) or (lambda text: text)
    buf = ""
    # Decodificador incremental: un carácter multibyte partido entre dos lecturas (á, ñ, emojis)
    # se completa con la siguiente en vez de salir como "��".
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        try:
            chunk = await stream.read(65536)
        except OSError:  # pty: EIO cuando el último escritor cierra
            break
        if not chunk:
            break
        buf += decoder.decode(chunk)
        while True:
            idx = min((i for i in (buf.find("\n"), buf.find("\r")) if i != -1), default=-1)
            if idx == -1:
                break
            line, buf = buf[:idx], buf[idx + 1 :]
            if line.strip():
                _emit_chunks(on_line, name, redact(line))
        while len(buf) > MAX_LINE + HOLD_BACK:  # línea gigantesca sin salto: se emite troceada
            buf = redact(buf)  # entera; lo que se queda (el final, que aún puede crecer) ya va redactado
            if len(buf) <= MAX_LINE + HOLD_BACK:
                break
            on_line(name, buf[:MAX_LINE])
            buf = buf[MAX_LINE:]
    buf += decoder.decode(b"", final=True)
    if buf.strip():
        _emit_chunks(on_line, name, redact(buf))


def _emit_chunks(on_line: Callable[[str, str], None], name: str, line: str) -> None:
    """Una línea de más de ``MAX_LINE`` se emite en varios trozos: no se pierde nada."""
    for i in range(0, len(line), MAX_LINE):
        on_line(name, line[i : i + MAX_LINE])


def _die_with_parent() -> None:  # pragma: no cover - solo Linux, en el hijo antes de exec
    """Linux: el hijo recibe SIGKILL si muere el servidor (PR_SET_PDEATHSIG), aunque sea con
    kill -9. En macOS no existe; ahí los huérfanos los recoge la siguiente instancia al arrancar."""
    import ctypes

    with contextlib.suppress(Exception):
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)  # 1 = PR_SET_PDEATHSIG


_PREEXEC = {"preexec_fn": _die_with_parent} if sys.platform.startswith("linux") else {}


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if hasattr(os, "killpg"):
            os.killpg(pid, sig)
        else:  # pragma: no cover - Windows
            os.kill(pid, sig)


async def _pty_reader(master: int) -> tuple[asyncio.StreamReader, asyncio.BaseTransport]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 20)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, os.fdopen(master, "rb", buffering=0))
    return reader, transport


async def run_command(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    on_line: Callable[[str, str], None],
    cancel_event: asyncio.Event,
    timeout_s: float | None,
    on_start: Callable[[int], None] | None = None,
    io_mode: str = "pipe",
    redact: Callable[[str], str] | None = None,
) -> ProcResult:
    argv = list(argv)
    if io_mode == "stdbuf":
        if sb := stdbuf_path():
            argv = [sb, "-oL", "-eL", *argv]
        else:
            on_line("core", "stdbuf no disponible: se usa un pty para evitar el buffering")
            io_mode = "pty"

    master = slave = None
    if io_mode == "pty":
        import pty

        master, slave = pty.openpty()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=slave if slave is not None else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
            **_PREEXEC,
        )
    except BaseException:
        if master is not None:
            os.close(master)
        raise
    finally:
        if slave is not None:
            os.close(slave)  # solo el hijo debe tenerlo abierto: así llega EOF/EIO al acabar

    if on_start:
        on_start(proc.pid)
    transport = None
    assert proc.stderr is not None
    if master is not None:
        out_reader, transport = await _pty_reader(master)
    else:
        assert proc.stdout is not None
        out_reader = proc.stdout
    pumps = [
        asyncio.create_task(_pump(out_reader, "stdout", on_line, redact)),
        asyncio.create_task(_pump(proc.stderr, "stderr", on_line, redact)),
    ]
    # No se usa proc.wait(): desde Python 3.12 no vuelve hasta que se cierran también las
    # tuberías, así que un hijo que siga vivo con nuestra stdout abierta lo dejaría colgado
    # aunque el proceso principal ya haya terminado.
    waiter = asyncio.create_task(_exited(proc))
    canceller = asyncio.create_task(cancel_event.wait())
    result = ProcResult(exit_code=None, pid=proc.pid)
    try:
        done, _ = await asyncio.wait({waiter, canceller}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            if canceller in done:
                result.cancelled = True
            else:
                result.timed_out = True
            await _terminate(proc.pid, waiter, result, on_line)
        result.exit_code = proc.returncode
        if not await _drained(pumps, DRAIN_S):
            # El proceso principal terminó pero dejó hijos que mantienen abiertas nuestras
            # tuberías: se termina el grupo para no colgar el job.
            _signal_group(proc.pid, signal.SIGTERM)
            await _drained(pumps, TERM_GRACE_S)
            await _sweep_group(proc.pid, result)
        return result
    except asyncio.CancelledError:
        _signal_group(proc.pid, signal.SIGKILL)
        raise
    finally:
        canceller.cancel()
        for t in pumps:
            if not t.done():
                t.cancel()
            elif not t.cancelled():
                t.exception()  # se recupera: si no, asyncio lo registra al recolectarlo
        if transport is not None:
            transport.close()


async def _drained(pumps: list[asyncio.Task[None]], timeout_s: float) -> bool:
    """True si todos los lectores terminaron (EOF) dentro del plazo. No cancela nada."""
    _, pending = await asyncio.wait(pumps, timeout=timeout_s)
    return not pending


async def _exited(proc: asyncio.subprocess.Process) -> int:
    """Espera a que termine el proceso principal (solo él, no sus tuberías)."""
    while proc.returncode is None:
        await asyncio.sleep(0.05)
    return proc.returncode


async def _terminate(
    pid: int, waiter: asyncio.Task[int], result: ProcResult,
    on_line: Callable[[str, str], None] | None = None,
) -> None:
    """SIGTERM al grupo, espera acotada, SIGKILL, espera acotada. Nunca espera indefinidamente.

    Que muera el líder no basta: un hijo del mismo grupo puede ignorar SIGTERM y quedarse
    vivo (adoptado por init/launchd) con el puerto o los ficheros ocupados. Por eso, al
    final, se barre el grupo completo (ver ``_sweep_group``) y se persigue a los descendientes que
    ya habían salido del grupo (``_reap_escapees``).
    """
    escapees = await _escapees(pid)  # antes de señalar: después el árbol se pierde (los adopta init)
    _signal_group(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(waiter), TERM_GRACE_S)
    except TimeoutError:
        _signal_group(pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), KILL_GRACE_S)
        except TimeoutError:
            result.abandoned = True
    await _sweep_group(pid, result)
    if escapees:
        await _reap_escapees(escapees, result, on_line)


async def _process_table() -> list[tuple[int, int, int]] | None:
    """(pid, ppid, pgid) de los procesos vivos (no zombis); ``None`` si no se puede saber (sin ``ps``)."""
    if not shutil.which("ps"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-A", "-o", "pid=,ppid=,pgid=,stat=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, TimeoutError):
        return None
    table = []
    for line in out.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 4 and not parts[3].startswith("Z") and all(p.isdigit() for p in parts[:3]):
            table.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return table


async def _group_members(pgid: int) -> list[int] | None:
    """PIDs vivos (no zombis) del grupo ``pgid``; ``None`` si no se puede saber (sin ``ps``)."""
    table = await _process_table()
    return None if table is None else [pid for pid, _, group in table if group == pgid]


async def _escapees(leader: int) -> dict[int, int]:
    """Descendientes del líder (por ppid) que ya NO están en su grupo: ``{pid: pgid}``.

    Son los que hicieron ``setsid`` o crearon su propio grupo (servidores de desarrollo, daemons...):
    ``killpg`` no los alcanza. Hay que tomarlos ANTES de señalar al líder.
    """
    table = await _process_table()
    if not table:
        return {}
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in table:
        children.setdefault(ppid, []).append(pid)
    pgids = {pid: group for pid, _, group in table}
    found: dict[int, int] = {}
    stack = list(children.get(leader, []))
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        if pgids[pid] != leader:
            found[pid] = pgids[pid]
        stack.extend(children.get(pid, []))
    return found


def _signal_pid(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


async def _still_alive(escapees: dict[int, int]) -> list[int]:
    """Los de la foto que siguen vivos con el mismo pid y el mismo grupo (descarta pids reutilizados)."""
    table = await _process_table()
    if table is None:
        return []
    now = {pid: group for pid, _, group in table}
    return [pid for pid, group in escapees.items() if now.get(pid) == group]


async def _reap_escapees(
    escapees: dict[int, int], result: ProcResult, on_line: Callable[[str, str], None] | None
) -> None:
    """SIGTERM, gracia, SIGKILL y gracia a los descendientes que escaparon del grupo."""
    alive = await _still_alive(escapees)
    if not alive:
        return
    result.escaped = sorted(alive)
    if on_line:
        on_line("core", f"{len(alive)} proceso(s) descendiente(s) habían salido del grupo (setsid) y se "
                        f"terminan con el job: pid {', '.join(map(str, sorted(alive)))}")
    loop = asyncio.get_running_loop()
    for sig, grace in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
        for pid in alive:
            _signal_pid(pid, sig)
        deadline = loop.time() + grace
        while alive and loop.time() < deadline:
            await asyncio.sleep(0.1)
            alive = await _still_alive({pid: escapees[pid] for pid in alive})
        if not alive:
            return
    result.survivors += alive


async def _group_alive(pgid: int) -> list[int]:
    members = await _group_members(pgid)
    if members is not None:
        return members
    try:  # sin ps: killpg(0) no distingue zombis, pero es mejor que nada
        os.killpg(pgid, 0)
        return [pgid]
    except (ProcessLookupError, PermissionError):
        return []


async def _sweep_group(pgid: int, result: ProcResult) -> None:
    """Tras matar al líder: si queda alguien en el grupo, espera la gracia de SIGTERM y luego SIGKILL."""
    if not hasattr(os, "killpg"):  # pragma: no cover - Windows
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TERM_GRACE_S
    alive = await _group_alive(pgid)
    while alive and loop.time() < deadline:  # ya recibieron SIGTERM: se les da la misma gracia
        await asyncio.sleep(0.2)
        alive = await _group_alive(pgid)
    if not alive:
        return
    result.killed = alive
    _signal_group(pgid, signal.SIGKILL)
    deadline = loop.time() + KILL_GRACE_S
    while alive and loop.time() < deadline:
        await asyncio.sleep(0.1)
        alive = await _group_alive(pgid)
    result.survivors = alive


async def _read_capped(stream: asyncio.StreamReader, limit: int, over: asyncio.Event) -> bytes:
    """Lee hasta EOF guardando como mucho ``limit`` bytes; al pasarse, avisa por ``over`` y deja de leer."""
    data = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(data)
        room = limit - len(data)
        data += chunk[:room]
        if len(chunk) > room:
            over.set()
            return bytes(data)


async def run_capture(
    argv: list[str], *, env: Mapping[str, str], cwd: str | None = None, timeout_s: float = 60,
    max_bytes: int | None = None,
) -> tuple[int, str, str]:
    """Ejecuta un comando corto capturando su salida (sondas de versión). Acotado por timeout y por
    ``max_bytes`` (por defecto ``MAX_CAPTURE_BYTES``) por flujo: un programa que imprime sin parar se
    mata en vez de llenar la memoria."""
    limit = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    over = asyncio.Event()
    readers = [asyncio.create_task(_read_capped(s, limit, over)) for s in (proc.stdout, proc.stderr)]
    everything = asyncio.ensure_future(asyncio.gather(*readers))
    overflow = asyncio.create_task(over.wait())
    try:
        done, _ = await asyncio.wait({everything, overflow}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if everything in done:
            out, err = everything.result()
            await asyncio.wait_for(_exited(proc), KILL_GRACE_S)
            return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
        failure = (
            f"la salida superó el límite de {limit} bytes; se cortó" if overflow in done
            else f"timeout tras {timeout_s}s"
        )
        _signal_group(proc.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_exited(proc), KILL_GRACE_S)
        return -1, "", failure
    except TimeoutError:  # no terminó de recogerse tras cerrar las tuberías
        _signal_group(proc.pid, signal.SIGKILL)
        return -1, "", f"timeout tras {timeout_s}s"
    finally:
        overflow.cancel()
        everything.cancel()
        for t in readers:
            t.cancel()
        # Se recuperan las excepciones de lo cancelado: nada de «exception was never retrieved».
        await asyncio.gather(everything, overflow, *readers, return_exceptions=True)
        _signal_group(proc.pid, signal.SIGKILL)  # cancelado desde fuera o salida inesperada: sin huérfanos
        # Se cierran las tuberías del transporte: si se cortó antes de EOF, si no, se cierran al recolectarlo
        # (y con el bucle ya cerrado, asyncio se queja en ``__del__``).
        with contextlib.suppress(Exception):
            proc._transport.close()  # type: ignore[attr-defined]
