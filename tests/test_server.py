"""Servidor MCP de punta a punta con un cliente MCP en proceso."""

from __future__ import annotations

import asyncio
from typing import Any

import mcp_types as types
import pytest
from mcp import Client

from watcher_mcp.notify import McpNotifier
from watcher_mcp.server import create_server


# E2E: herramientas y servicios reales. Las pruebas puntuales se ejecutan con -m 'not e2e'.
pytestmark = pytest.mark.e2e


def data(result: types.CallToolResult) -> Any:
    assert not result.is_error, result.content
    sc = result.structured_content
    return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc


class Recorder:
    def __init__(self) -> None:
        self.logs: list[types.LoggingMessageNotificationParams] = []
        self.updated: list[str] = []
        self.progress: list[tuple[float, float | None, str | None]] = []

    async def on_log(self, params: types.LoggingMessageNotificationParams) -> None:
        self.logs.append(params)

    async def on_message(self, message: Any) -> None:
        if isinstance(message, types.ResourceUpdatedNotification):
            self.updated.append(str(message.params.uri))

    async def on_progress(self, progress: float, total: float | None, message: str | None) -> None:
        self.progress.append((progress, total, message))


async def wait_until(pred, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timeout esperando condición"
        await asyncio.sleep(0.05)


async def test_end_to_end_install_wait_and_notify(config, registry):
    mcp, manager = create_server(config, registry)
    rec = Recorder()
    async with Client(mcp, mode="legacy", logging_callback=rec.on_log, message_handler=rec.on_message) as client:
        res = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 15, "interval": 0.1}}))
        job_id = res["job_id"]
        # Según la planificación del bucle puede haber arrancado ya al responder.
        assert res["state"] in ("queued", "running") and res["uri"] == f"job://{job_id}"
        await client.subscribe_resource(f"job://{job_id}")

        # Logs en vivo mientras corre.
        await asyncio.sleep(0.6)
        live = data(await client.call_tool("logs", {"job_id": job_id, "tail": 3}))
        assert live["state"] == "running" and 0 < len(live["lines"]) <= 3

        # Espera acotada que vence: devuelve "sigue en curso" + últimas líneas.
        partial = data(await client.call_tool("wait", {"job_id": job_id, "timeout_s": 0.3}))
        assert partial["done"] is False and partial["log_tail"] and "hint" in partial

        # Espera con progreso hasta el final.
        final = data(
            await client.call_tool(
                "wait", {"job_id": job_id, "timeout_s": 20}, progress_callback=rec.on_progress
            )
        )
        assert final["done"] is True
        report = final["report"]
        assert report["ok"] and report["exit_code"] == 0 and report["installed"] == {"fake-tool": "1.2.3"}

        # notifications/progress: monótono y termina en total.
        assert rec.progress, "wait debe emitir notifications/progress"
        values = [p for p, _, _ in rec.progress]
        assert values == sorted(values) and len(set(values)) == len(values)
        assert rec.progress[-1][0] == rec.progress[-1][1]

        # notifications/message al terminar, con el informe.
        await wait_until(lambda: any(l.data.get("event") == "finished" for l in rec.logs))
        finished = next(l for l in rec.logs if l.data.get("event") == "finished")
        assert finished.level == "info" and finished.logger == "watcher-mcp"
        assert finished.data["report"]["job_id"] == job_id
        assert [l.data["event"] for l in rec.logs][:2] == ["queued", "started"]

        # resources/updated para el suscriptor de job://<id>.
        await wait_until(lambda: f"job://{job_id}" in rec.updated)

        # El recurso refleja el estado final.
        contents = await client.read_resource(f"job://{job_id}")
        text = contents.contents[0].text
        assert '"state": "succeeded"' in text and '"report"' in text

        status = data(await client.call_tool("status", {"job_id": job_id, "tail": 2}))
        assert status["state"] == "succeeded" and len(status["log_tail"]) == 2
        listed = data(await client.call_tool("list_jobs", {}))
        assert listed[0]["job_id"] == job_id
    await manager.shutdown()


async def test_notification_on_failure_without_waiting(config, registry):
    """El cliente no llama a wait: igualmente recibe el aviso de fallo con el informe."""
    mcp, manager = create_server(config, registry)
    rec = Recorder()
    async with Client(mcp, mode="legacy", logging_callback=rec.on_log, message_handler=rec.on_message) as client:
        res = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 2, "exit_code": 2}}))
        await wait_until(lambda: any(l.data.get("event") == "finished" for l in rec.logs), timeout=10)
        finished = next(l for l in rec.logs if l.data.get("event") == "finished")
        assert finished.level == "error"
        report = finished.data["report"]
        assert report["job_id"] == res["job_id"] and report["exit_code"] == 2 and not report["ok"]
        assert any("something broke" in ln for ln in report["relevant_lines"])
    await manager.shutdown()


async def test_cancel_tool_and_list_subscription(config, registry):
    mcp, manager = create_server(config, registry)
    rec = Recorder()
    async with Client(mcp, mode="legacy", logging_callback=rec.on_log, message_handler=rec.on_message) as client:
        await client.subscribe_resource("jobs://list")
        res = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 500, "interval": 0.1}}))
        await asyncio.sleep(0.4)
        out = data(await client.call_tool("cancel", {"job_id": res["job_id"]}))
        assert out["state"] == "cancelled" and out["report"]["state"] == "cancelled"
        await wait_until(lambda: rec.updated.count("jobs://list") >= 2)  # al encolar y al terminar
        await wait_until(lambda: any(l.data.get("event") == "finished" and l.level == "warning" for l in rec.logs))
    await manager.shutdown()


async def test_set_level_filters_notifications(config, registry):
    mcp, manager = create_server(config, registry)
    rec = Recorder()
    async with Client(mcp, mode="legacy", logging_callback=rec.on_log) as client:
        await client.set_logging_level("error")
        ok = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 1}}))
        bad = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 1, "exit_code": 1}}))
        await client.call_tool("wait", {"job_id": ok["job_id"], "timeout_s": 10})
        await client.call_tool("wait", {"job_id": bad["job_id"], "timeout_s": 10})
        await asyncio.sleep(0.2)
        assert [l.data["job_id"] for l in rec.logs] == [bad["job_id"]]
    await manager.shutdown()


async def test_errors_are_tool_errors(config, registry):
    mcp, manager = create_server(config, registry)
    async with Client(mcp, mode="legacy") as client:
        r = await client.call_tool("install", {"backend": "apt", "params": {}})
        assert r.is_error and "no permitido" in r.content[0].text
        r = await client.call_tool("install", {"backend": "pip", "params": {"packages": ["--index-url=https://evil"]}})
        assert r.is_error and "opciones" in r.content[0].text
        r = await client.call_tool("status", {"job_id": "nope"})
        assert r.is_error
        backends = data(await client.call_tool("backends", {}))
        names = {b["name"]: b for b in backends}
        assert names["pip"]["allowed"] and "packages" in names["pip"]["params_schema"]["properties"]
    await manager.shutdown()


async def test_bus_publish_for_modern_clients():
    published = []

    class Bus:
        async def publish(self, event):
            published.append(event.uri)

    from watcher_mcp.jobs import JobEvent

    class FakeJob:
        id, uri, state, current_step = "abc", "job://abc", "succeeded", None

    n = McpNotifier(bus=Bus())
    await n.on_event(JobEvent("finished", FakeJob(), "info", "done", {"report": {}}))  # type: ignore[arg-type]
    assert published == ["job://abc", "jobs://list"]


async def test_stalled_and_timed_out_arrive_as_notifications(config, registry):
    """Sin sondear: el cliente recibe 'stalled' (pid, comando, últimas líneas) y luego 'timed_out'."""
    mcp, manager = create_server(config, registry)
    rec = Recorder()
    async with Client(mcp, mode="legacy", logging_callback=rec.on_log, message_handler=rec.on_message) as client:
        res = data(
            await client.call_tool(
                "install",
                {"backend": "fake", "params": {"lines": 3, "pause_s": 30, "stall_timeout_s": 0.4, "job_timeout_s": 1.5}},
            )
        )
        job_id = res["job_id"]
        await client.subscribe_resource(f"job://{job_id}")
        await wait_until(lambda: any(l.data.get("event") == "stalled" for l in rec.logs), timeout=5)
        stalled = next(l for l in rec.logs if l.data.get("event") == "stalled")
        assert stalled.level == "warning"
        assert stalled.data["pid"] and stalled.data["command"] and stalled.data["log_tail"]
        assert stalled.data["silent_for_s"] >= 0.4
        st = data(await client.call_tool("status", {"job_id": job_id}))
        assert st["state"] == "stalled" and st["pid"] == stalled.data["pid"]
        assert data(await client.call_tool("list_jobs", {"state": "stalled"}))[0]["job_id"] == job_id

        await wait_until(lambda: any(l.data.get("event") == "finished" for l in rec.logs), timeout=12)
        finished = next(l for l in rec.logs if l.data.get("event") == "finished")
        assert finished.level == "error" and finished.data["report"]["state"] == "timed_out"
        assert finished.data["state"] == "timed_out"
        await wait_until(lambda: rec.updated.count(f"job://{job_id}") >= 2)
    await manager.shutdown()


async def test_wait_tool_has_default_and_hard_cap(config, registry):
    config.default_wait_s = 0.3
    config.max_wait_s = 0.6
    mcp, manager = create_server(config, registry)
    async with Client(mcp, mode="legacy") as client:
        res = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 1000, "interval": 0.1}}))
        loop = asyncio.get_running_loop()

        t0 = loop.time()
        out = data(await client.call_tool("wait", {"job_id": res["job_id"]}))  # sin timeout -> default
        assert loop.time() - t0 < 1.5 and out["waited_s"] == 0.3

        t0 = loop.time()
        out = data(await client.call_tool("wait", {"job_id": res["job_id"], "timeout_s": 3600}))  # tope duro
        assert loop.time() - t0 < 2.0
        assert out["done"] is False and out["timeout_hit"] and out["waited_s"] == 0.6
        assert out["state"] == "running" and out["pid"] and out["log_tail"] and "No hace falta sondear" in out["hint"]

        # install con wait_s también respeta el tope.
        t0 = loop.time()
        out = data(await client.call_tool("install", {"backend": "fake", "params": {"lines": 1000, "interval": 0.1},
                                                       "wait_s": 3600}))
        assert loop.time() - t0 < 2.0 and out["done"] is False
        for j in manager.list():
            await manager.cancel(j.id)
    await manager.shutdown()
