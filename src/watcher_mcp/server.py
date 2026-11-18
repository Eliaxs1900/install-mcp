"""Servidor MCP (SDK oficial ``mcp`` 2.x: ``MCPServer``, sucesor de FastMCP).

Tools: ``install``, ``status``, ``list_jobs``, ``logs``, ``wait``, ``cancel``, ``backends``.
Recursos: ``job://{job_id}`` (estado + informe) y ``jobs://list``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

from .backends import Backend, load_registry
from .config import Config
from .jobs import JobError, JobManager
from .notify import LIST_URI, McpNotifier

INSTRUCTIONS = """\
Lanza y vigila trabajos largos (instalaciones, descargas, clones, builds, tests). Nunca bloquea: `install` devuelve un job_id al instante y
ninguna espera es infinita (wait tiene tope duro; cada job tiene timeout global y watchdog de atasco).
No hace falta sondear: al terminar llega notifications/message con el informe (éxito/fallo, código de
salida, duración, versiones instaladas y, si falló, las líneas relevantes del log), y si el proceso se
queda sin salida llega un aviso `stalled` con pid, comando y últimas líneas. Mientras tanto puedes seguir
trabajando. Estados: queued, running, stalled, succeeded, failed, cancelled, timed_out.
Backends: web (download, binary), código (pip, git, npm, cargo, go, maven, gradle), sistema (brew,
apt, docker) y comandos largos (run, con allowlist). backends() da los parámetros de cada uno.
Herramientas útiles: logs(job_id, tail) para ver la salida en vivo; wait(job_id) para una espera acotada.
Suscríbete a job://<id> para recibir notifications/resources/updated.
"""


def create_server(
    config: Config | None = None,
    registry: Mapping[str, type[Backend[Any]]] | None = None,
) -> tuple[MCPServer, JobManager]:
    config = config or Config.load()
    manager = JobManager(config, registry if registry is not None else load_registry(config.plugins))
    manager.load_history()
    mcp = MCPServer(name="watcher-mcp", instructions=INSTRUCTIONS, version="0.2.0")
    notifier = McpNotifier(bus=getattr(mcp, "_subscriptions", None))
    manager.listeners.append(notifier.on_event)

    # --- suscripciones (protocolo con handshake) ------------------------------------
    low = mcp._lowlevel_server

    async def on_subscribe(ctx: ServerRequestContext[Any], params: types.SubscribeRequestParams) -> types.EmptyResult:
        notifier.subscribe(str(params.uri), ctx.session)
        return types.EmptyResult()

    async def on_unsubscribe(
        ctx: ServerRequestContext[Any], params: types.UnsubscribeRequestParams
    ) -> types.EmptyResult:
        notifier.unsubscribe(str(params.uri), ctx.session)
        return types.EmptyResult()

    async def on_set_level(ctx: ServerRequestContext[Any], params: types.SetLevelRequestParams) -> types.EmptyResult:
        notifier.set_level(ctx.session, params.level)
        return types.EmptyResult()

    low.add_request_handler("resources/subscribe", types.SubscribeRequestParams, on_subscribe)
    low.add_request_handler("resources/unsubscribe", types.UnsubscribeRequestParams, on_unsubscribe)
    low.add_request_handler("logging/setLevel", types.SetLevelRequestParams, on_set_level)

    # --- espera con progreso ------------------------------------------------------------
    async def wait_with_progress(job_id: str, timeout_s: float | None, ctx: Context | None) -> dict[str, Any]:
        """Espera acotada: como mucho ``max_wait_s`` (tope duro), con progreso cada ~1 s."""
        job = manager.get(job_id)
        if timeout_s is None:
            timeout_s = config.default_wait_s
        timeout_s = max(0.0, min(timeout_s, config.max_wait_s))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last_progress = -1.0

        async def report() -> None:
            nonlocal last_progress
            if ctx is None:
                return
            progress, total = job.progress()
            if progress <= last_progress:
                return
            last_progress = progress
            tail = job.tail(1)
            msg = f"{job.current_step or job.state}: {tail[0].text[:200] if tail else ''}"
            try:
                await asyncio.wait_for(ctx.report_progress(progress, total, msg), config.listener_timeout_s)
            except Exception:  # sin progressToken, conexión cerrada o cliente lento
                pass

        while not job.done.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await manager.wait(job_id, min(1.0, remaining))
            await report()
        if job.done.is_set():
            await report()
            return {"done": True, "report": job.report}
        return {
            "done": False,
            "timeout_hit": True,
            "waited_s": round(timeout_s, 3),
            "state": str(job.state),
            "current_step": job.current_step,
            "pid": job.pid,
            "command": job.current_command,
            "silent_for_s": job.silent_for(),
            "duration_s": job.duration(),
            "last_seq": job.last_seq,
            "log_tail": [ln.format() for ln in job.tail(15)],
            "hint": (
                "Sigue en curso. No hace falta sondear: llegará notifications/message al terminar "
                f"(o un aviso si se atasca). Si quieres más detalle: logs('{job.id}', since_seq={job.last_seq})."
            ),
        }

    # --- tools ----------------------------------------------------------------------------
    @mcp.tool(description="Lista los backends (tipos de instalador), si están permitidos/disponibles y su esquema de params.")
    async def backends() -> list[dict[str, Any]]:
        return manager.describe_backends()

    @mcp.tool(
        description=(
            "Lanza una instalación como job en segundo plano y devuelve su job_id al instante. "
            "Ejemplo: backend='pip', params={'packages': ['pypdf'], 'venv': 'tools'}. "
            "Con wait_s>0 espera hasta ese tiempo (con notifications/progress) antes de responder."
        )
    )
    async def install(
        backend: Annotated[str, Field(description="Nombre del backend, p. ej. 'pip'")],
        params: Annotated[dict[str, Any], Field(description="Parámetros del backend (ver tool backends)")],
        ctx: Context,
        wait_s: Annotated[float, Field(ge=0, description="Segundos a esperar antes de responder (0 = no esperar)")] = 0,
    ) -> dict[str, Any]:
        try:
            job = await manager.submit(backend, params, on_created=lambda j: notifier.set_owner(j.id, ctx.session))
        except JobError as exc:
            raise ToolError(str(exc)) from None
        if wait_s > 0:
            result = await wait_with_progress(job.id, wait_s, ctx)
            result["job_id"] = job.id
            return result
        return {
            "job_id": job.id,
            "state": str(job.state),
            "summary": job.summary,
            "uri": job.uri,
            "log_file": str(job.log_path),
            "next": f"wait('{job.id}', timeout_s=60) o logs('{job.id}', tail=30). "
            "Al terminar recibirás notifications/message con el informe.",
        }

    @mcp.tool(
        description=(
            "Estado de un job (queued/running/stalled/succeeded/failed/cancelled/timed_out), paso actual, "
            "pid, comando, segundos sin salida, código de salida, duración y límites."
        )
    )
    async def status(
        job_id: str,
        tail: Annotated[int, Field(ge=0, le=200, description="Incluir las últimas N líneas de log")] = 10,
    ) -> dict[str, Any]:
        try:
            job = manager.get(job_id)
        except JobError as exc:
            raise ToolError(str(exc)) from None
        out = job.status(tail=tail)
        if job.report:
            out["report"] = job.report
        return out

    @mcp.tool(description="Lista los jobs más recientes (opcionalmente filtrados por estado).")
    async def list_jobs(
        state: Literal["queued", "running", "stalled", "succeeded", "failed", "cancelled", "timed_out"] | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        return [
            {
                "job_id": j.id,
                "backend": j.backend,
                "state": str(j.state),
                "summary": j.summary,
                "current_step": j.current_step,
                "exit_code": j.exit_code,
                "duration_s": j.duration(),
                "created_at": j.status()["created_at"],
            }
            for j in manager.list(state, limit)
        ]

    @mcp.tool(
        description=(
            "Últimas N líneas del log de un job (stdout+stderr con marca de tiempo, sin buffering). "
            "Para seguirlo en vivo pasa since_seq=<last_seq de la llamada anterior>."
        )
    )
    async def logs(
        job_id: str,
        tail: Annotated[int, Field(ge=1, le=2000)] = 50,
        since_seq: Annotated[int | None, Field(ge=0, description="Solo líneas posteriores a este número")] = None,
        stream: Literal["all", "stdout", "stderr", "core"] = "all",
    ) -> dict[str, Any]:
        try:
            job = manager.get(job_id)
        except JobError as exc:
            raise ToolError(str(exc)) from None
        lines = job.tail(tail, since_seq=since_seq, stream=stream)
        return {
            "job_id": job.id,
            "state": str(job.state),
            "current_step": job.current_step,
            "last_seq": job.last_seq,
            "lines": [ln.format() for ln in lines],
        }

    @mcp.tool(
        description=(
            "Espera ACOTADA a que termine un job (por defecto default_wait_s, nunca más de max_wait_s). "
            "Devuelve el informe si acaba antes; si no, 'done: false' con estado, pid, segundos sin salida "
            "y últimas líneas. Emite notifications/progress mientras espera."
        )
    )
    async def wait(
        job_id: str,
        ctx: Context,
        timeout_s: Annotated[
            float | None, Field(ge=0, description="Máximo a esperar; vacío = default_wait_s; tope max_wait_s")
        ] = None,
    ) -> dict[str, Any]:
        try:
            return await wait_with_progress(job_id, timeout_s, ctx)
        except JobError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(description="Cancela un job (en cola o en curso). Mata el grupo de procesos (SIGTERM y luego SIGKILL).")
    async def cancel(
        job_id: str,
        wait_s: Annotated[float, Field(ge=0, le=30)] = 10,
    ) -> dict[str, Any]:
        try:
            job = await manager.cancel(job_id)
            await manager.wait(job_id, wait_s)
        except JobError as exc:
            raise ToolError(str(exc)) from None
        out = job.status(tail=5)
        if job.report:
            out["report"] = job.report
        return out

    # --- recursos ----------------------------------------------------------------------------
    @mcp.resource("job://{job_id}", name="job", description="Estado, últimas líneas e informe de un job", mime_type="application/json")
    async def job_resource(job_id: str) -> str:
        try:
            job = manager.get(job_id)
        except JobError as exc:
            raise ResourceNotFoundError(str(exc)) from None
        data = job.status(tail=20)
        if job.report:
            data["report"] = job.report
        return json.dumps(data, ensure_ascii=False, indent=2)

    @mcp.resource(LIST_URI, name="jobs", description="Lista de jobs recientes", mime_type="application/json")
    async def jobs_resource() -> str:
        return json.dumps(
            [{"job_id": j.id, "state": str(j.state), "summary": j.summary, "uri": j.uri} for j in manager.list(limit=50)],
            ensure_ascii=False,
            indent=2,
        )

    return mcp, manager
