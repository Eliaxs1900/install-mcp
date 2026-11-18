"""Utilidades compartidas por las demos: cliente MCP por stdio y salida legible."""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPDeprecationWarning

# resources/subscribe pertenece al protocolo con handshake (el que usa Claude Code hoy).
warnings.simplefilter("ignore", MCPDeprecationWarning)

HOME = str(Path.home())
T0 = time.monotonic()

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (f"\033[{c}m" for c in (31, 32, 33, 34, 35, 36, 90))
LEVEL_COLOR = {"info": CYAN, "warning": YELLOW, "error": RED}


def clean(text: str) -> str:
    """Acorta rutas del usuario (``/Users/x`` -> ``~``) para que las capturas no las muestren."""
    return text.replace(HOME, "~")


def elapsed() -> float:
    return time.monotonic() - T0


def say(text: str = "", color: str = "") -> None:
    stamp = f"{GREY}[{elapsed():5.1f}s]{RESET} "
    print(stamp + color + clean(text) + (RESET if color else ""), flush=True)


def heading(text: str) -> None:
    print(f"\n{BOLD}{BLUE}━━ {text} {'━' * max(3, 86 - len(text))}{RESET}", flush=True)


def call_line(name: str, args: dict[str, Any]) -> None:
    say(f"→ {name}({', '.join(f'{k}={json.dumps(v, ensure_ascii=False)}' for k, v in args.items())})", MAGENTA)


def payload(result: types.CallToolResult) -> Any:
    if result.is_error:
        raise SystemExit(f"error: {result.content[0].text}")
    sc = result.structured_content or {}
    return sc.get("result", sc) if set(sc) == {"result"} else sc


def demo_state_dir() -> Path:
    return Path(os.environ.get("INSTALL_MCP_DEMO_STATE", Path.home() / ".cache" / "install-mcp-demo"))


class Demo:
    """Cliente MCP que imprime cada notificación del servidor según llega."""

    def __init__(self, state_dir: Path, *, show_progress: bool = False) -> None:
        self.state_dir = state_dir
        self.show_progress = show_progress
        self.events: list[dict[str, Any]] = []
        self.client: Client | None = None

    async def on_log(self, p: types.LoggingMessageNotificationParams) -> None:
        data = p.data if isinstance(p.data, dict) else {"message": str(p.data)}
        self.events.append({"t": elapsed(), "level": p.level, **data})
        say(f"◆ notifications/message [{p.level}] {data.get('message')}", LEVEL_COLOR.get(p.level, ""))
        if data.get("event") == "stalled":
            say(f"     pid={data.get('pid')}  sin salida desde hace {data.get('silent_for_s')}s", YELLOW)
            say(f"     comando: {' '.join(data.get('command') or [])[:110]}", YELLOW)
            for line in (data.get("log_tail") or [])[-3:]:
                say(f"     │ {line[25:][:105]}", DIM)

    async def on_message(self, m: Any) -> None:
        if isinstance(m, types.ResourceUpdatedNotification):
            say(f"◆ notifications/resources/updated {m.params.uri}", GREY)

    async def on_progress(self, progress: float, total: float | None, message: str | None) -> None:
        if self.show_progress:
            say(f"… notifications/progress {progress:.2f}/{total:.0f}  {(message or '')[:90]}", GREY)

    def connect(self) -> Client:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "install_mcp", "--state-dir", str(self.state_dir)]
        )
        self.client = Client(params, mode="legacy", logging_callback=self.on_log, message_handler=self.on_message)
        return self.client

    async def tool(self, name: str, args: dict[str, Any], *, quiet: bool = False, progress: bool = False) -> Any:
        assert self.client is not None
        if not quiet:
            call_line(name, args)
        t = time.monotonic()
        res = payload(
            await self.client.call_tool(name, args, progress_callback=self.on_progress if progress else None)
        )
        if not quiet:
            say(f"← {name} respondió en {time.monotonic() - t:.2f}s", GREY)
        return res


def print_report(report: dict[str, Any]) -> None:
    color = GREEN if report["ok"] else (YELLOW if report["state"] == "cancelled" else RED)
    say(f"informe: {report['headline']}", BOLD + color)
    say(f"  state={report['state']}  exit_code={report['exit_code']}  duración={report['duration_s']}s  "
        f"atascos={report.get('stalls', 0)}", color)
    if report.get("installed"):
        say(f"  instalado: {report['installed']}", color)
    for line in report.get("relevant_lines", [])[-4:]:
        say(f"  × {line[25:][:110]}", RED)
