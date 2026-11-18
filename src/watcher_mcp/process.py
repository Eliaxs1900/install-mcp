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
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MAX_LINE = 8192
TERM_GRACE_S = 5.0
KILL_GRACE_S = 5.0
DRAIN_S = 3.0


@dataclass
class ProcResult:
    exit_code: int | None
    cancelled: bool = False
    timed_out: bool = False
    pid: int | None = None
    abandoned: bool = False  # ni SIGKILL lo terminó a tiempo


def build_env(extra: Mapping[str, str], remove: tuple[str, ...] = ()) -> dict[str, str]:
    env = dict(os.environ)
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


async def _pump(stream: asyncio.StreamReader, name: str, on_line: Callable[[str, str], None]) -> None:
    buf = ""
    while True:
        try:
            chunk = await stream.read(65536)
        except OSError:  # pty: EIO cuando el último escritor cierra
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="replace")
        while True:
            idx = min((i for i in (buf.find("\n"), buf.find("\r")) if i != -1), default=-1)
            if idx == -1:
                break
            line, buf = buf[:idx], buf[idx + 1 :]
            if line.strip():
                on_line(name, line[:MAX_LINE])
        if len(buf) > MAX_LINE:  # línea gigantesca sin salto: se emite troceada
            on_line(name, buf[:MAX_LINE])
            buf = buf[MAX_LINE:]
    if buf.strip():
        on_line(name, buf[:MAX_LINE])


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
        asyncio.create_task(_pump(out_reader, "stdout", on_line)),
        asyncio.create_task(_pump(proc.stderr, "stderr", on_line)),
    ]
    waiter = asyncio.create_task(proc.wait())
    canceller = asyncio.create_task(cancel_event.wait())
    result = ProcResult(exit_code=None, pid=proc.pid)
    try:
        done, _ = await asyncio.wait({waiter, canceller}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            if canceller in done:
                result.cancelled = True
            else:
                result.timed_out = True
            await _terminate(proc.pid, waiter, result)
        result.exit_code = proc.returncode
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*pumps)), DRAIN_S)
        except TimeoutError:
            # El proceso principal terminó pero dejó hijos que mantienen abiertas nuestras
            # tuberías: se termina el grupo para no colgar el job.
            _signal_group(proc.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*pumps), TERM_GRACE_S)
        return result
    except asyncio.CancelledError:
        _signal_group(proc.pid, signal.SIGKILL)
        raise
    finally:
        canceller.cancel()
        for t in pumps:
            if not t.done():
                t.cancel()
        if transport is not None:
            transport.close()


async def _terminate(pid: int, waiter: asyncio.Task[int], result: ProcResult) -> None:
    """SIGTERM al grupo, espera acotada, SIGKILL, espera acotada. Nunca espera indefinidamente."""
    _signal_group(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(waiter), TERM_GRACE_S)
        return
    except TimeoutError:
        pass
    _signal_group(pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(asyncio.shield(waiter), KILL_GRACE_S)
    except TimeoutError:
        result.abandoned = True


async def run_capture(
    argv: list[str], *, env: Mapping[str, str], cwd: str | None = None, timeout_s: float = 60
) -> tuple[int, str, str]:
    """Ejecuta un comando corto capturando su salida (sondas de versión). Acotado por timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        _signal_group(proc.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
        return -1, "", f"timeout tras {timeout_s}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
