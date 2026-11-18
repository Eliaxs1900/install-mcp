"""Comparativa real: la forma original (Bash + pip) frente a install-mcp, con el mismo paquete.

    .venv/bin/python examples/compare.py [--build-seconds 25] [--limit 8]

Carga de trabajo: ``slowpkg`` (examples/slowpkg.py), un sdist local que tarda N segundos
en compilar sin escribir nada, como un wheel que se compila desde el código fuente. Sin red.

ANTES — lo que hacía el agente al principio:
  1. Bash ``pip install …``: la llamada bloquea al agente. Claude Code la corta a los 120 s
     y la deja en segundo plano escribiendo en un fichero (aquí el límite va a escala: --limit).
  2. A partir de ahí solo puede leer ese fichero de vez en cuando (``cat``/``tail``).
DESPUÉS — install-mcp:
  ``install`` responde al instante; el servidor avisa del atasco y del final por notificaciones MCP.

Todas las cifras de la tabla final se miden en esta ejecución.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import (  # noqa: E402
    BOLD, DIM, GREEN, RED, RESET, YELLOW, Demo, clean, demo_state_dir, elapsed, heading, print_report, say,
)
from slowpkg import make_slow_sdist  # noqa: E402

POLL_S = 5.0


async def before(work: Path, wheels: Path, limit: float) -> dict:
    heading("ANTES · Bash + pip install (la forma original)")
    venv = work / "venv-antes"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    log = work / "salida.log"
    cmd = [str(venv / "bin/python"), "-m", "pip", "install", "--no-cache-dir", "--no-index",
           "--find-links", str(wheels), "slowpkg"]
    say("$ " + " ".join(cmd).replace(str(work), "…"), BOLD)
    t_start, wall_start = time.monotonic(), time.time()
    with open(log, "wb") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    say(f"la llamada a Bash bloquea al agente… (en Claude Code el límite es 120 s; aquí {limit:.0f} s)", DIM)
    try:
        proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        pass
    blocked = time.monotonic() - t_start
    say(f"! Bash: límite de {limit:.0f} s alcanzado → el comando sigue en segundo plano; "
        "el agente solo tiene el fichero de salida", RED)
    polls, last_size, stall_noticed = 0, -1, None
    while proc.poll() is None:
        polls += 1
        data = log.read_bytes()
        last = data.decode(errors="replace").strip().splitlines()[-1:] or ["(vacío)"]
        same = " (sin cambios)" if len(data) == last_size else ""
        say(f"$ tail -1 salida.log  →  {len(data)} bytes{same} │ {last[0].strip()[:70]}", YELLOW if same else "")
        last_size = len(data)
        say("   ¿sigue vivo? ¿colgado? ¿cuánto falta? — el fichero no lo dice; ni pid, ni estado, ni aviso", DIM)
        await asyncio.sleep(POLL_S)
    detected = time.monotonic() - t_start
    finished_at = log.stat().st_mtime - wall_start  # última escritura de pip ≈ final real
    out = log.read_text(errors="replace")
    say(f"el proceso ya no existe (lo descubre el sondeo nº {polls + 1}, {detected - finished_at:.1f} s después "
        f"de terminar); salida final: {len(out.encode())} bytes, exit {proc.returncode}", BOLD)
    for line in out.strip().splitlines()[-2:]:
        say(f"   │ {clean(line.strip())[:100]}", DIM)
    return {
        "blocked_s": blocked, "detected_s": detected, "finished_s": finished_at, "polls": polls + 1,
        "exit": proc.returncode, "stall_notice": stall_noticed,
    }


async def after(state: Path, wheels: Path) -> dict:
    heading("DESPUÉS · install-mcp")
    d = Demo(state)
    async with d.connect():
        t_start = time.monotonic()
        t0_el = elapsed()
        params = {"packages": ["slowpkg"], "venv": "venv-despues", "installer": "pip", "no_index": True,
                  "find_links": [str(wheels)], "stall_timeout_s": 5}
        job = await d.tool("install", {"backend": "pip", "params": params})
        returned = time.monotonic() - t_start
        say(f"el agente recupera el control en {returned:.2f}s y puede seguir trabajando; "
            "los avisos llegarán solos", GREEN)
        res = await d.tool("wait", {"job_id": job["job_id"], "timeout_s": 120})
        await asyncio.sleep(0.5)
        print_report(res["report"])
    rel = {e.get("event"): e["t"] - t0_el for e in d.events}
    return {
        "returned_s": returned, "stall_s": rel.get("stalled"), "finished_s": rel.get("finished"),
        "exit": res["report"]["exit_code"], "installed": res["report"]["installed"].get("slowpkg"),
        "calls": 2,
    }


def table(a: dict, b: dict, limit: float) -> None:
    heading("RESULTADO (medido en esta ejecución)")
    rows = [
        ("El agente recupera el control", f"{a['blocked_s']:.1f} s (límite Bash; 120 s real)", f"{b['returned_s']:.2f} s"),
        ("Durante la compilación ve…", "log congelado en 'Building wheel…'", "estado, pid, comando, s sin salida"),
        ("Aviso de atasco", "ninguno", f"'stalled' a los {b['stall_s']:.1f} s" if b["stall_s"] else "—"),
        ("Se entera de que terminó", f"por sondeo, {a['detected_s'] - a['finished_s']:.1f} s tarde",
         "notificación, al instante"),
        ("Llamadas del agente", f"1 Bash + {a['polls']} sondeos", f"{b['calls']} (install + wait)"),
        ("Informe final", f"texto bruto (exit {a['exit']})", f"exit {b['exit']}, slowpkg {b['installed']}, duración"),
    ]
    w0, w1 = 30, 38
    print(f"{BOLD}  {'':<{w0}} {'ANTES (Bash + pip)':<{w1}} DESPUÉS (install-mcp){RESET}")
    for k, x, y in rows:
        print(f"  {k:<{w0}} {RED}{x:<{w1}}{RESET} {GREEN}{y}{RESET}")
    print(flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-seconds", type=float, default=25)
    ap.add_argument("--limit", type=float, default=8, help="límite de Bash a escala (en Claude Code: 120 s)")
    args = ap.parse_args()
    state = demo_state_dir()
    work = state / f"compare-{int(time.time())}"
    try:
        wheels_a = make_slow_sdist(work / "wheels-a", args.build_seconds).parent
        wheels_b = make_slow_sdist(work / "wheels-b", args.build_seconds).parent
        say(f"carga de trabajo: slowpkg, compilación de {args.build_seconds:.0f} s sin salida (igual en los dos casos)", DIM)
        a = await before(work, wheels_a, args.limit)
        b = await after(state, wheels_b)
        table(a, b, args.limit)
    finally:
        shutil.rmtree(state, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
