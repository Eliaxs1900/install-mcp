"""Demos reales de install-mcp, por stdio (igual que lo lanza Claude Code).

    .venv/bin/python examples/demo.py install   # pypdf desde PyPI vía uv: éxito con aviso al terminar
    .venv/bin/python examples/demo.py fail      # paquete inexistente: fallo con líneas relevantes
    .venv/bin/python examples/demo.py stall     # compilación silenciosa: aviso 'stalled' y luego éxito
    .venv/bin/python examples/demo.py timeout   # compilación eterna: 'timed_out' y proceso terminado

Todo lo que se imprime viene del servidor real; solo se acortan las rutas del usuario a ``~``.
El estado de la demo va a ``~/.cache/install-mcp-demo`` (cámbialo con INSTALL_MCP_DEMO_STATE).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import BOLD, DIM, Demo, demo_state_dir, heading, print_report, say  # noqa: E402
from slowpkg import make_slow_sdist  # noqa: E402


async def follow_until_done(d: Demo, job_id: str, *, peek: int = 0) -> dict:
    """Muestra unas líneas en vivo (opcional) y luego espera con wait() acotado, sin sondear el estado."""
    seq = 0
    for _ in range(peek):
        await asyncio.sleep(1)
        chunk = await d.tool("logs", {"job_id": job_id, "since_seq": seq, "tail": 6}, quiet=True)
        for line in chunk["lines"]:
            say(f"  log │ {line[25:][:110]}", DIM)
        seq = chunk["last_seq"]
    while True:
        res = await d.tool("wait", {"job_id": job_id, "timeout_s": 30}, progress=True)
        if res["done"]:
            return res["report"]
        say(f"  wait volvió a los {res['waited_s']}s: sigue '{res['state']}' (pid {res['pid']}), sin colgarse", DIM)


async def scenario(name: str) -> None:
    state = demo_state_dir()
    d = Demo(state, show_progress=name in ("install",))
    stamp = int(time.time())
    async with d.connect():
        if name == "install":
            heading("install-mcp · instalación real desde PyPI (uv)")
            params = {"packages": ["pypdf"], "venv": f"demo-{stamp}"}
        elif name == "fail":
            heading("install-mcp · fallo con informe")
            params = {"packages": ["paquete-que-no-existe-xyz"], "venv": f"demo-fail-{stamp}", "installer": "pip"}
        else:
            build_s = 18 if name == "stall" else 600
            wheels = make_slow_sdist(state / f"wheels-{stamp}", build_s).parent
            heading(f"install-mcp · {'atasco detectado (stalled)' if name == 'stall' else 'timeout global (timed_out)'}")
            say(f"slowpkg: sdist local que tarda {build_s}s en compilar sin escribir nada", DIM)
            params = {
                "packages": ["slowpkg"], "venv": f"demo-{name}-{stamp}", "installer": "pip",
                "no_index": True, "find_links": [str(wheels)], "stall_timeout_s": 4,
            }
            if name == "timeout":
                params["job_timeout_s"] = 12
        job = await d.tool("install", {"backend": "pip", "params": params})
        say(f"job {job['job_id']} en cola; el cliente queda libre (no se bloquea)", BOLD)
        await d.client.subscribe_resource(job["uri"])  # avisos resources/updated
        report = await follow_until_done(d, job["job_id"], peek=3 if name != "fail" else 0)
        await asyncio.sleep(0.5)  # deja llegar las últimas notificaciones
        print_report(report)
        pids = {e["pid"] for e in d.events if e.get("pid")}
        for pid in sorted(pids):  # el servidor corre en esta misma máquina: se puede comprobar
            try:
                os.kill(pid, 0)
                say(f"  comprobación: el proceso {pid} sigue vivo", "\033[31m")
            except ProcessLookupError:
                say(f"  comprobación: el proceso {pid} (y su grupo) ya no existe ✓", "\033[32m")
    if "--keep" not in sys.argv:
        shutil.rmtree(state, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["install", "fail", "stall", "timeout"])
    ap.add_argument("--keep", action="store_true", help="no borrar el estado de la demo")
    asyncio.run(scenario(ap.parse_args().scenario))


if __name__ == "__main__":
    main()
