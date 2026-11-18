"""Ejemplo de punta a punta: lanza el servidor por stdio (como Claude Code) e instala un paquete.

    .venv/bin/python examples/demo_client.py pypdf --venv demo

Muestra: job_id inmediato, logs en vivo con since_seq, notifications/progress durante
wait, notifications/message al terminar y notifications/resources/updated del recurso.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from typing import Any

import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

# resources/subscribe es del protocolo con handshake (el que usa Claude Code hoy).
warnings.simplefilter("ignore", MCPDeprecationWarning)


def payload(result: types.CallToolResult) -> Any:
    if result.is_error:
        raise SystemExit(f"error: {result.content[0].text}")
    sc = result.structured_content or {}
    return sc.get("result", sc) if set(sc) == {"result"} else sc


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("packages", nargs="+")
    ap.add_argument("--venv", default="demo")
    ap.add_argument("--state-dir", default=None)
    args = ap.parse_args()

    server_args = ["-m", "watcher_mcp"] + (["--state-dir", args.state_dir] if args.state_dir else [])
    params = StdioServerParameters(command=sys.executable, args=server_args)

    async def on_log(p: types.LoggingMessageNotificationParams) -> None:
        print(f"  [notifications/message {p.level}] {p.data.get('message')}")

    async def on_message(m: Any) -> None:
        if isinstance(m, types.ResourceUpdatedNotification):
            print(f"  [notifications/resources/updated] {m.params.uri}")

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        print(f"  [notifications/progress] {progress:.2f}/{total} {message}")

    async with Client(params, mode="legacy", logging_callback=on_log, message_handler=on_message) as client:
        job = payload(
            await client.call_tool("install", {"backend": "pip", "params": {"packages": args.packages, "venv": args.venv}})
        )
        print(f"install -> {job['job_id']} ({job['state']}): {job['summary']}")
        await client.subscribe_resource(job["uri"])

        seq = 0
        for _ in range(3):  # sigue el log en vivo unas cuantas veces
            await asyncio.sleep(1)
            chunk = payload(await client.call_tool("logs", {"job_id": job["job_id"], "since_seq": seq, "tail": 10}))
            for line in chunk["lines"]:
                print("  log:", line)
            seq = chunk["last_seq"]
            if chunk["state"] not in ("queued", "running"):
                break

        while True:
            res = payload(
                await client.call_tool("wait", {"job_id": job["job_id"], "timeout_s": 30}, progress_callback=on_progress)
            )
            if res["done"]:
                break
            print("  ...sigue en curso:", res["log_tail"][-1] if res["log_tail"] else "")
        await asyncio.sleep(0.3)  # deja llegar las notificaciones finales
        report = res["report"]
        print(json.dumps({k: report[k] for k in ("headline", "state", "exit_code", "duration_s", "installed", "info")},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
