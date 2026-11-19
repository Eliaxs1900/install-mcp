"""Puente opcional hacia sessionbus: avisa a la sesión que lanzó el job cuando termina.

Claude Code no enseña al modelo las ``notifications/message`` de MCP, así que la sesión que lanzó
un job no se entera de que acabó hasta que llama a ``wait``/``status``. sessionbus sí lo hace: sus
hooks inyectan en cada sesión las tramas dirigidas a ``nodo/<su id>/…``.

Contrato acordado con sessionbus (docs/design.md, "Protocolo de notificaciones"):

* tema ``nodo/<lanzador>/jobs/<job_id>``, ``kind=info``, caducidad de un día; nada a ``todos/**``;
* ``<lanzador>`` es el nodo de la sesión: ``CLAUDE_CODE_HOST_SESSION_ID`` (o, si no hay,
  ``CLAUDE_CODE_SESSION_ID``) del propio servidor, que es uno por sesión de Claude Code;
* el CLI se lanza **sin** esas dos variables y con ``SESSIONBUS_NODE_NAME=watcher-mcp``: así el
  remitente es ``cli-watcher-mcp``. Si publicara como la propia sesión, el bus no se lo entregaría
  (nunca entrega a un nodo sus propias tramas) y el hook no lo inyectaría;
* prioridad: ``normal`` si termina bien, ``alerta`` si falla, se agota el tiempo o se atasca
  (``baja`` nunca se inyecta).

Es *fail-open*: sin sessionbus, sin identidad, con el CLI ausente, colgado o roto, el job termina
igual y a tiempo. El listener solo lanza una tarea en segundo plano; el fallo se anota en el log del
job y en stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Config, SessionbusConfig
from .jobs import JobEvent

log = logging.getLogger(__name__)

SUMMARY_MAX = 200
BODY_MAX_BYTES = 16 * 1024
# Variables que hacen que el CLI publique "como la sesión" (y el bus no se lo entregue a ella).
IDENTITY_VARS = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
# Segmento de tema válido en sessionbus.
_SEGMENT = re.compile(r"[a-z0-9._-]+")
FALLBACK_CLI = Path("~/Desktop/sessionbus-mcp/.venv/bin/sessionbus")
_RELEVANT_MAX = 20


def launcher_node(env: dict[str, str] | None = None) -> str | None:
    """Nodo de la sesión que lanzó los jobs (este servidor es uno por sesión), o ``None``."""
    env = os.environ if env is None else env
    for var in IDENTITY_VARS:
        value = (env.get(var) or "").strip()
        if value and _SEGMENT.fullmatch(value) and value not in (".", ".."):
            return value
    return None


def resolve_command(cfg: SessionbusConfig) -> list[str] | None:
    """Argv base del CLI: ``command`` de la configuración o autodetección; ``None`` si no existe."""
    if cfg.command:
        head = os.path.expanduser(cfg.command[0])
        found = shutil.which(head)
        return [found or head, *cfg.command[1:]] if found else None
    if found := shutil.which("sessionbus"):
        return [found]
    fallback = FALLBACK_CLI.expanduser()
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return [str(fallback)]
    return None


def _truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    mark = "\n… (recortado)"
    cut = raw[: limit - len(mark.encode())].decode("utf-8", "ignore")
    return cut + mark


class BusBridge:
    def __init__(self, cfg: SessionbusConfig, *, env: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        self._env = dict(os.environ if env is None else env)
        self._tasks: set[asyncio.Task[None]] = set()
        self._warned: set[str] = set()

    # --- listener ---------------------------------------------------------------
    async def on_event(self, event: JobEvent) -> None:
        if event.kind not in self.cfg.events:
            return
        # Nunca bloquea ni retrasa el evento: la publicación va en una tarea aparte.
        task = asyncio.create_task(self._publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout_s: float = 6.0) -> None:
        """Espera (acotada) a las publicaciones en curso; para tests y cierre ordenado."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout_s)

    # --- contenido ----------------------------------------------------------------
    def priority_for(self, event: JobEvent) -> str:
        state = "stalled" if event.kind == "stalled" else str(event.job.state)
        return self.cfg.priorities.get(state, "normal")

    @staticmethod
    def build_summary(event: JobEvent) -> str:
        job = event.job
        text = (job.report or {}).get("headline") if event.kind == "finished" else None
        text = " ".join((text or event.message or f"[{job.id}] {event.kind}").split())
        return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"

    @staticmethod
    def build_body(event: JobEvent) -> str:
        job = event.job
        lines: list[str] = []
        if event.kind == "finished":
            r = job.report or {}
            lines += [
                f"estado: {r.get('state', job.state)}",
                f"exit_code: {r.get('exit_code', job.exit_code)}",
                f"duración: {r.get('duration_s', job.duration())} s",
                f"backend: {job.backend} · {job.summary}",
            ]
            if r.get("error") or job.error:
                lines.append(f"error: {r.get('error') or job.error}")
            if r.get("hint"):
                lines.append(f"pista: {r['hint']}")
            relevant = (r.get("relevant_lines") or [])[-_RELEVANT_MAX:]
            if relevant:
                lines += ["", "líneas relevantes:", *relevant]
            lines += ["", f"log_file: {r.get('log_file', job.log_path)}", f"job: {job.uri}"]
        else:  # stalled
            d = event.data
            lines += [
                f"estado: {job.state} (el proceso sigue vivo)",
                f"paso: {d.get('step')} · pid {d.get('pid')}",
                f"sin salida desde hace: {d.get('silent_for_s')} s",
            ]
            if d.get("command"):
                lines.append("comando: " + " ".join(map(str, d["command"])))
            if d.get("log_tail"):
                lines += ["", "últimas líneas:", *d["log_tail"][-_RELEVANT_MAX:]]
            lines += ["", f"log_file: {job.log_path}", f"job: {job.uri}"]
        # El informe ya va redactado; se vuelve a pasar por el redactor antes de salir del proceso.
        return _truncate_bytes(job.redact("\n".join(lines)), BODY_MAX_BYTES)

    def build_argv(self, base: list[str], event: JobEvent, node: str) -> list[str]:
        return [
            *base, "publish",
            "--topic", f"nodo/{node}/jobs/{event.job.id}",
            "--summary", event.job.redact(self.build_summary(event)),
            "--kind", "info",
            "--priority", self.priority_for(event),
            "--ttl", f"{self.cfg.ttl_s:g}",
            "--body-file", "-",
            "--json",
        ]

    def child_env(self) -> dict[str, str]:
        env = {k: v for k, v in self._env.items() if k not in IDENTITY_VARS}
        env["SESSIONBUS_NODE_NAME"] = self.cfg.node_name
        return env

    # --- publicación ----------------------------------------------------------------
    async def _publish(self, event: JobEvent) -> None:
        job = event.job
        try:
            node = launcher_node(self._env)
            if node is None:
                self._warn_once("identity", "no hay CLAUDE_CODE_HOST_SESSION_ID/CLAUDE_CODE_SESSION_ID en el entorno: "
                                "no se publica en sessionbus")
                return
            base = resolve_command(self.cfg)
            if base is None:
                self._fail(job, "no se encuentra el CLI de sessionbus (configura [sessionbus] command)")
                return
            argv = self.build_argv(base, event, node)
            body = self.build_body(event)
            await self._run(job, argv, body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # fail-open: nada de esto debe tocar el job
            self._fail(job, f"fallo inesperado: {exc!r}")

    async def _run(self, job: Any, argv: list[str], body: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self.child_env(),
            )
        except OSError as exc:
            self._fail(job, f"no se pudo lanzar el CLI: {exc}")
            return
        try:
            out, err = await asyncio.wait_for(proc.communicate(body.encode("utf-8")), self.cfg.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)
            self._fail(job, f"el CLI tardó más de {self.cfg.timeout_s:g}s; se cortó")
            return
        if proc.returncode != 0:
            detail = (err or out).decode("utf-8", "replace").strip()[-300:]
            self._fail(job, f"el CLI terminó con código {proc.returncode}: {job.redact(detail)}")
            return
        frame = ""
        with contextlib.suppress(ValueError, AttributeError):
            frame = f" (trama {json.loads(out.decode('utf-8', 'replace')).get('frame_id')})"
        self._note(job, f"aviso publicado en sessionbus{frame}")

    # --- avisos -----------------------------------------------------------------------
    def _note(self, job: Any, text: str) -> None:
        """Anota en el log del job. El fichero ya se cerró al terminar: se añade la línea a mano."""
        with contextlib.suppress(Exception):
            job.add_line("core", f"sessionbus: {text}")
            with open(job.log_path, "a", encoding="utf-8") as fh:
                fh.write(job.tail(1)[0].format() + "\n")

    def _fail(self, job: Any, text: str) -> None:
        self._note(job, text)
        print(f"watcher-mcp: sessionbus [{job.id}]: {text}", file=sys.stderr)

    def _warn_once(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"watcher-mcp: sessionbus: {text}", file=sys.stderr)


def create_bridge(config: Config) -> BusBridge | None:
    """El puente si está activado en ``[sessionbus]``; si no, ``None`` (no se registra nada)."""
    return BusBridge(config.sessionbus) if config.sessionbus.enabled else None
