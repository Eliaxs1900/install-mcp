"""Puente entre los ``JobEvent`` del núcleo y el protocolo MCP.

Mecanismos usados (ver docs/design.md, "Protocolo de notificaciones"):

* ``notifications/message`` (logging): al dueño del job (la conexión que llamó a
  ``install``) y a quien esté suscrito a ``job://<id>`` o ``jobs://list``, en cada
  transición (queued, started, step, stalled, resumed, finished). ``stalled`` lleva
  pid, comando, segundos sin salida y últimas líneas; ``finished`` lleva el informe.
* ``notifications/resources/updated``: a las conexiones suscritas con
  ``resources/subscribe`` (protocolo con handshake, el que usa hoy Claude Code).
* ``subscriptions/listen`` (protocolo 2026-07-28): se publica ``ResourceUpdated`` en
  el bus del SDK para los clientes modernos.
* ``notifications/progress``: no va aquí; lo emiten ``wait``/``install(wait_s>0)``
  mientras la petición está abierta (el progreso va ligado al progressToken de una
  petición en curso).
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

from mcp.shared.exceptions import MCPDeprecationWarning
from mcp.shared.subscriptions import ResourceUpdated

from .jobs import JobEvent

log = logging.getLogger(__name__)

LIST_URI = "jobs://list"
# Eventos que cambian el estado visible en jobs://list.
LIST_EVENTS = ("queued", "stalled", "resumed", "finished")
_LEVELS = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]


def _conn_key(session: Any) -> Any:
    # ServerSession es por petición en mcp 2.x; la conexión es lo estable.
    return getattr(session, "_connection", session)


class McpNotifier:
    def __init__(self, bus: Any = None) -> None:
        self.bus = bus
        self._sessions: dict[Any, Any] = {}
        self._owners: dict[str, Any] = {}
        self._subs: dict[str, set[Any]] = {}
        self._levels: dict[Any, str] = {}

    # --- registro ---------------------------------------------------------------
    def _remember(self, session: Any) -> Any:
        key = _conn_key(session)
        self._sessions[key] = session
        return key

    def set_owner(self, job_id: str, session: Any) -> None:
        if session is not None:
            self._owners[job_id] = self._remember(session)

    def subscribe(self, uri: str, session: Any) -> None:
        self._subs.setdefault(uri, set()).add(self._remember(session))

    def unsubscribe(self, uri: str, session: Any) -> None:
        self._subs.get(uri, set()).discard(_conn_key(session))

    def set_level(self, session: Any, level: str) -> None:
        self._levels[self._remember(session)] = level

    def _forget(self, key: Any) -> None:
        self._sessions.pop(key, None)
        self._levels.pop(key, None)
        for subs in self._subs.values():
            subs.discard(key)
        for job_id in [j for j, k in self._owners.items() if k == key]:
            self._owners.pop(job_id, None)

    def _wants(self, key: Any, level: str) -> bool:
        minimum = self._levels.get(key, "debug")
        return _LEVELS.index(level) >= _LEVELS.index(minimum)

    # --- eventos ------------------------------------------------------------------
    async def on_event(self, event: JobEvent) -> None:
        job = event.job
        uri = job.uri
        log_targets: set[Any] = set(self._subs.get(uri, set())) | set(self._subs.get(LIST_URI, set()))
        if owner := self._owners.get(job.id):
            log_targets.add(owner)

        payload: dict[str, Any] = {
            "message": event.message,
            "event": event.kind,
            "job_id": job.id,
            "uri": uri,
            "state": str(job.state),
            "current_step": job.current_step,
        }
        # finished -> report; stalled -> pid, command, silent_for_s, log_tail...
        payload.update(event.data)

        for key in log_targets:
            if not self._wants(key, event.level):
                continue
            await self._send(key, "log", event.level, payload)

        updated = [(k, uri) for k in self._subs.get(uri, set())]
        if event.kind in LIST_EVENTS:
            updated += [(k, LIST_URI) for k in self._subs.get(LIST_URI, set())]
        for key, target_uri in updated:
            await self._send(key, "updated", target_uri)

        if self.bus is not None:
            try:
                await self.bus.publish(ResourceUpdated(uri=uri))
                if event.kind in LIST_EVENTS:
                    await self.bus.publish(ResourceUpdated(uri=LIST_URI))
            except Exception as exc:  # pragma: no cover - el bus en memoria no falla
                log.debug("bus publish falló: %r", exc)

        if event.kind == "finished":
            self._owners.pop(job.id, None)

    async def _send(self, key: Any, kind: str, *args: Any) -> None:
        session = self._sessions.get(key)
        if session is None:
            return
        try:
            if kind == "log":
                level, data = args
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", MCPDeprecationWarning)
                    await session.send_log_message(level=level, data=data, logger="install-mcp")
            else:
                await session.send_resource_updated(args[0])
        except Exception as exc:
            # Conexión cerrada o cliente que no admite el mensaje: se olvida.
            log.debug("no se pudo notificar (%s): %r", kind, exc)
            self._forget(key)
