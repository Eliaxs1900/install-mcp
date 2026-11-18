"""Graba la ejecución real de un comando en un pty, en formato asciicast v2 (asciinema).

    python scripts/record_cast.py salida.cast --title "demo" -- python examples/demo.py stall

El .cast se puede reproducir con ``asciinema play`` o convertir con ``scripts/render_cast.py``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import struct
import subprocess
import sys
import termios
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--title", default="")
    ap.add_argument("--cols", type=int, default=120)
    ap.add_argument("--rows", type=int, default=40)
    argv = sys.argv[1:]
    if "--" not in argv:
        ap.error("falta '--' antes del comando a grabar")
    split = argv.index("--")
    a = ap.parse_args(argv[:split])
    cmd = argv[split + 1 :]

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", a.rows, a.cols, 0, 0))
    env = {**os.environ, "COLUMNS": str(a.cols), "LINES": str(a.rows), "TERM": "xterm-256color"}
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)

    t0 = time.monotonic()
    events = []
    while True:
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        events.append([round(time.monotonic() - t0, 4), "o", text])
        sys.stdout.write(text)
        sys.stdout.flush()
    code = proc.wait()
    header = {"version": 2, "width": a.cols, "height": a.rows, "timestamp": int(time.time()),
              "title": a.title, "env": {"TERM": "xterm-256color"}}
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.exit(code)


if __name__ == "__main__":
    main()
